"""两段式 VLM 的结构契约与**确定性校验**。

这里承担的是反幻觉的硬约束。prompt 里的请求（"请务必如实标注"）只是软约束，
模型可以不遵守；本文件的校验是硬的，模型绕不过去。

三条硬规则：

1. **帧号必须真实存在**。模型引用一个没给过它的帧号 = 幻觉，必须处理。
2. **肯定性判定必须有证据帧**。`completed` / `not_completed` 都是对视频内容的
   肯定断言（"做了" / "明确没做"），必须挂上支撑它的帧。空口断言的代价是
   学生被无故扣分，这是不可接受的。
3. **词表外的值一律不放行**。见 labels.py 的说明——集合差会静默地让全员满分
   或全员扣分。

校验失败的**分级**很重要：结构不合法（不是 JSON、缺字段）才值得重试；
局部越界（某个步骤引用了不存在的帧）是局部问题，降级该步骤即可，
不要因为一处瑕疵让整个视频作废——那会浪费掉已经付过费的 token。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .labels import (
    CAMERA_QUALITY,
    STEP_CODES,
    STEP_NAMES,
    TRISTATE,
    VERDICTS,
    LabelValidationError,
)

# 说明：这里用 ignore 而不是 forbid。
# forbid 会把"模型多返回了一个无用字段"升级成整段视频作废，
# 而多出来的字段对我们没有任何危害。schema 漂移应当被**记录**（见 extras_seen），
# 而不是被用来烧掉一次已经付费的调用。
_STRICT_BUT_TOLERANT = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# Pass 1：感知
# --------------------------------------------------------------------------


class FrameObservation(BaseModel):
    """单帧的客观观察。注意这里全是事实，没有任何"是否规范"的判断。"""

    model_config = _STRICT_BUT_TOLERANT

    frame_index: int
    # 逐帧人数是必要的：0 人的帧意味着"这一刻画面里没人"，
    # 这是判定 not_visible 的重要依据，也常用于识别空镜头的机位问题。
    person_count: int = 0
    person_fully_in_frame: Literal["yes", "no", "unclear"] = "unclear"
    helmet: Literal["yes", "no", "unclear"] = "unclear"
    insulating_gloves: Literal["yes", "no", "unclear"] = "unclear"
    insulating_boots: Literal["yes", "no", "unclear"] = "unclear"
    holding: str = ""
    action: str = ""
    note: str = ""


class WindowObservation(BaseModel):
    model_config = _STRICT_BUT_TOLERANT

    window_index: int
    camera_quality: Literal["usable", "partially_obscured", "unusable"] = "usable"
    subject_count: int = 0
    frames: list[FrameObservation] = Field(default_factory=list)
    window_note: str = ""


# --------------------------------------------------------------------------
# Pass 2：判定
# --------------------------------------------------------------------------


class StepVerdict(BaseModel):
    model_config = _STRICT_BUT_TOLERANT

    step_code: str
    verdict: Literal["completed", "not_completed", "not_visible", "not_applicable"]
    confidence: float = 0.5
    evidence_frames: list[int] = Field(default_factory=list)
    reason: str = ""


class VideoJudgment(BaseModel):
    model_config = _STRICT_BUT_TOLERANT

    video_quality: Literal["usable", "partially_obscured", "unusable"] = "usable"
    scene_summary: str = ""
    steps: list[StepVerdict] = Field(default_factory=list)
    overall_note: str = ""


# --------------------------------------------------------------------------
# 校验结果
# --------------------------------------------------------------------------


class ValidatedStep(BaseModel):
    """校验后的步骤判定。校验动过的地方全部记录在 validation_notes 里。"""

    step_code: str
    step_name: str
    verdict: str
    confidence: float
    evidence_frames: list[int]
    reason: str
    validation_notes: list[str] = Field(default_factory=list)
    needs_review: bool = False


class JudgmentOutcome(BaseModel):
    """一次完整分析的判定结果。score 不在这里——分数永远由规则引擎算。"""

    video_quality: str
    scene_summary: str
    overall_note: str
    steps: list[ValidatedStep]
    # 数据质量信号：不是错误，但教师应当知道
    warnings: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class ValidationError_(ValueError):
    """结构不合法，值得重试一次。"""


def _as_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError_(f"模型返回的不是 JSON 对象，而是 {type(payload).__name__}")
    return payload


def parse_window(payload: Any, *, expected_window: int, allowed_frames: set[int]) -> WindowObservation:
    """解析并校验 Pass 1 的单个时间窗。

    越界的帧**直接丢弃**而不是报错——一帧的编号错误不该毁掉整窗观察，
    但也不能留着污染后续判定。
    """
    data = _as_dict(payload)
    window = WindowObservation.model_validate(data)

    kept: list[FrameObservation] = []
    dropped: list[int] = []
    for frame in window.frames:
        if frame.frame_index in allowed_frames:
            kept.append(frame)
        else:
            dropped.append(frame.frame_index)

    window.frames = kept
    if dropped:
        window.window_note = (
            f"{window.window_note}（已丢弃 {len(dropped)} 个不存在的帧号：{dropped}）"
        ).strip()
    return window


def validate_judgment(
    payload: Any, *, allowed_frames: set[int], review_confidence: float
) -> JudgmentOutcome:
    """校验 Pass 2 的判定。这是反幻觉的最后一道闸门。

    处理策略（按严重度递增）：
    - 步骤码不在词表 → 丢弃该步骤（记 warning），不整单失败
    - 缺步骤 → 补成 not_visible + needs_review（沉默的缺失比错误的判定更危险）
    - 帧号越界 → 从证据里剔除并记录；剔除后若肯定性判定没有证据了，降级为 not_visible
    - 置信度低 → needs_review
    """
    data = _as_dict(payload)
    judgment = VideoJudgment.model_validate(data)

    warnings: list[str] = []
    review_reasons: list[str] = []
    by_code: dict[str, ValidatedStep] = {}

    for raw in judgment.steps:
        if raw.step_code not in STEP_CODES:
            warnings.append(f"模型返回了未知步骤码 {raw.step_code!r}，已忽略。")
            continue
        if raw.step_code in by_code:
            warnings.append(f"步骤 {raw.step_code} 被返回了多次，只保留第一条。")
            continue
        by_code[raw.step_code] = _validate_one(
            raw, allowed_frames=allowed_frames, review_confidence=review_confidence,
            review_reasons=review_reasons,
        )

    # 补齐缺失的步骤。**不能沉默地跳过**：少一步就等于那一步没被判过，
    # 而规则引擎的 step_required 会把它当成"没做"直接扣分。
    for code in STEP_CODES:
        if code not in by_code:
            warnings.append(f"模型未对步骤「{STEP_NAMES[code]}」给出判定，已按不可见处理。")
            review_reasons.append(f"步骤「{STEP_NAMES[code]}」模型未给判定")
            by_code[code] = ValidatedStep(
                step_code=code,
                step_name=STEP_NAMES[code],
                verdict="not_visible",
                confidence=0.0,
                evidence_frames=[],
                reason="模型未返回该步骤的判定。",
                validation_notes=["模型遗漏，已按 not_visible 补齐"],
                needs_review=True,
            )

    steps = [by_code[code] for code in STEP_CODES]

    # 整片级别的复核信号
    if judgment.video_quality == "unusable":
        review_reasons.append("画面质量判为不可用")
    elif judgment.video_quality == "partially_obscured":
        warnings.append("画面存在遮挡，部分步骤可能无法判读。")

    if all(step.verdict in {"not_visible", "not_applicable"} for step in steps):
        review_reasons.append("全部步骤均不可见，无法自动评分")

    return JudgmentOutcome(
        video_quality=judgment.video_quality,
        scene_summary=judgment.scene_summary,
        overall_note=judgment.overall_note,
        steps=steps,
        warnings=warnings,
        needs_review=bool(review_reasons),
        review_reasons=review_reasons,
    )


def _validate_one(
    raw: StepVerdict,
    *,
    allowed_frames: set[int],
    review_confidence: float,
    review_reasons: list[str],
) -> ValidatedStep:
    notes: list[str] = []
    name = STEP_NAMES[raw.step_code]

    valid_frames = [index for index in raw.evidence_frames if index in allowed_frames]
    bogus = [index for index in raw.evidence_frames if index not in allowed_frames]
    if bogus:
        # 引用不存在的帧 = 幻觉的直接证据。留着这条记录，
        # 它是评估模型可靠性的第一手材料。
        notes.append(f"引用了不存在的帧号 {bogus}，已剔除")

    verdict = raw.verdict
    if verdict in {"completed", "not_completed"} and not valid_frames:
        # 肯定性断言却拿不出证据 → 降级，而不是采信。
        # 这是本文件存在的主要理由：宁可交给教师，不可凭空扣分。
        notes.append(f"判定为 {verdict} 但无有效证据帧，已降级为 not_visible")
        verdict = "not_visible"

    confidence = max(0.0, min(1.0, float(raw.confidence)))
    needs_review = confidence < review_confidence
    if needs_review:
        review_reasons.append(f"步骤「{name}」置信度偏低（{confidence:.2f}）")

    return ValidatedStep(
        step_code=raw.step_code,
        step_name=name,
        verdict=verdict,
        confidence=confidence,
        evidence_frames=valid_frames,
        reason=raw.reason,
        validation_notes=notes,
        needs_review=needs_review,
    )
