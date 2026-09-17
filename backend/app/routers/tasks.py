"""作业任务（教师建、学生做）。

权限边界很简单但要紧：**建任务和改任务只有教师能做，看任务所有人能看**。
学生必须能看到任务列表才知道有什么作业要交；但任务本身不含敏感数据，
不需要按人过滤。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import current_user, require_teacher
from ..database import get_session
from ..models import AnalysisJob, RosterEntry, TaskStatus, TrainingTask, User, VideoSubmission
from ..services import audit

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    title: str
    course: str
    class_name: str
    description: str = ""
    due_at: Optional[datetime] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    course: Optional[str] = None
    class_name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    due_at: Optional[datetime] = None


def _task_view(session: Session, task: TrainingTask, user: User) -> dict:
    roster_total = len(
        session.exec(select(RosterEntry.id).where(RosterEntry.task_id == task.id)).all()
    )
    submitted = len(
        session.exec(
            select(VideoSubmission.id).where(VideoSubmission.task_id == task.id)
        ).all()
    )
    view = {
        "id": task.id,
        "title": task.title,
        "course": task.course,
        "class_name": task.class_name,
        "teacher": task.teacher,
        "description": task.description,
        "status": task.status,
        "due_at": task.due_at,
        "created_at": task.created_at,
        "roster_total": roster_total,
        "submitted_count": submitted,
    }

    if user.role.value == "student":
        # 学生额外拿到"我交了没有"——前端据此决定显示"上传作业"还是"查看报告"。
        # 这个字段必须按当前用户算，不能复用教师看到的汇总。
        entry = session.exec(
            select(RosterEntry).where(
                RosterEntry.task_id == task.id, RosterEntry.user_id == user.id
            )
        ).first()
        view["in_roster"] = entry is not None
        view["my_submission_id"] = None
        if entry is not None:
            mine = session.exec(
                select(VideoSubmission).where(VideoSubmission.roster_entry_id == entry.id)
            ).first()
            if mine is not None:
                view["my_submission_id"] = mine.id
                job = session.exec(
                    select(AnalysisJob).where(AnalysisJob.submission_id == mine.id)
                ).first()
                view["my_job_status"] = job.status if job else None
    return view


@router.get("")
def list_tasks(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    tasks = session.exec(select(TrainingTask).order_by(TrainingTask.created_at.desc())).all()
    return [_task_view(session, task, user) for task in tasks]


@router.post("")
def create_task(
    payload: TaskCreate,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="作业标题不能为空。")

    task = TrainingTask(
        title=title,
        course=payload.course.strip(),
        class_name=payload.class_name.strip(),
        # 任课教师以**当前登录账号**为准，不采信表单字段——
        # V1 是自由文本，于是任务归属可以随便填。
        teacher=teacher.display_name or teacher.username,
        description=payload.description.strip(),
        owner_user_id=teacher.id,
        due_at=payload.due_at,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    audit.record(
        session, action="create_task", user=teacher, target_type="task",
        target_id=task.id, detail=f"新建作业「{title}」（{task.class_name}）",
    )
    return _task_view(session, task, teacher)


@router.get("/{task_id}")
def get_task(
    task_id: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    task = session.get(TrainingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="作业任务不存在。")
    return _task_view(session, task, user)


@router.patch("/{task_id}")
def update_task(
    task_id: int,
    payload: TaskUpdate,
    teacher: Annotated[User, Depends(require_teacher)],
    session: Annotated[Session, Depends(get_session)],
):
    task = session.get(TrainingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="作业任务不存在。")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    before = {key: getattr(task, key) for key in changes}
    for key, value in changes.items():
        setattr(task, key, value)
    session.add(task)
    session.commit()

    audit.record(
        session, action="update_task", user=teacher, target_type="task",
        target_id=task_id, detail=audit.changes(before, changes),
    )
    return _task_view(session, task, teacher)
