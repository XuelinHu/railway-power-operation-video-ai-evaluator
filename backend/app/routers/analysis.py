from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import UPLOAD_DIR, engine, get_session
from ..models import (
    ActionResult,
    AnalysisJob,
    AnalysisStatus,
    DetectionResult,
    EvaluationReport,
    StepEvent,
    VideoSubmission,
    Violation,
)
from ..services.analyzer import Analyzer
from ..services.report import generate_report
from ..services.rule_engine import evaluate_rules

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def run_analysis(job_id: int) -> None:
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        if not job:
            return
        job.status = AnalysisStatus.running
        job.started_at = datetime.utcnow()
        session.add(job)
        session.commit()

        try:
            submission = session.get(VideoSubmission, job.submission_id)
            if not submission:
                raise RuntimeError("Submission not found")
            facts = Analyzer().analyze(UPLOAD_DIR / submission.stored_filename, submission.original_filename)

            for fact in facts.detections:
                session.add(DetectionResult(job_id=job_id, **fact.__dict__))
            for fact in facts.actions:
                session.add(ActionResult(job_id=job_id, **fact.__dict__))
            for fact in facts.steps:
                session.add(StepEvent(job_id=job_id, **fact.__dict__))
            session.commit()

            score, _ = evaluate_rules(session, job_id)
            report = generate_report(session, job_id, score)
            session.add(report)

            job.score = score
            notes = f" 模型备注：{'；'.join(facts.notes)}" if facts.notes else ""
            job.summary = f"{report.conclusion}{notes}"
            job.status = AnalysisStatus.completed
            job.completed_at = datetime.utcnow()
            session.add(job)
            session.commit()
        except Exception as exc:
            job.status = AnalysisStatus.failed
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            session.add(job)
            session.commit()


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session)):
    return session.exec(select(AnalysisJob).order_by(AnalysisJob.created_at.desc())).all()


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/rerun")
def rerun_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    for model in [DetectionResult, ActionResult, StepEvent, Violation, EvaluationReport]:
        rows = session.exec(select(model).where(model.job_id == job_id)).all()
        for row in rows:
            session.delete(row)
    job.status = AnalysisStatus.pending
    job.score = 0
    job.summary = ""
    job.error_message = ""
    session.add(job)
    session.commit()
    run_analysis(job_id)
    session.refresh(job)
    return job


@router.get("/jobs/{job_id}/detail")
def get_analysis_detail(job_id: int, session: Session = Depends(get_session)):
    job = session.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    submission = session.get(VideoSubmission, job.submission_id)
    return {
        "job": job,
        "submission": submission,
        "detections": session.exec(select(DetectionResult).where(DetectionResult.job_id == job_id)).all(),
        "actions": session.exec(select(ActionResult).where(ActionResult.job_id == job_id).order_by(ActionResult.start_sec)).all(),
        "steps": session.exec(select(StepEvent).where(StepEvent.job_id == job_id).order_by(StepEvent.start_sec)).all(),
        "violations": session.exec(select(Violation).where(Violation.job_id == job_id).order_by(Violation.deduction.desc())).all(),
        "report": session.exec(select(EvaluationReport).where(EvaluationReport.job_id == job_id)).first(),
    }


@router.get("/stats/overview")
def overview(session: Session = Depends(get_session)):
    jobs = session.exec(select(AnalysisJob)).all()
    completed = [job for job in jobs if job.status == AnalysisStatus.completed]
    average = round(sum(job.score for job in completed) / len(completed), 1) if completed else 0
    return {
        "total_jobs": len(jobs),
        "completed_jobs": len(completed),
        "average_score": average,
        "excellent_count": len([job for job in completed if job.score >= 90]),
        "risk_count": len([job for job in completed if job.score < 70]),
    }
