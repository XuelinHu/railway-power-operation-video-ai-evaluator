"""审计留痕。

改分直接决定学生成绩，所以每次改分都必须能回答四个问题：
**谁、什么时候、把什么、从多少改成了多少**。缺任何一个，
一次误判就会变成一场说不清的纠纷。

这里刻意做成"调用方传 before/after"而不是自动 diff：
自动 diff 需要反射整个模型，容易漏字段且难以读懂；
显式传反而逼调用方想清楚"这次改动到底改变了什么"。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from sqlmodel import Session

from ..models import AuditLog, User


def record(
    session: Session,
    *,
    action: str,
    user: Optional[User] = None,
    target_type: str = "",
    target_id: Optional[int] = None,
    detail: Any = "",
    commit: bool = True,
) -> AuditLog:
    """写一条审计记录。

    `commit=False` 供"与业务改动同一个事务"的场景使用——
    审计和业务改动必须同生共死，否则会出现"分改了但没留痕"。
    """
    entry = AuditLog(
        user_id=user.id if user else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail if isinstance(detail, str) else str(detail),
    )
    session.add(entry)
    if commit:
        session.commit()
        session.refresh(entry)
    return entry


def changes(before: Any, after: Any) -> str:
    """把前后值渲染成给**人**看的 `旧 → 新`。

    审计记录是给教师和教务看的，不是给程序看的。
    `{'status': <ViolationStatus.auto: 'auto'>}` 这种 repr 出现在
    一份要拿去对质的记录里毫无意义，所以这里做可读化渲染。
    """
    if isinstance(before, dict) or isinstance(after, dict):
        old = before if isinstance(before, dict) else {}
        new = after if isinstance(after, dict) else {}
        parts = [
            f"{key}: {_render(old.get(key))} → {_render(new.get(key))}"
            for key in dict.fromkeys([*old, *new])
            if old.get(key) != new.get(key)
        ]
        return "；".join(parts) or "无变化"
    return f"{_render(before)} → {_render(after)}"


def _render(value: Any) -> str:
    if value is None or value == "":
        return "空"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)
