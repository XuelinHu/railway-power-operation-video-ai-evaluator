"""VLM 分析编排：视频 → 抽帧 → Pass 1 → Pass 2 → 判定结果。

这里没有数据库、没有 FastAPI、没有后台任务——纯函数式的编排。
刻意的：Phase 1 的可行性脚本与 Phase 4 的 worker 共用这一份代码，
保证"闸门里测出来的东西"和"上线后跑的东西"是同一个实现。
闸门测的是别的实现，那闸门就白测了。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg import DEFAULT_FRAME_BUDGET, ExtractedFrame, extract_frames, probe_duration
from .judgment import judge
from .labels import REVIEW_CONFIDENCE_THRESHOLD
from .perception import DEFAULT_WINDOW_COUNT, observe, render_observation_table
from .schema import JudgmentOutcome
from .vlm_client import VLMClient, VLMResult

logger = logging.getLogger(__name__)

# 抽帧少于这个数量就不值得调 VLM：黑屏、极短的视频必然判不准，
# 花了钱还会产出一份看着像模像样的错误报告。宁可显式失败。
MIN_FRAMES_TO_ANALYZE = 4


class AnalysisFailed(RuntimeError):
    """分析无法完成。消息面向教师，不含路径与堆栈。"""


@dataclass
class AnalysisRun:
    """一次完整分析的全部产物与用量。"""

    outcome: JudgmentOutcome
    frames: list[ExtractedFrame]
    duration_sec: float
    calls: list[VLMResult] = field(default_factory=list)
    elapsed_sec: float = 0.0
    # 判定阶段实际读到的那份观察表。它一物三用：存进证据台账供教师核对、
    # 调 prompt 时用来分辨"是看错了还是判错了"、以及复现某次判定的输入。
    observation_table: str = ""

    @property
    def prompt_tokens(self) -> int:
        return sum(call.prompt_tokens for call in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(call.completion_tokens for call in self.calls)

    @property
    def estimated_cost_cny(self) -> float:
        return sum(call.estimated_cost_cny for call in self.calls)

    @property
    def request_ids(self) -> list[str]:
        """留档用。出账目争议时这是唯一能拿去核对的东西。"""
        return [call.request_id for call in self.calls if call.request_id]


def analyze(
    client: VLMClient,
    video: Path,
    work_dir: Path,
    *,
    frame_budget: int = DEFAULT_FRAME_BUDGET,
    window_count: int = DEFAULT_WINDOW_COUNT,
    review_confidence: float = REVIEW_CONFIDENCE_THRESHOLD,
    json_object: bool = False,
    on_stage: Callable[[str], None] | None = None,
) -> AnalysisRun:
    """跑完整条 VLM 链路。抽帧产物落在 work_dir，由调用方决定保留还是清理。

    `on_stage` 在 extract / perceive / judge 三个阶段入口被调用。
    三个而不是五个，是因为这就是真实可观测的阶段边界——
    再细分只能靠编造，而假的进度条比没有进度条更糟（老师会以为卡住了）。
    """
    started = time.monotonic()
    notify = on_stage or (lambda _name: None)

    notify("extract")
    duration = probe_duration(video)
    frames = extract_frames(video, work_dir, budget=frame_budget)

    if len(frames) < MIN_FRAMES_TO_ANALYZE:
        raise AnalysisFailed(
            f"视频可用画面不足（只抽到 {len(frames)} 帧，至少需要 {MIN_FRAMES_TO_ANALYZE} 帧）。"
            "视频可能过短、黑屏或损坏，请确认后重新上传。"
        )

    logger.info("抽帧完成：%d 帧，时长 %.1fs", len(frames), duration)

    notify("perceive")
    observations, perception_calls = observe(
        client, frames, window_count=window_count, json_object=json_object
    )
    table = render_observation_table(observations, frames)
    logger.info("Pass 1 完成：%d 个时间窗", len(observations))

    notify("judge")
    outcome, judgment_call = judge(
        client,
        table,
        frame_count=len(frames),
        review_confidence=review_confidence,
        json_object=json_object,
    )

    calls = [*perception_calls, judgment_call]
    run = AnalysisRun(
        outcome=outcome,
        frames=frames,
        duration_sec=duration,
        calls=calls,
        elapsed_sec=time.monotonic() - started,
        observation_table=table,
    )
    logger.info(
        "分析完成：用时 %.1fs，token 输入 %d / 输出 %d，估算 ¥%.4f",
        run.elapsed_sec, run.prompt_tokens, run.completion_tokens, run.estimated_cost_cny,
    )
    return run
