"""教师复核、终分、成绩导出，以及学生的"申请复核"。

## 为什么复核是这套系统可信度的地基

机器分永远不能是终分。原因不是"AI 不够准"，而是**权责**：
成绩决定学生评优、毕业、就业，这个决定必须有一个能负责的人做出。
知识库文档里白纸黑字写着"教师复核为最终确认"，而 V1 的代码里
没有任何改分机制——文档和实现对不上，出事时系统帮不了任何人。

## 三件事必须可追溯

改分是这个系统里最敏感的写操作，所以每一次都要在 `AuditLog` 里留下
**谁、什么时候、把什么、从多少改成了多少**。这不是为了追责，
而是因为"我说我改过"和"系统显示你没改"之间的争议，
只能靠一条不可篡改的记录来解决。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import current_user, require_teacher
from ..clock import now
from ..database import get_session
from ..models import (
    AnalysisJob,
    EvaluationReport,
    ReviewRequest,
    ReviewStatus,
    RosterEntry,
    StepEvent,
    User,
    UserRole,
    VideoSubmission,
    Violation,
    ViolationStatus,
)
from ..services import audit
from ..services.rule_engine import confirmed_violations, final_score_for

router = APIRouter(prefix="/api/reviews", tags=["reviews"])

# 允许人工改判的判定值。与 VLM 的输出枚举是同一套——
# 教师改判走的是同一个语义空间，否则规则引擎要处理两套词汇。
_HUMAN_VERDICTS = {"completed", "not_completed", "not_visible", "not_applicable"}


class ViolationReview(BaseModel):
    action: str                 # confirm | dismiss
    comment: str = ""


class StepOverride(BaseModel):
    verdict: str
    reason: str = ""


class FinalizeRequest(BaseModel):
    comment: str = ""
    # 教师可以在终审时手工给一个总分（比如学生申诉后重新看视频）。
    # 为空表示按当前生效的扣分项自动算。
    final_score: Optional[float] = None


class AppealRequest(BaseModel):
    message: str = ""


def _job_or_404(session: Session, job_id: int) -> AnalysisJob:
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在。")
    return job


def _report_or_404(session: Session, job_id: int) -> EvaluationReport:
    report = session.exec(
        select(EvaluationReport).where(EvaluationReport.job_id == job_id)
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="该任务还没有生成评价报告。")
    return report


def _locked(report: EvaluationReport) -> bool:
    return report.review_status == ReviewStatus.confirmed


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------


@router.get("/queue")
def review_queue(
    _: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
    only_flagged: bool = False,
):
    """教师的复核待办列表。

    默认把"机器自己觉得不可靠的"排在前面——那些是 `needs_review` 的任务，
    教师的时间应该优先花在最可能出错的地方，而不是平均分配。
    """
    query = select(EvaluationReport)
    if only_flagged:
        query = query.where(EvaluationReport.review_status == ReviewStatus.needs_review)
    reports = session.exec(query.order_by(EvaluationReport.generated_at.desc())).all()

    items = []
    for report in reports:
        if report.review_status == ReviewStatus.confirmed:
            continue
        job = session.get(AnalysisJob, report.job_id)
        if job is None:
            continue
        submission = session.get(VideoSubmission, job.submission_id)
        invisible = len(
            session.exec(
                select(StepEvent).where(
                    StepEvent.job_id == job.id, StepEvent.verdict == "not_visible"
                )
            ).all()
        )
        items.append(
            {
                "job_id": job.id,
                "submission_id": job.submission_id,
                "student_name": submission.student_name if submission else "",
                "student_no": submission.student_no if submission else "",
                "task_id": submission.task_id if submission else None,
                "machine_score": report.score,
                "review_status": report.review_status,
                "invisible_steps": invisible,
                "generated_at": report.generated_at,
                "job_status": job.status,
            }
        )

    # needs_review 优先，其次按生成时间倒序
    items.sort(key=lambda item: (item["review_status"] != ReviewStatus.needs_review,
                                 -(item["generated_at"].timestamp() if item["generated_at"] else 0)))
    return items


# ---------------------------------------------------------------------------
# 扣分项与步骤改判
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/violations/{violation_id}")
def review_violation(
    job_id: int,
    violation_id: int,
    payload: ViolationReview,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    """确认或驳回（误判）一条扣分项。"""
    report = _report_or_404(session, job_id)
    if _locked(report):
        raise HTTPException(status_code=409, detail="本次作业已终审，如需修改请先撤销终审。")

    violation = session.get(Violation, violation_id)
    if not violation or violation.job_id != job_id:
        raise HTTPException(status_code=404, detail="扣分项不存在。")

    action = payload.action.strip().lower()
    if action not in {"confirm", "dismiss"}:
        raise HTTPException(status_code=400, detail="action 只能是 confirm 或 dismiss。")

    before = violation.status
    violation.status = ViolationStatus.confirmed if action == "confirm" else ViolationStatus.dismissed
    violation.reviewer_id = teacher.id
    violation.review_comment = payload.comment.strip()
    violation.reviewed_at = now()
    session.add(violation)

    report.final_score = final_score_for(session, job_id)
    session.add(report)
    session.commit()

    audit.record(
        session,
        action="review_violation",
        user=teacher,
        target_type="violation",
        target_id=violation_id,
        detail=(
            f"扣分项「{violation.title}」（扣 {violation.deduction:g} 分）"
            f"{'确认' if action == 'confirm' else '驳回'}：{before.value} → {violation.status.value}；"
            f"机器分 {report.score}，当前终分 {report.final_score}"
            + (f"；教师备注：{violation.review_comment}" if violation.review_comment else "")
        ),
    )
    return {"violation_id": violation_id, "status": violation.status,
            "final_score": report.final_score}


@router.post("/jobs/{job_id}/steps/{step_id}")
def override_step(
    job_id: int,
    step_id: int,
    payload: StepOverride,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    """人工改判某个步骤（如"AI 漏判了验电"）。

    改判**不改原记录**，而是新增一条 `source="human"` 的判定。
    规则引擎按"人工优先"取用。保留两条是为了事后能回答
    "AI 当时判的是什么、老师改成了什么"——那是评估模型准确率的原始数据，
    覆盖掉就永远拿不回来了。
    """
    report = _report_or_404(session, job_id)
    if _locked(report):
        raise HTTPException(status_code=409, detail="本次作业已终审，如需修改请先撤销终审。")

    original = session.get(StepEvent, step_id)
    if not original or original.job_id != job_id:
        raise HTTPException(status_code=404, detail="步骤记录不存在。")

    verdict = payload.verdict.strip()
    if verdict not in _HUMAN_VERDICTS:
        raise HTTPException(
            status_code=400,
            detail=f"verdict 必须是 {'、'.join(sorted(_HUMAN_VERDICTS))} 之一。",
        )

    session.add(
        StepEvent(
            job_id=job_id,
            step_code=original.step_code,
            step_name=original.step_name,
            start_sec=original.start_sec,
            end_sec=original.end_sec,
            confidence=1.0,
            evidence=payload.reason.strip() or f"教师人工改判为 {verdict}",
            evidence_frames=list(original.evidence_frames or []),
            source="human",
            verdict=verdict,
            needs_review=False,
        )
    )
    session.commit()

    _recalculate(session, job_id)
    audit.record(
        session, action="override_step", user=teacher, target_type="step", target_id=step_id,
        detail=f"「{original.step_name}」机器判定 {original.verdict} → 教师改判 {verdict}"
        f"（{payload.reason or '未填写理由'}）",
    )
    return {"step_code": original.step_code, "verdict": verdict}


def _recalculate(session: Session, job_id: int) -> None:
    """按当前生效的步骤判定与扣分项重算。

    重算而不是增量调整：增量调整会随改判次数累积误差，
    而"终分 = 100 - 生效扣分"这个式子必须永远成立，否则没人能解释分数怎么来的。
    """
    from ..services.rule_engine import evaluate_rules

    # 先清掉机器判定的扣分项（人工已处理的保留，它们是教师的决定）
    existing = session.exec(select(Violation).where(Violation.job_id == job_id)).all()
    for item in existing:
        if item.status == ViolationStatus.auto:
            session.delete(item)
    # 人工改判产生的自动扣分同样重新生成，但保留教师已处理的那些
    session.flush()

    evaluation = evaluate_rules(session, job_id)

    report = session.exec(
        select(EvaluationReport).where(EvaluationReport.job_id == job_id)
    ).first()
    if report is None:
        session.commit()
        return

    job = session.get(AnalysisJob, job_id)
    if job is not None:
        job.score = evaluation.score
        session.add(job)

    report.score = evaluation.score
    report.final_score = final_score_for(session, job_id)
    if evaluation.needs_review and report.review_status == ReviewStatus.pending:
        report.review_status = ReviewStatus.needs_review
    session.add(report)
    session.commit()


# ---------------------------------------------------------------------------
# 终审
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/finalize")
def finalize(
    job_id: int,
    payload: FinalizeRequest,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    """提交终审：锁定终分。"""
    report = _report_or_404(session, job_id)
    if _locked(report):
        raise HTTPException(status_code=409, detail="本次作业已经终审过了。")

    before = report.final_score
    if payload.final_score is not None:
        if not 0 <= payload.final_score <= 100:
            raise HTTPException(status_code=400, detail="终分必须在 0 到 100 之间。")
        report.final_score = payload.final_score
    elif report.final_score is None:
        report.final_score = final_score_for(session, job_id)

    report.review_status = ReviewStatus.confirmed
    report.reviewer_id = teacher.id
    report.reviewed_at = now()
    report.review_comment = payload.comment.strip()
    session.add(report)

    # 终审即锁定：把所有机器扣分项标记为已确认，
    # 否则待办列表里它们会一直挂着。
    for item in session.exec(select(Violation).where(Violation.job_id == job_id)).all():
        if item.status == ViolationStatus.auto:
            item.status = ViolationStatus.confirmed
            item.reviewer_id = teacher.id
            item.reviewed_at = now()
            session.add(item)

    pending = session.exec(
        select(ReviewRequest).where(ReviewRequest.job_id == job_id, ReviewRequest.resolved == False)  # noqa: E712
    ).all()
    for request in pending:
        request.resolved = True
        request.resolver_id = teacher.id
        request.resolved_at = now()
        session.add(request)

    session.commit()

    audit.record(
        session, action="finalize_review", user=teacher, target_type="job", target_id=job_id,
        detail=f"终审：机器分 {report.score}，终分 {report.final_score}"
        f"（原终分 {before}）。评语：{payload.comment or '无'}",
    )
    return {"job_id": job_id, "final_score": report.final_score,
            "review_status": report.review_status, "reviewed_at": report.reviewed_at}


@router.post("/jobs/{job_id}/reopen")
def reopen(
    job_id: int,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    """撤销终审，允许重新修改。

    必须有这个入口。没有它，教师终审后发现算错了就只能去改数据库，
    而改数据库是没有审计记录的——那才是真正的风险。
    """
    report = _report_or_404(session, job_id)
    if not _locked(report):
        raise HTTPException(status_code=409, detail="本次作业尚未终审。")

    report.review_status = ReviewStatus.pending
    report.reviewed_at = None
    report.review_comment = ""
    session.add(report)
    session.commit()

    audit.record(
        session, action="reopen_review", user=teacher, target_type="job", target_id=job_id,
        detail=f"撤销终审，终分 {report.final_score} 重新开放修改",
    )
    return {"job_id": job_id, "review_status": report.review_status}


# ---------------------------------------------------------------------------
# 学生申诉
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/appeal")
def appeal(
    job_id: int,
    payload: AppealRequest,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    """学生对成绩申请复核。

    哪怕只做到"按钮 + 一条记录"也要有：第一次误判时，
    学生的申诉路径不该是"去找系主任"。
    """
    job = _job_or_404(session, job_id)
    submission = session.get(VideoSubmission, job.submission_id)
    if user.role == UserRole.student and (not submission or submission.student_id != user.id):
        raise HTTPException(status_code=404, detail="分析任务不存在。")

    message = payload.message.strip()
    if len(message) < 5:
        raise HTTPException(status_code=400, detail="请简要说明你申请复核的理由（至少 5 个字）。")

    existing = session.exec(
        select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.student_id == user.id,
            ReviewRequest.resolved == False,  # noqa: E712
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="你已经提交过复核申请，教师会尽快处理。")

    request = ReviewRequest(job_id=job_id, student_id=user.id, message=message)
    session.add(request)
    session.commit()
    session.refresh(request)

    audit.record(
        session, action="appeal", user=user, target_type="job", target_id=job_id,
        detail=f"学生申请复核：{message[:200]}",
    )
    return {"id": request.id, "created_at": request.created_at,
            "note": "复核申请已提交，教师处理后会更新成绩。"}


@router.get("/appeals")
def list_appeals(
    _: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
    include_resolved: bool = False,
):
    query = select(ReviewRequest).order_by(ReviewRequest.created_at.desc())
    if not include_resolved:
        query = query.where(ReviewRequest.resolved == False)  # noqa: E712
    rows = session.exec(query).all()

    result = []
    for row in rows:
        job = session.get(AnalysisJob, row.job_id)
        submission = session.get(VideoSubmission, job.submission_id) if job else None
        student = session.get(User, row.student_id)
        result.append(
            {
                "id": row.id,
                "job_id": row.job_id,
                "student_name": student.display_name if student else "",
                "student_no": submission.student_no if submission else "",
                "message": row.message,
                "created_at": row.created_at,
                "resolved": row.resolved,
            }
        )
    return result


# ---------------------------------------------------------------------------
# 成绩导出
# ---------------------------------------------------------------------------


@router.get("/export.csv")
def export_scores(
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
    task_id: Optional[int] = None,
):
    """按任务导出成绩 CSV。

    **编码与换行是两个真会出问题的地方**：

    - `utf-8-sig` 而不是 `utf-8`：Excel 打开纯 UTF-8 的 CSV 会把中文显示成乱码，
      而教务老师用的就是 Excel。BOM 是让 Excel 认对编码的唯一可靠办法。
    - `\\r\\n` 而不是 `\\n`：Excel 对 LF 换行的兼容性不稳定，
      偶发把整个文件读成一行。
    """
    tasks = session.exec(select(AnalysisJob)).all()
    rows: list[dict] = []

    for job in tasks:
        submission = session.get(VideoSubmission, job.submission_id)
        if submission is None:
            continue
        if task_id is not None and submission.task_id != task_id:
            continue

        report = session.exec(
            select(EvaluationReport).where(EvaluationReport.job_id == job.id)
        ).first()
        task = None
        from ..models import TrainingTask

        task = session.get(TrainingTask, submission.task_id)

        rows.append(
            {
                "学号": submission.student_no,
                "姓名": submission.student_name,
                "班级": task.class_name if task else "",
                "作业": task.title if task else "",
                "机器分": "" if report is None or report.score is None else f"{report.score:.0f}",
                "终分": "" if report is None or report.final_score is None else f"{report.final_score:.0f}",
                "复核状态": _review_label(report),
                "扣分项数": len(confirmed_violations(session, job.id)),
                "上传时间": submission.uploaded_at.strftime("%Y-%m-%d %H:%M") if submission.uploaded_at else "",
                "文件名": submission.original_filename,
            }
        )

    rows.sort(key=lambda item: (item["班级"], item["学号"]))

    buffer = io.StringIO()
    fieldnames = ["学号", "姓名", "班级", "作业", "机器分", "终分", "复核状态", "扣分项数", "上传时间", "文件名"]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)

    audit.record(
        session, action="export_scores", user=teacher, target_type="task",
        target_id=task_id, detail=f"导出成绩 {len(rows)} 条",
    )

    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="scores-{stamp}.csv"'},
    )


def _review_label(report: EvaluationReport | None) -> str:
    if report is None:
        return "无报告"
    if report.review_status == ReviewStatus.confirmed:
        return "已终审"
    if report.review_status == ReviewStatus.needs_review:
        return "待复核（机器不确定）"
    return "待复核"
