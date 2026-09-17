"""AI 提供方选择与统一输出契约。

三个提供方，由 `AI_PROVIDER` 选择：

- `vlm`（默认，生产）：通义千问-VL 两段式，见 vlm_analyzer.py
- `fake`：确定性假数据，**仅供自动化测试**。不联网、不花钱、结果可断言。
- `demo`：V1 的按文件名猜测的演示逻辑。仅在显式设置时启用。

**不变量 #3：绝不静默回退。** 配置成 `vlm` 但 key 缺失时，
系统必须**失败**，而不是悄悄改用 demo 数据跑出一份好看的报告。
V1 的病根正是"真实模型没装上就自动退回 mock，保证平台流程不断"——
流程不断的代价是**所有人恒定满分**，而没人会发现。

## 统一输出契约

三个提供方都必须产出 `AnalysisOutput`。规则引擎只认这个结构，
因此它不知道也不关心分数是怎么来的——这正是"模型只出事实、规则引擎算分"
这条边界的落地点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .labels import STEP_NAMES

logger = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """配置的提供方不可用。属于**部署问题**，应当在启动时 fail-fast。"""


@dataclass
class StepOutcome:
    """单个标准步骤的判定结果。"""

    step_code: str
    step_name: str
    verdict: str                       # completed|not_completed|not_visible|not_applicable
    confidence: float = 0.0
    evidence_frames: list[int] = field(default_factory=list)
    start_sec: float | None = None
    end_sec: float | None = None
    reason: str = ""
    needs_review: bool = False
    validation_note: str = ""


@dataclass
class AnalysisOutput:
    """一次分析的完整结果。**不含分数**——分数永远由 rule_engine 算。"""

    provider: str
    model: str
    steps: list[StepOutcome]
    video_quality: str = "usable"
    scene_summary: str = ""
    overall_note: str = ""
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)

    # 成本台账
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 计费口径是"调用次数"，不是"任务数"：一次分析有 3 次感知调用 + 1 次判定，
    # 按任务计数会让每日预算闸门形同虚设（额度写 2000，实际放行 8000 次调用）。
    call_count: int = 0
    request_ids: list[str] = field(default_factory=list)

    # 证据
    frame_times: list[float] = field(default_factory=list)   # 帧号 1..N 对应的时间
    observation_table: str = ""


def resolve_provider() -> str:
    from ...config import settings

    provider = settings.ai_provider
    if provider not in {"vlm", "fake", "demo"}:
        raise ProviderUnavailable(
            f"未知的 AI_PROVIDER={provider!r}。可选值：vlm、fake、demo。"
        )
    if provider == "vlm" and not settings.dashscope_api_key:
        # 这里必须抛，不能降级。见模块开头的说明。
        raise ProviderUnavailable(
            "AI_PROVIDER=vlm 但未配置 DASHSCOPE_API_KEY，无法调用视觉模型。\n"
            "请在 backend/.env 中设置 DASHSCOPE_API_KEY，"
            "或显式设置 AI_PROVIDER=fake 仅用于界面联调（会产出假数据）。"
        )
    return provider


def analyze_video(
    video: Path,
    work_dir: Path,
    *,
    frame_budget: int = 16,
    on_stage: "Callable[[str], None] | None" = None,
) -> AnalysisOutput:
    """按配置的提供方分析视频。

    `on_stage` 在进入 `extract` / `perceive` / `judge` 三个阶段时被调用，
    供调用方上报进度。它是**回调而不是返回值**，因为这个函数可能跑几十秒，
    调用方需要在它跑的过程中就能知道进展。
    """
    provider = resolve_provider()

    if provider == "vlm":
        return _analyze_vlm(video, work_dir, frame_budget=frame_budget, on_stage=on_stage)
    if provider == "fake":
        if on_stage:
            on_stage("perceive")
            on_stage("judge")
        return _analyze_fake(video, work_dir, frame_budget=frame_budget, on_stage=on_stage)
    return _analyze_demo(video, work_dir, frame_budget=frame_budget, on_stage=on_stage)


# ---------------------------------------------------------------------------
# vlm
# ---------------------------------------------------------------------------


def _analyze_vlm(
    video: Path,
    work_dir: Path,
    *,
    frame_budget: int,
    on_stage: "Callable[[str], None] | None" = None,
) -> AnalysisOutput:
    from ...config import settings
    from .vlm_analyzer import analyze
    from .vlm_client import VLMClient

    kwargs = {"model": settings.vlm_model} if settings.vlm_model else {}
    with VLMClient(settings.dashscope_api_key, **kwargs) as client:
        run = analyze(client, video, work_dir, frame_budget=frame_budget, on_stage=on_stage)

    outcome = run.outcome
    frame_times = [frame.timestamp_sec for frame in run.frames]

    steps: list[StepOutcome] = []
    for step in outcome.steps:
        # 时间只来自帧序号，绝不用模型估计的时间。模型从静帧估时间的能力基本为零，
        # 而 sequence 规则完全依赖 start_sec 的先后比较，一次错序就是一次误判。
        start_sec = None
        if step.evidence_frames:
            valid = [i for i in step.evidence_frames if 1 <= i <= len(frame_times)]
            if valid:
                start_sec = min(frame_times[i - 1] for i in valid)
        steps.append(
            StepOutcome(
                step_code=step.step_code,
                step_name=step.step_name,
                verdict=step.verdict,
                confidence=step.confidence,
                evidence_frames=step.evidence_frames,
                start_sec=start_sec,
                end_sec=start_sec,
                reason=step.reason,
                needs_review=step.needs_review,
                validation_note="；".join(step.validation_notes),
            )
        )

    return AnalysisOutput(
        provider="vlm",
        model=run.calls[0].model if run.calls else settings.vlm_model,
        steps=steps,
        video_quality=outcome.video_quality,
        scene_summary=outcome.scene_summary,
        overall_note=outcome.overall_note,
        warnings=outcome.warnings,
        needs_review=outcome.needs_review,
        review_reasons=outcome.review_reasons,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        call_count=len(run.calls),
        request_ids=run.request_ids,
        frame_times=frame_times,
        observation_table=run.observation_table,
    )


# ---------------------------------------------------------------------------
# 离线提供方（fake / demo）共用的证据帧处理
# ---------------------------------------------------------------------------


def _extract_frame_times(
    video: Path,
    work_dir: Path,
    frame_budget: int,
    on_stage: "Callable[[str], None] | None" = None,
) -> list[float]:
    """给 fake / demo 也**真抽一遍帧**，返回帧号 1..N 对应的时间。

    这两个提供方的**判定**是编的，但**证据不能是编的**：报告里每个 completed
    都挂着帧号，教师点开缩略图要能看到画面。

    此前 demo 恒返回 `[1]`、fake 恒返回 1..15，而磁盘上一帧都没有——
    点开就是 404。而"AI 的每个判断你都能点开看"恰恰是这套系统相对于
    "黑箱给个分"的全部卖点，演示时第一下点击就露馅。

    抽帧失败**不在这里抛异常**：demo 的用途就是"任何视频都能跑出结果"，
    真正的兜底是 `_fit_evidence()`——它会把找不到的帧号全部收敛掉，
    保证报告里不会留下指向空气的证据。
    """
    from .ffmpeg import extract_frames

    if on_stage:
        on_stage("extract")
    try:
        frames = extract_frames(video, work_dir, budget=frame_budget)
    except Exception:
        logger.warning("抽帧失败，本次不留证据帧（提供方=%s）", resolve_provider(), exc_info=True)
        return []
    return [frame.timestamp_sec for frame in frames]


def _fit_evidence(frames: list[int], frame_count: int, verdict: str) -> list[int]:
    """把帧号收敛到**真实存在**的范围内。

    和 `schema._validate_one` 同一条规矩：肯定性判定（completed /
    not_completed）拿不出有效证据就不能采信，降级为 `not_visible`。
    这里不降级，是因为调用方给的是固定剧本，降级只会让演示结果变得
    莫名其妙；但越界帧号必须剔除，否则又会退回到"点开是 404"。
    """
    valid = [index for index in frames if 1 <= index <= frame_count]
    if not valid and verdict in {"completed", "not_completed"} and frame_count:
        # 剧本里的帧号全都越界了（视频比预期短）。宁可指到第 1 帧，
        # 也不能留一个空证据——空证据在 schema 里等价于"该判定无效"。
        valid = [1]
    if verdict == "not_visible":
        # 看不见就不该有证据，这条在 schema 里是硬性的。
        return []
    return valid


def _spread(count: int, total: int) -> list[int]:
    """把 `count` 个步骤沿 `total` 帧均匀铺开，返回 1 基帧号。

    演示时九个步骤点开是九个不同时刻的画面，而不是同一个第 1 帧——
    后者看起来就像缩略图坏了。
    """
    if total <= 0 or count <= 0:
        return []
    if total == 1:
        return [1] * count
    return [1 + round(i * (total - 1) / max(1, count - 1)) for i in range(count)]


# ---------------------------------------------------------------------------
# fake（仅测试）
# ---------------------------------------------------------------------------


def _analyze_fake(
    video: Path,
    work_dir: Path,
    *,
    frame_budget: int = 16,
    on_stage: "Callable[[str], None] | None" = None,
) -> AnalysisOutput:
    """确定性假数据，供端到端测试断言。

    故意做成"部分完成、部分不可见"的形状，这样测试能覆盖到三态语义，
    而不是只测到"全对"这一条路径。
    """
    plan = {
        "ppe": ("completed", [1, 2], 0.92),
        "ticket": ("not_visible", [], 0.70),
        "tool_check": ("completed", [3], 0.85),
        "power_off": ("completed", [5], 0.80),
        "voltage_test": ("not_completed", [6, 7], 0.88),
        "ground_wire": ("completed", [9], 0.83),
        "switch_operation": ("completed", [11], 0.79),
        "review": ("not_visible", [], 0.65),
        "cleanup": ("completed", [15], 0.81),
    }
    # 真抽帧，让断言里出现的帧号在磁盘上真的有对应文件。
    # 测试的端到端路径也要经过 frames 接口，否则"证据能点开"这件事
    # 只有生产路径被测到，而夹具路径的坏了没人知道。
    frame_times = _extract_frame_times(video, work_dir, frame_budget, on_stage)

    steps = [
        StepOutcome(
            step_code=code,
            step_name=STEP_NAMES[code],
            verdict=verdict,
            confidence=confidence,
            evidence_frames=_fit_evidence(frames, len(frame_times), verdict),
            start_sec=frame_times[frames[0] - 1] if frames and frames[0] <= len(frame_times) else None,
            end_sec=frame_times[frames[0] - 1] if frames and frames[0] <= len(frame_times) else None,
            reason=f"[fake] {STEP_NAMES[code]} 判定为 {verdict}",
            needs_review=confidence < 0.6,
        )
        for code, (verdict, frames, confidence) in plan.items()
    ]
    return AnalysisOutput(
        provider="fake",
        model="fake-analyzer",
        steps=steps,
        video_quality="usable",
        scene_summary="[fake] 测试用固定场景描述。",
        frame_times=frame_times,
    )


# ---------------------------------------------------------------------------
# demo（V1 遗留的演示逻辑，仅在显式设置时启用）
# ---------------------------------------------------------------------------


def _analyze_demo(
    video: Path,
    work_dir: Path,
    *,
    frame_budget: int = 16,
    on_stage: "Callable[[str], None] | None" = None,
) -> AnalysisOutput:
    """按文件名关键词猜测结果。**只在 AI_PROVIDER=demo 时启用。**

    保留它是因为演示/培训场景仍需要"任何视频都能跑出结果"；
    但它必须被显式选中，绝不能成为默认或回退路径。

    **判定是假的，帧是真的**：这里仍然走一遍真实抽帧，让报告里的
    证据帧能点开看到画面。判定本身依旧由文件名决定，界面上有"演示数据"
    横幅明示这一点——但演示时不该出现"点缩略图 404"这种一眼假的东西。
    """
    from .legacy.mock_analyzer import MockAnalyzer

    frame_times = _extract_frame_times(video, work_dir, frame_budget, on_stage)
    facts = MockAnalyzer().analyze(video, video.name)
    by_code = {event.step_code: event for event in facts.steps}

    completed_codes = [code for code in STEP_NAMES if code in by_code]
    spreads = dict(zip(completed_codes, _spread(len(completed_codes), len(frame_times))))

    steps: list[StepOutcome] = []
    for code, name in STEP_NAMES.items():
        event = by_code.get(code)
        if event is None:
            steps.append(StepOutcome(code, name, "not_completed", 0.5, [],
                                     reason=f"[demo] 未识别到「{name}」"))
        else:
            index = spreads.get(code, 1)
            start_sec = frame_times[index - 1] if index <= len(frame_times) else None
            steps.append(StepOutcome(
                code, name, "completed", event.confidence,
                _fit_evidence([index], len(frame_times), "completed"),
                start_sec, start_sec,
                reason=f"[demo] 按文件名关键词判定「{name}」已完成",
            ))
    # 判定 stage 在这里是空转的（没有模型调用），但进度条需要它，
    # 否则演示模式下前端会一直停在"感知中"。
    if on_stage:
        on_stage("judge")

    logger.warning("正在使用 AI_PROVIDER=demo，结果为按文件名猜测的演示数据。")
    return AnalysisOutput(
        provider="demo",
        model="demo-keyword-analyzer",
        steps=steps,
        video_quality="usable",
        scene_summary="[演示数据] 结果由文件名关键词生成，未分析视频内容。",
        warnings=["当前为演示模式，结果不是对视频内容的真实分析。"],
        frame_times=frame_times,
    )
