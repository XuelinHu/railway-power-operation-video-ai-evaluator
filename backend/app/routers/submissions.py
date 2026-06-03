from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..database import UPLOAD_DIR, get_session
from ..models import AnalysisJob, VideoSubmission
from .analysis import run_analysis

router = APIRouter(prefix="/api/submissions", tags=["submissions"])


@router.get("")
def list_submissions(task_id: int | None = None, session: Session = Depends(get_session)):
    query = select(VideoSubmission).order_by(VideoSubmission.uploaded_at.desc())
    if task_id:
        query = select(VideoSubmission).where(VideoSubmission.task_id == task_id).order_by(VideoSubmission.uploaded_at.desc())
    return session.exec(query).all()


@router.post("")
async def upload_submission(
    background_tasks: BackgroundTasks,
    task_id: int = Form(...),
    student_name: str = Form(...),
    student_no: str = Form(""),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    stored_filename = f"{uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_filename
    content = await file.read()
    target.write_bytes(content)

    submission = VideoSubmission(
        task_id=task_id,
        student_name=student_name,
        student_no=student_no,
        original_filename=file.filename or stored_filename,
        stored_filename=stored_filename,
        content_type=file.content_type or "",
        size_bytes=len(content),
    )
    session.add(submission)
    session.commit()
    session.refresh(submission)

    job = AnalysisJob(submission_id=submission.id)
    session.add(job)
    session.commit()
    session.refresh(job)
    background_tasks.add_task(run_analysis, job.id)

    return {"submission": submission, "job": job}


@router.get("/{submission_id}/video")
def get_video(submission_id: int, session: Session = Depends(get_session)):
    submission = session.get(VideoSubmission, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    path = UPLOAD_DIR / submission.stored_filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(path, media_type=submission.content_type or "video/mp4", filename=submission.original_filename)
