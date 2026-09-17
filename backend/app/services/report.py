"""评价报告的生成。

报告是**给学生和教师看的中文叙述**，不是数据转储。所以这里的措辞标准是
"一个不懂技术的教师读一遍就知道该让学生改什么"。

## 报告必须诚实地说出自己不知道什么

V1 的报告在无扣分项时写"未发现硬性规则扣分项"，而当**所有步骤都不可见**时
它也会这么写——因为确实没有扣分项。学生看到的是"没发现问题"，
而事实是"什么都没看到"。这两件事在报告里必须长得不一样。

所以这里接受完整的 `RuleEvaluation` 而不是一个分数，
因为"分数是多少"和"这个分数有多可信"是两件事，后者才是报告的价值所在。
"""

from sqlmodel import Session, select

from ..models import EvaluationReport, KnowledgeDocument, StepEvent, Violation
from .rule_engine import RuleEvaluation


def generate_report(
    session: Session, job_id: int, evaluation: RuleEvaluation
) -> EvaluationReport:
    steps = session.exec(
        select(StepEvent).where(StepEvent.job_id == job_id).order_by(StepEvent.start_sec)
    ).all()
    violations = session.exec(
        select(Violation).where(Violation.job_id == job_id).order_by(Violation.deduction.desc())
    ).all()
    docs = session.exec(select(KnowledgeDocument).limit(3)).all()

    done = [step for step in steps if step.verdict == "completed"]
    missed = [step for step in steps if step.verdict == "not_completed"]
    invisible = [step for step in steps if step.verdict == "not_visible"]

    doc_titles = "、".join(doc.title for doc in docs) or "知识库暂无条目"

    conclusion = _conclusion(evaluation, done, missed, invisible)

    if done:
        strengths = "识别到已完成的步骤：" + "、".join(step.step_name for step in done) + "。"
    else:
        strengths = "本次未能确认任何步骤完成。"
    strengths += f" 评价依据参考知识库：{doc_titles}。"

    if violations:
        problems = "；".join(
            f"{item.title}（扣 {item.deduction:g} 分）：{item.reason}" for item in violations
        )
    elif missed:
        problems = "识别到未完成的步骤：" + "、".join(step.step_name for step in missed) + "。"
    else:
        problems = "未发现明确的违规项。"

    suggestions = _suggestions(violations, invisible)

    return EvaluationReport(
        job_id=job_id,
        score=evaluation.score,
        conclusion=conclusion,
        strengths=strengths,
        problems=problems,
        suggestions=suggestions,
    )


def _conclusion(
    evaluation: RuleEvaluation,
    done: list[StepEvent],
    missed: list[StepEvent],
    invisible: list[StepEvent],
) -> str:
    if evaluation.score is None:
        # 没有分数时**绝不写"合格"**。V1 在这里会写"未发现扣分项"，
        # 而学生读到的是"我做对了"。
        return (
            "本次视频未能提供足够的可判断画面，系统无法给出评分。"
            "这不代表操作有问题，也不代表操作正确，需要教师人工评阅。"
        )

    if invisible:
        # 部分不可见时，结论必须带上"这个分数只覆盖了一部分步骤"，
        # 否则 95 分会被读成"全流程规范"，而实际上有三步根本没拍到。
        return (
            f"依据视频中可见的 {len(done) + len(missed)} 个步骤，"
            f"本次作业得分 {evaluation.score:.0f} 分。"
            f"另有 {len(invisible)} 个步骤画面不可见，未纳入评分，需教师人工确认。"
        )

    if evaluation.score >= 90:
        return f"本次作业流程整体规范，得分 {evaluation.score:.0f} 分。"
    if evaluation.score >= 70:
        return f"本次作业基本完成，得分 {evaluation.score:.0f} 分，存在需要整改的扣分项。"
    return f"本次作业存在较明显安全或流程问题，得分 {evaluation.score:.0f} 分，建议重新训练后复评。"


def _suggestions(violations: list[Violation], invisible: list[StepEvent]) -> str:
    parts = [item.suggestion for item in violations if item.suggestion]
    if invisible:
        names = "、".join(step.step_name for step in invisible)
        parts.append(
            f"以下步骤本次未被拍到，无法自动评价：{names}。"
            "建议下次拍摄时调整机位，让操作全过程（尤其是手部动作）保持在画面内。"
        )
    if not parts:
        parts.append("保持作业前防护检查、操作中步骤口述、操作后复核的完整视频记录。")
    return "；".join(parts)
