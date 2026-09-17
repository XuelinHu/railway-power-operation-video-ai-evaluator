"""规则评分引擎。

## 三态语义（本版本最重要的改动）

感知源从"确定性检测器"换成"会看走眼的 VLM"之后，**"看不见"和"没做"
是两件完全不同的事**：

- `not_completed` —— 明确看到该做的动作没做（如双手清晰入画但没戴手套）→ 扣分
- `not_visible`  —— 画面不足以判断（机位太远、被遮挡、动作在画外）→ **不扣分**，转人工
- `completed`    —— 有明确证据完成 → 不扣分
- `not_applicable` —— 本次作业本就不适用 → 不扣分

V1 的 `step_required` 是"查无此步即扣分"。在旧链路里这还算合理，
因为检测器不会"看不清"；但在 VLM 链路上，机位拍不到是常态，
沿用旧语义会**因为拍摄条件而扣学生的分**——这是不能接受的。

## 为什么规则不再做标签集合差

V1 的防护用品判定是 `PPE_LABELS - detected_labels`。这是个陷阱：
模型返回 `"安全帽"` 或 `"helmet"` 时集合差永远非空 → 全员扣 15 分；
返回复合串时集合差为空 → 全员满分。两种都不报错。

现在改为**消费步骤判定**：VLM 的 Pass 2 已经在观察表的基础上推理过
"至少一帧明确看到佩戴即算完成"，规则引擎直接采信它的结论并负责算分。
职责因此变得干净：模型只出事实与判定，规则引擎只算分。

## 时间只来自帧序号

sequence 规则依赖 `start_sec` 的先后比较。这里的 `start_sec` 由
provider 从**帧号 × 帧间隔**算出，模型输出的任何时间戳都被丢弃——
模型从静帧估时间的能力基本为零，一次错序就是一次 25 分的误判。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from ..models import ScoringRule, StepEvent, Violation, ViolationStatus
from .ai.labels import STEP_NAMES

# 不参与顺序比较的判定：它们要么没有时间，要么"没做"本就不该拿来比先后。
_UNORDERED_VERDICTS = {"not_visible", "not_applicable", "not_completed"}


@dataclass
class RuleEvaluation:
    """评分结果。

    `score` 为 None 表示**没有足够证据给出分数**——不是 0 分，也不是 100 分。
    不变量 #1：没有证据支撑时分数必须是空。
    """

    score: float | None
    violations: list[Violation] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    visible_steps: list[str] = field(default_factory=list)
    invisible_steps: list[str] = field(default_factory=list)


def _display(step_code: str) -> str:
    return STEP_NAMES.get(step_code, step_code)


def evaluate_rules(session: Session, job_id: int) -> RuleEvaluation:
    rules = session.exec(select(ScoringRule).order_by(ScoringRule.id)).all()
    steps = session.exec(select(StepEvent).where(StepEvent.job_id == job_id)).all()

    # 人工改判优先于机器判定：教师修正过的步骤以 source="human" 的为准。
    step_by_code: dict[str, StepEvent] = {}
    for step in steps:
        existing = step_by_code.get(step.step_code)
        if existing is None or (step.source == "human" and existing.source != "human"):
            step_by_code[step.step_code] = step

    evaluation = RuleEvaluation(score=None)

    for rule in rules:
        violation = _evaluate_rule(rule, step_by_code, job_id, evaluation)
        if violation is not None:
            session.add(violation)
            evaluation.violations.append(violation)

    _finalize_score(evaluation, step_by_code)
    return evaluation


def _evaluate_rule(
    rule: ScoringRule,
    step_by_code: dict[str, StepEvent],
    job_id: int,
    evaluation: RuleEvaluation,
) -> Violation | None:
    if rule.rule_type in {"step_required", "detection_required"}:
        return _evaluate_required(rule, step_by_code, job_id, evaluation)
    if rule.rule_type == "sequence":
        return _evaluate_sequence(rule, step_by_code, job_id)
    return None


def _evaluate_required(
    rule: ScoringRule,
    step_by_code: dict[str, StepEvent],
    job_id: int,
    evaluation: RuleEvaluation,
) -> Violation | None:
    step = step_by_code.get(rule.target_code)

    if step is None:
        # 理论上不该发生：provider 总会产出全部 9 步。
        # 真发生了说明上游有 bug，此时**不能当成"没做"扣分**——
        # 那会让学生为系统的错误买单。转人工。
        evaluation.needs_review = True
        evaluation.review_reasons.append(f"缺少「{_display(rule.target_code)}」的判定记录")
        return None

    if step.verdict == "not_completed":
        return Violation(
            job_id=job_id,
            rule_code=rule.code,
            title=rule.title,
            deduction=rule.deduction,
            severity=rule.severity,
            timestamp_sec=step.start_sec,
            reason=step.evidence or f"画面证据显示「{step.step_name}」未按要求完成。",
            suggestion=rule.description,
            status=ViolationStatus.auto,
        )

    if step.verdict == "not_visible":
        # 核心语义：看不见 ≠ 没做。不扣分，但必须让教师知道这一步没被验证过。
        evaluation.needs_review = True
        evaluation.review_reasons.append(
            f"「{step.step_name}」在视频中不可见，未自动扣分，需教师人工确认"
        )
        return None

    return None


def _evaluate_sequence(
    rule: ScoringRule,
    step_by_code: dict[str, StepEvent],
    job_id: int,
) -> Violation | None:
    try:
        before_code, after_code = rule.target_code.split(">", 1)
    except ValueError:
        return None

    before = step_by_code.get(before_code)
    after = step_by_code.get(after_code)
    if not before or not after:
        return None

    # 只有两边都是"明确完成"且都有时间时，顺序比较才有意义。
    # 任何一边不可见/未完成，先后关系就无从谈起——强行比较就是在
    # 拿 None 比大小，或者拿"没做的事"的时间去比。
    if before.verdict in _UNORDERED_VERDICTS or after.verdict in _UNORDERED_VERDICTS:
        return None
    if before.start_sec is None or after.start_sec is None:
        return None

    if before.start_sec > after.start_sec:
        return Violation(
            job_id=job_id,
            rule_code=rule.code,
            title=rule.title,
            deduction=rule.deduction,
            severity=rule.severity,
            timestamp_sec=after.start_sec,
            reason=f"识别到「{after.step_name}」发生在「{before.step_name}」之前，不符合操作顺序。",
            suggestion=rule.description,
            status=ViolationStatus.auto,
        )
    return None


def _finalize_score(evaluation: RuleEvaluation, step_by_code: dict[str, StepEvent]) -> None:
    for code in STEP_NAMES:
        step = step_by_code.get(code)
        if step is not None and step.verdict in {"completed", "not_completed"}:
            evaluation.visible_steps.append(_display(code))
        else:
            evaluation.invisible_steps.append(_display(code))

    # 全部步骤都不可见 → 没有证据，不给分数。
    # 若照常算分就会得出 100（无扣分项），而"没拍到"被读成"做得很好"。
    if not evaluation.visible_steps:
        evaluation.score = None
        evaluation.needs_review = True
        evaluation.review_reasons.append("所有步骤在视频中均不可见，无法自动评分")
        return

    evaluation.score = max(0.0, 100.0 - sum(item.deduction for item in evaluation.violations))

    if evaluation.invisible_steps:
        evaluation.needs_review = True
        names = "、".join(evaluation.invisible_steps)
        evaluation.review_reasons.append(
            f"以下步骤不可见，本次分数仅基于其余 {len(evaluation.visible_steps)} 步：{names}"
        )


def confirmed_violations(session: Session, job_id: int) -> list[Violation]:
    """参与终分的扣分项：教师确认的，加上教师尚未处理的。

    被教师驳回（误判）的不算——那正是复核机制存在的意义。
    """
    rows = session.exec(select(Violation).where(Violation.job_id == job_id)).all()
    return [row for row in rows if row.status != ViolationStatus.dismissed]


def final_score_for(session: Session, job_id: int) -> float | None:
    """按当前生效的扣分项计算最终分数。

    教师改判后重算走这里，保证"终分"永远等于"100 减去生效的扣分"，
    不会因为反复改判而漂移。
    """
    violations = confirmed_violations(session, job_id)
    steps = session.exec(select(StepEvent).where(StepEvent.job_id == job_id)).all()
    if not any(step.verdict in {"completed", "not_completed"} for step in steps):
        return None
    return max(0.0, 100.0 - sum(item.deduction for item in violations))


__all__ = [
    "RuleEvaluation",
    "evaluate_rules",
    "confirmed_violations",
    "final_score_for",
]
