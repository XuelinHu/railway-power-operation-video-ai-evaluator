"""分析任务的查询、重跑与统计。

**分析本身不在这里跑**。V1 的 `run_analysis()` 在这个文件里被
`BackgroundTasks` 调用（长事务跨越分析调用、进程重启即丢失、无法观测），
现在搬到 `app/services/jobs.py`，由 `app/worker.py` 独立进程认领执行。

本模块只做三件事：查状态、把人排进队列、统计。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..auth import current_user, require_teacher
from ..clock import now
from ..database import get_session
from ..models import (
    AnalysisJob,
    AnalysisStatus,
    EvaluationReport,
    ReviewStatus,
    StepEvent,
    User,
    UserRole,
    VideoSubmission,
    Violation,
)
from ..services import audit

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

# 单视频预计耗时（秒）。串行队列下用来给学生一个"还要等多久"的量级感。
# 实测 20-60 秒，取 45 秒做中位数；它只影响文案，不影响任何调度。
SECONDS_PER_JOB = 45


def _job_view(job: AnalysisJob, queue_position: int | None = None) -> dict:
    view = {
        "id": job.id,
        "submission_id": job.submission_id,
        "status": job.status,
        "score": job.score,
        "progress": job.progress,
        "stage": job.stage,
        "summary": job.summary,
        "error": job.error_code,   # 已脱敏，内部堆栈在 error_message 里不外发
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "attempts": job.attempts,
    }
    if queue_position is not None:
        view["queue_position"] = queue_position
        # 老师最怕的不是慢，是"不知道还要多久"而以为系统坏了。
        view["estimated_wait_sec"] = queue_position * SECONDS_PER_JOB
    return view


def _queue_position(session: Session, job: AnalysisJob) -> int | None:
    """返回前面还有几个任务，仅对排队中的任务有意义。"""
    if job.status != AnalysisStatus.pending:
        return None
    ahead = session.exec(
        select(AnalysisJob.id).where(
            AnalysisJob.status == AnalysisStatus.pending, AnalysisJob.id < job.id
        )
    ).all()
    return len(ahead)


def _owned_job(session: Session, job_id: int, user: User) -> AnalysisJob:
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在。")
    if user.role == UserRole.student:
        submission = session.get(VideoSubmission, job.submission_id)
        if not submission or submission.student_id != user.id:
            raise HTTPException(status_code=404, detail="分析任务不存在。")
    return job


@router.get("/jobs")
def list_jobs(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    query = select(AnalysisJob).order_by(AnalysisJob.created_at.desc())
    if user.role == UserRole.student:
        # 学生只看到自己的任务。V1 里这里返回全部人的任务，
        # 连别人的分数都一并送出去了。
        mine = session.exec(
            select(VideoSubmission.id).where(VideoSubmission.student_id == user.id)
        ).all()
        query = query.where(AnalysisJob.submission_id.in_(list(mine) or [0]))
    jobs = session.exec(query).all()
    return [_job_view(job, _queue_position(session, job)) for job in jobs]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _owned_job(session, job_id, user)
    return _job_view(job, _queue_position(session, job))


@router.post("/jobs/{job_id}/rerun")
def rerun_job(
    job_id: int,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    """重新分析：**只把任务放回队列，不在这里跑，也不删复核结果**。

    V1 的实现有两个必须修掉的问题：

    1. 它先删掉 `EvaluationReport` / `Violation` / `StepEvent` 再同步重跑。
       教师刚做完复核、点一下"重新分析"，**复核结论全部消失**——
       而且没有任何提示。改分是有审计留痕的正式操作，
       被一次误点清空是不可接受的。
    2. 它是同步执行的，请求会挂 30-60 秒。100 个学生同时在线的系统里，
       任何"提交后等一分钟"的按钮都会被连点，于是同一份视频分析三遍。

    现在只做一次短事务：清空机器产出 + 置 pending，剩下的交给 worker。
    """
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在。")

    report = session.exec(
        select(EvaluationReport).where(EvaluationReport.job_id == job_id)
    ).first()
    if report and report.review_status == ReviewStatus.confirmed:
        raise HTTPException(
            status_code=409,
            detail="本次作业已完成教师终审，不能重新分析。如确需重评，请先撤销终审。",
        )

    if job.status in {AnalysisStatus.pending, AnalysisStatus.running}:
        raise HTTPException(status_code=409, detail="该任务正在分析中，无需重复提交。")

    # 只清机器产物。教师的人工改判（source="human" 的 StepEvent）保留——
    # 它们是教师的判断，不属于"机器产出"，重跑不该抹掉。
    for model in (StepEvent, Violation, EvaluationReport):
        rows = session.exec(select(model).where(model.job_id == job_id)).all()
        for row in rows:
            if model is StepEvent and row.source == "human":
                continue
            session.delete(row)

    job.status = AnalysisStatus.pending
    job.worker_id = ""
    job.score = None            # 不变量 #1：重跑期间没有分数，不是 0 分
    job.summary = ""
    job.progress = 0
    job.stage = "排队中，等待分析"
    job.error_code = ""
    job.error_message = ""
    job.started_at = None
    job.completed_at = None
    job.heartbeat_at = None
    job.progress_at = None
    session.add(job)
    session.commit()

    audit.record(
        session,
        action="rerun_analysis",
        user=teacher,
        target_type="job",
        target_id=job_id,
        detail=f"重新分析任务 {job_id}（第 {job.attempts + 1} 次）",
    )
    return _job_view(job, _queue_position(session, job))


@router.get("/jobs/{job_id}/detail")
def get_analysis_detail(
    job_id: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _owned_job(session, job_id, user)
    submission = session.get(VideoSubmission, job.submission_id)
    report = session.exec(
        select(EvaluationReport).where(EvaluationReport.job_id == job_id)
    ).first()

    steps = session.exec(
        select(StepEvent).where(StepEvent.job_id == job_id).order_by(StepEvent.start_sec)
    ).all()
    violations = session.exec(
        select(Violation)
        .where(Violation.job_id == job_id)
        .order_by(Violation.deduction.desc())
    ).all()

    return {
        "job": _job_view(job, _queue_position(session, job)),
        "submission": None
        if submission is None
        else {
            "id": submission.id,
            "student_name": submission.student_name,
            "student_no": submission.student_no,
            "original_filename": submission.original_filename,
            "duration_sec": submission.duration_sec,
            "uploaded_at": submission.uploaded_at,
        },
        "steps": [_step_view(step) for step in steps],
        "violations": [_violation_view(item) for item in violations],
        "report": _report_view(report),
        # 学生看到的报告必须写明"以教师复核为准"。知识库里已经这么写了，
        # 界面上不一致的话，第一次误判就会变成一场说不清的纠纷。
        "disclaimer": "AI 分析结果仅供参考，最终成绩以教师复核确认为准。",
    }


def _step_view(step: StepEvent) -> dict:
    return {
        "id": step.id,
        "step_code": step.step_code,
        "step_name": step.step_name,
        "verdict": step.verdict,
        "start_sec": step.start_sec,
        "end_sec": step.end_sec,
        "confidence": step.confidence,
        "evidence": step.evidence,
        "evidence_frames": step.evidence_frames or [],
        "source": step.source,
        "needs_review": step.needs_review,
        "validation_note": step.validation_note,
    }


def _violation_view(item: Violation) -> dict:
    return {
        "id": item.id,
        "rule_code": item.rule_code,
        "title": item.title,
        "deduction": item.deduction,
        "severity": item.severity,
        "timestamp_sec": item.timestamp_sec,
        "reason": item.reason,
        "suggestion": item.suggestion,
        "status": item.status,
        "review_comment": item.review_comment,
    }


def _report_view(report: EvaluationReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "score": report.score,
        "final_score": report.final_score,
        "conclusion": report.conclusion,
        "strengths": report.strengths,
        "problems": report.problems,
        "suggestions": report.suggestions,
        "review_status": report.review_status,
        "reviewed_at": report.reviewed_at,
        "review_comment": report.review_comment,
        "generated_at": report.generated_at,
    }


@router.get("/stats/overview")
def overview(
    _: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    jobs = session.exec(select(AnalysisJob)).all()
    completed = [job for job in jobs if job.status == AnalysisStatus.completed]
    # 只统计真的有分数的那些。把 score 为空的算成 0 分，
    # 会让"视频没拍好"直接拉低班级平均分——那是拿拍摄条件惩罚学生。
    scored = [job.score for job in completed if job.score is not None]
    average = round(sum(scored) / len(scored), 1) if scored else None

    return {
        "total_jobs": len(jobs),
        "completed_jobs": len(completed),
        "failed_jobs": len([job for job in jobs if job.status == AnalysisStatus.failed]),
        "pending_jobs": len([job for job in jobs if job.status == AnalysisStatus.pending]),
        "running_jobs": len([job for job in jobs if job.status == AnalysisStatus.running]),
        "scored_jobs": len(scored),
        "average_score": average,
        "excellent_count": len([score for score in scored if score >= 90]),
        "risk_count": len([score for score in scored if score < 70]),
        "awaiting_review": len(
            session.exec(
                select(EvaluationReport).where(
                    EvaluationReport.review_status != ReviewStatus.confirmed
                )
            ).all()
        ),
    }
