"""账号管理（管理员）。

学生账号的来源是名册批量开通，不在这里逐个建——见 roster.py。
这里管的是教师/管理员账号，以及停用、重置密码、强制下线。
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import hash_password, require_admin, revoke_all_for_user, validate_password_strength
from ..database import get_session
from ..models import User, UserRole
from ..services import audit

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.teacher
    display_name: str = ""
    class_name: str = ""


class PasswordReset(BaseModel):
    new_password: str


def _public(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value if isinstance(user.role, UserRole) else user.role,
        "display_name": user.display_name,
        "student_no": user.student_no,
        "class_name": user.class_name,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at,
    }


@router.get("")
def list_users(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(require_admin)],
    role: Optional[UserRole] = None,
):
    query = select(User).order_by(User.id)
    if role:
        query = select(User).where(User.role == role).order_by(User.id)
    return [_public(user) for user in session.exec(query).all()]


@router.post("")
def create_user(
    payload: UserCreate,
    session: Annotated[Session, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空。")
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(status_code=409, detail=f"用户名 {username} 已存在。")

    validate_password_strength(payload.password)
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name or username,
        class_name=payload.class_name,
        must_change_password=True,  # 管理员知道初始密码 → 本人首登必须改
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    audit.record(session, action="create_user", user=admin, target_type="user",
                 target_id=user.id, detail=f"新建 {payload.role.value} 账号 {username}")
    return _public(user)


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: PasswordReset,
    session: Annotated[Session, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="账号不存在。")

    validate_password_strength(payload.new_password)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    session.add(user)
    session.commit()

    revoked = revoke_all_for_user(session, user.id)
    audit.record(session, action="reset_password", user=admin, target_type="user",
                 target_id=user.id, detail=f"重置 {user.username} 的密码，注销 {revoked} 个会话")
    return {"ok": True, "revoked_sessions": revoked}


class ActiveToggle(BaseModel):
    is_active: bool


@router.post("/{user_id}/active")
def set_active(
    user_id: int,
    payload: ActiveToggle,
    session: Annotated[Session, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    """停用/启用账号。停用会**立即**注销该账号的所有会话。

    这正是选服务端会话而不是 JWT 的理由：JWT 在过期前撤销不了，
    而"停用必须立刻生效"是学校的硬需求。
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="账号不存在。")
    if user.id == admin.id and not payload.is_active:
        raise HTTPException(status_code=400, detail="不能停用自己的账号。")

    user.is_active = payload.is_active
    session.add(user)
    session.commit()

    revoked = revoke_all_for_user(session, user.id) if not payload.is_active else 0
    audit.record(session, action="toggle_active", user=admin, target_type="user",
                 target_id=user.id,
                 detail=f"{user.username} 设为 {'启用' if payload.is_active else '停用'}，注销 {revoked} 个会话")
    return {"ok": True, "revoked_sessions": revoked}
