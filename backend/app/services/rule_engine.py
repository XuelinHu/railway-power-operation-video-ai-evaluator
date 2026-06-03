from sqlmodel import Session, select

from ..models import DetectionResult, ScoringRule, StepEvent, Violation


PPE_LABELS = {"safety_helmet", "insulating_gloves"}


def evaluate_rules(session: Session, job_id: int) -> tuple[float, list[Violation]]:
    rules = session.exec(select(ScoringRule).order_by(ScoringRule.id)).all()
    steps = session.exec(select(StepEvent).where(StepEvent.job_id == job_id)).all()
    detections = session.exec(select(DetectionResult).where(DetectionResult.job_id == job_id)).all()

    step_by_code = {step.step_code: step for step in steps}
    detected_labels = {item.label for item in detections}
    violations: list[Violation] = []

    for rule in rules:
        violation = None
        if rule.rule_type == "detection_required" and rule.target_code == "ppe":
            missing = sorted(PPE_LABELS - detected_labels)
            if missing:
                violation = Violation(
                    job_id=job_id,
                    rule_code=rule.code,
                    title=rule.title,
                    deduction=rule.deduction,
                    severity=rule.severity,
                    timestamp_sec=0,
                    reason=f"视频证据未完整识别到防护用品：{', '.join(missing)}。",
                    suggestion="进入作业区域前完成安全帽、绝缘手套、绝缘靴等防护用品检查，并在视频中清晰展示。",
                )
        elif rule.rule_type == "step_required":
            if rule.target_code not in step_by_code:
                violation = Violation(
                    job_id=job_id,
                    rule_code=rule.code,
                    title=rule.title,
                    deduction=rule.deduction,
                    severity=rule.severity,
                    timestamp_sec=None,
                    reason=f"未识别到必需步骤：{rule.description}",
                    suggestion="按标准流程补充该步骤，并保证动作完整、可见、可复核。",
                )
        elif rule.rule_type == "sequence":
            before_code, after_code = rule.target_code.split(">", 1)
            before = step_by_code.get(before_code)
            after = step_by_code.get(after_code)
            if before and after and before.start_sec > after.start_sec:
                violation = Violation(
                    job_id=job_id,
                    rule_code=rule.code,
                    title=rule.title,
                    deduction=rule.deduction,
                    severity=rule.severity,
                    timestamp_sec=after.start_sec,
                    reason=f"识别到“{after.step_name}”发生在“{before.step_name}”之前，不符合操作顺序。",
                    suggestion=rule.description,
                )

        if violation:
            session.add(violation)
            violations.append(violation)

    score = max(0.0, 100.0 - sum(item.deduction for item in violations))
    return score, violations
