from sqlmodel import Session, select

from ..models import EvaluationReport, KnowledgeDocument, StepEvent, Violation


def generate_report(session: Session, job_id: int, score: float) -> EvaluationReport:
    steps = session.exec(select(StepEvent).where(StepEvent.job_id == job_id).order_by(StepEvent.start_sec)).all()
    violations = session.exec(select(Violation).where(Violation.job_id == job_id).order_by(Violation.deduction.desc())).all()
    docs = session.exec(select(KnowledgeDocument).limit(3)).all()

    step_names = "、".join(step.step_name for step in steps) or "未识别到有效操作步骤"
    doc_titles = "、".join(doc.title for doc in docs)

    if score >= 90:
        conclusion = "本次作业流程整体规范，关键安全步骤识别完整，建议作为合格示范视频归档。"
    elif score >= 70:
        conclusion = "本次作业基本完成，但存在需要教师复核和学生整改的扣分项。"
    else:
        conclusion = "本次作业存在较明显安全或流程问题，建议重新训练并复评。"

    strengths = f"系统识别到的操作流程包括：{step_names}。评价参考知识库依据：{doc_titles}。"
    if violations:
        problems = "；".join(f"{item.title}（扣 {item.deduction:g} 分）：{item.reason}" for item in violations)
        suggestions = "；".join(item.suggestion for item in violations)
    else:
        problems = "未发现硬性规则扣分项。"
        suggestions = "保持作业前防护检查、操作中步骤口述和操作后复核的完整视频证据。"

    return EvaluationReport(
        job_id=job_id,
        score=score,
        conclusion=conclusion,
        strengths=strengths,
        problems=problems,
        suggestions=suggestions,
    )
