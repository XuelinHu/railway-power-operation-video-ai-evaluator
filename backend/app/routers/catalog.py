"""标准步骤、评分规则、知识库：只读的参考数据。

三个接口都要登录才能看。它们本身不含学生数据，但**未登录能读规则表**
意味着外人可以据此反推出评分逻辑，进而知道怎么"刷分"——
标准步骤和扣分项是教学设计的一部分，不该对未登录访客开放。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..auth import current_user
from ..database import get_session
from ..models import KnowledgeDocument, ScoringRule, StandardStep, User

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/steps")
def list_steps(
    _: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    return session.exec(select(StandardStep).order_by(StandardStep.order_index)).all()


@router.get("/rules")
def list_rules(
    _: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    return session.exec(select(ScoringRule).order_by(ScoringRule.id)).all()


@router.get("/knowledge")
def list_knowledge(
    _: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    return session.exec(select(KnowledgeDocument).order_by(KnowledgeDocument.id)).all()
