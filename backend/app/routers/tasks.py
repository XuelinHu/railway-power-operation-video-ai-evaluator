from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import TrainingTask

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    title: str
    course: str
    class_name: str
    teacher: str
    description: str = ""


@router.get("")
def list_tasks(session: Session = Depends(get_session)):
    return session.exec(select(TrainingTask).order_by(TrainingTask.created_at.desc())).all()


@router.post("")
def create_task(payload: TaskCreate, session: Session = Depends(get_session)):
    task = TrainingTask(**payload.model_dump())
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.get("/{task_id}")
def get_task(task_id: int, session: Session = Depends(get_session)):
    task = session.get(TrainingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
