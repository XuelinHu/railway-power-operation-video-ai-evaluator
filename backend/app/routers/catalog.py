from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..database import get_session
from ..models import KnowledgeDocument, ScoringRule, StandardStep

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/steps")
def list_steps(session: Session = Depends(get_session)):
    return session.exec(select(StandardStep).order_by(StandardStep.order_index)).all()


@router.get("/rules")
def list_rules(session: Session = Depends(get_session)):
    return session.exec(select(ScoringRule).order_by(ScoringRule.id)).all()


@router.get("/knowledge")
def list_knowledge(session: Session = Depends(get_session)):
    return session.exec(select(KnowledgeDocument).order_by(KnowledgeDocument.id)).all()
