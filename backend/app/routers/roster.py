"""名册（教师）：CSV 粘贴导入 + 批量开通学生账号。

**为什么名册是前提而不是锦上添花**：V1 里学生姓名和学号是上传表单里的自由文本，
于是必然出现"同一学号多种写法"和"错字造出幽灵学生"。一旦有了这些，
成绩对账做不了，防冒名提交也做不了——因为你根本不知道名单上应该是谁。

做法上选择"粘贴导入"而不是做一套完整的文件上传+预览界面：
教师从教务系统复制两列粘进来是最省事的路径，比让他导出 CSV 再上传还快，
而且**绕开了编码问题**——老教务系统导出 GBK 是常见坑，
但粘贴进浏览器时编码已经由浏览器处理好了。半个工作日省下来。
"""

from __future__ import annotations

import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import (
    generate_initial_password,
    hash_password,
    require_teacher,
    revoke_all_for_user,
    validate_password_strength,
)
from ..database import get_session
from ..models import RosterEntry, TrainingTask, User, UserRole
from ..services import audit

router = APIRouter(prefix="/api/roster", tags=["roster"])

# 逗号、中文逗号、制表符、多个空格都当作分隔符。
# 教师从教务系统复制出来的东西，分隔符是什么全凭运气。
_SPLIT = re.compile(r"[,，\t]+|\s{2,}")


class RosterImport(BaseModel):
    task_id: int
    text: str
    initial_password: str = ""
    create_accounts: bool = True


def _parse_lines(text: str) -> list[tuple[str, str]]:
    """解析成 (学号, 姓名)。容忍表头、空行、多余列。"""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in _SPLIT.split(line) if part.strip()]
        if len(parts) < 2:
            continue
        student_no, student_name = parts[0], parts[1]
        if student_no in seen:
            continue
        # 跳过表头。教务系统的表头五花八门，用"像不像学号"来判断比穷举表头名可靠。
        if not re.search(r"\d", student_no):
            continue
        seen.add(student_no)
        rows.append((student_no, student_name))
    return rows


@router.get("/{task_id}")
def list_roster(
    task_id: int,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(require_teacher)],
):
    entries = session.exec(
        select(RosterEntry).where(RosterEntry.task_id == task_id).order_by(RosterEntry.student_no)
    ).all()
    uploaders = {entry.student_no for entry in entries if entry.user_id}
    return {
        "entries": [
            {
                "id": entry.id,
                "student_no": entry.student_no,
                "student_name": entry.student_name,
                "bound": entry.user_id is not None,
            }
            for entry in entries
        ],
        "total": len(entries),
        "bound": len(uploaders),
    }


@router.post("/import")
def import_roster(
    payload: RosterImport,
    session: Annotated[Session, Depends(get_session)],
    teacher: Annotated[User, Depends(require_teacher)],
):
    task = session.get(TrainingTask, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")

    rows = _parse_lines(payload.text)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="没有解析出任何名册行。每行需要「学号 姓名」两列，用逗号或制表符分隔。",
        )

    initial_password = payload.initial_password.strip()
    if payload.create_accounts:
        if not initial_password:
            # 未指定时生成一个，并**只在本次响应里返回一次**。
            # 不落库、不写日志——它只应该出现在教师眼前这一秒。
            initial_password = generate_initial_password()
        validate_password_strength(initial_password)

    created_entries = 0
    created_users = 0
    reused_users = 0
    password_hash = hash_password(initial_password) if payload.create_accounts else ""

    existing_entries = {
        entry.student_no: entry
        for entry in session.exec(select(RosterEntry).where(RosterEntry.task_id == payload.task_id)).all()
    }

    for student_no, student_name in rows:
        if student_no in existing_entries:
            continue

        user_id = None
        if payload.create_accounts:
            user = session.exec(select(User).where(User.username == student_no)).first()
            if user:
                # 同一学生跨任务复用时**不重置密码**——重置会把他在别的课
                # 已经改过的密码打回去，属于不可接受的行为。
                reused_users += 1
                user_id = user.id
            else:
                user = User(
                    username=student_no,
                    password_hash=password_hash,
                    role=UserRole.student,
                    display_name=student_name,
                    student_no=student_no,
                    class_name=task.class_name,
                    must_change_password=True,
                )
                session.add(user)
                session.flush()  # 拿到自增 id
                created_users += 1
                user_id = user.id

        session.add(
            RosterEntry(
                task_id=payload.task_id,
                student_no=student_no,
                student_name=student_name,
                user_id=user_id,
            )
        )
        created_entries += 1

    session.commit()
    audit.record(
        session, action="import_roster", user=teacher, target_type="task",
        target_id=payload.task_id,
        detail=f"导入名册 {created_entries} 条，新建账号 {created_users} 个，复用 {reused_users} 个",
    )

    result = {
        "parsed": len(rows),
        "created_entries": created_entries,
        "created_users": created_users,
        "reused_users": reused_users,
    }
    if payload.create_accounts and created_users:
        # 只在本次响应里返回。教师需要当场记下并在班上公布。
        result["initial_password"] = initial_password
        result["note"] = (
            "初始密码仅在此处显示一次，请立即记录。"
            "学生首次登录会被强制修改密码。"
        )
    return result


@router.post("/{task_id}/reset-student")
def reset_student_password(
    task_id: int,
    student_no: str,
    session: Annotated[Session, Depends(get_session)],
    teacher: Annotated[User, Depends(require_teacher)],
):
    """给单个学生重置密码（忘记密码时的救济路径）。"""
    entry = session.exec(
        select(RosterEntry).where(RosterEntry.task_id == task_id, RosterEntry.student_no == student_no)
    ).first()
    if not entry or not entry.user_id:
        raise HTTPException(status_code=404, detail="名册中没有该学号，或该学生尚未开通账号。")

    user = session.get(User, entry.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="账号不存在。")

    # 和名册导入共用同一个生成器。原先这里是 f"rpo{secrets.token_hex(4)}"，
    # 当那 8 位十六进制恰好全是 a-f 时（约 0.06%），整串是纯字母，
    # 会被 validate_password_strength 拒掉——同一种 bug 的另一处副本。
    new_password = generate_initial_password()
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    session.add(user)
    session.commit()

    revoked = revoke_all_for_user(session, user.id)
    audit.record(session, action="reset_student_password", user=teacher, target_type="user",
                 target_id=user.id, detail=f"重置学生 {student_no} 密码，注销 {revoked} 个会话")
    return {"student_no": student_no, "new_password": new_password,
            "note": "此密码仅显示一次，学生首次登录需修改。"}
