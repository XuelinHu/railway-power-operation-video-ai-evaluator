"""登录 / 登出 / 当前用户 / 改密。"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Cookie, Depends, Response, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import (
    AuthError,
    current_user,
    hash_password,
    issue_session,
    revoke_all_for_user,
    revoke_session,
    validate_password_strength,
    verify_password,
)
from ..clock import now
from ..config import settings
from ..database import get_session
from ..models import User, UserRole
from ..services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginPayload(BaseModel):
    username: str
    password: str


class ChangePasswordPayload(BaseModel):
    old_password: str
    new_password: str


def _public(user: User) -> dict:
    """投给前端的用户信息。**绝不包含 password_hash**。"""
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value if isinstance(user.role, UserRole) else user.role,
        "display_name": user.display_name,
        "student_no": user.student_no,
        "class_name": user.class_name,
        "must_change_password": user.must_change_password,
    }


@router.post("/login")
def login(
    payload: LoginPayload,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
):
    username = payload.username.strip()
    user = session.exec(select(User).where(User.username == username)).first()

    # 统一的失败文案：不区分"用户不存在"和"密码错误"。
    # 区分开来等于免费提供一个账号枚举接口。
    generic = "用户名或密码错误。"
    if not user or not verify_password(payload.password, user.password_hash):
        raise AuthError(generic, code=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        raise AuthError("该账号已被停用，请联系管理员。", code=status.HTTP_403_FORBIDDEN)

    user.last_login_at = now()
    session.add(user)
    session.commit()
    session.refresh(user)

    issue_session(session, user, response)
    audit.record(session, action="login", user=user, target_type="user", target_id=user.id)
    return {"user": _public(user)}


@router.post("/logout")
def logout(
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    token: Annotated[Optional[str], Cookie(alias=settings.cookie_name)] = None,
):
    if token:
        revoke_session(session, token)
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: Annotated[User, Depends(current_user)]):
    return _public(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordPayload,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(current_user)],
    token: Annotated[Optional[str], Cookie(alias=settings.cookie_name)] = None,
):
    """改密。**这个接口不检查 must_change_password 拦截**——它正是用来解除该状态的。"""
    if not verify_password(payload.old_password, user.password_hash):
        raise AuthError("原密码不正确。", code=status.HTTP_400_BAD_REQUEST)
    validate_password_strength(payload.new_password)
    if payload.old_password == payload.new_password:
        raise AuthError("新密码不能与原密码相同。", code=status.HTTP_400_BAD_REQUEST)

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    session.add(user)
    session.commit()

    # 改密后踢掉该账号的**其它**会话，但保住当前这一个。
    # 不留当前会话的话，用户改完密码立刻被登出——而每个学生首次登录
    # 都会被强制改密，等于把新用户的第一印象做成"这系统有 bug"。
    revoked = revoke_all_for_user(session, user.id, except_token=token)
    audit.record(
        session,
        action="change_password",
        user=user,
        target_type="user",
        target_id=user.id,
        detail=f"改密成功，已注销该账号其它 {revoked} 个会话",
    )
    return {"ok": True}
