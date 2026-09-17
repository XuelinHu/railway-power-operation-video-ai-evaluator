"""作业处理：三段式，**任何写事务都不得跨越 VLM 调用**。

## 为什么必须分三段

朴素写法是"开一个 Session，中间调 VLM，最后 commit"。在 SQLite 上这会
造成一个连锁故障：写事务从第一次 `session.add()` 开始持有 RESERVED 锁，
而 VLM 调用要 30-60 秒。这 30-60 秒里**所有 API 写操作全部阻塞**
（登录、上传、复核全都在内），100 个用户看到的是大面积 `database is locked`。

更隐蔽的是它不会被日常测试发现——单用户手工点是撞不上的，
只有并发压上来才暴露，而那时候已经在真实课堂上了。

所以强制：

    阶段 A  短事务：读所需的一切 → 提交 → **关闭 session**
    阶段 B  无 session：转码 + 抽帧 + VLM
    阶段 C  短事务：写步骤、算分、出报告 → 提交

阶段 B 里唯一的数据库操作是进度上报，每次都是独立的毫秒级短事务。

## 为什么进度上报要独立开 session

它必须能在阶段 B 中途执行，而阶段 B 刻意不持有 session。
每次开新连接看着浪费，但 SQLite 的连接开销在本地文件上是微秒级，
换来的是"进度条真的在动"——而进度条不动是教师判断系统卡死的唯一依据。
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from ..clock import now
from ..config import settings
from ..database import FRAMES_DIR, UPLOAD_DIR, engine
from ..models import (
    AnalysisJob,
    AnalysisStatus,
    BudgetCounter,
    StepEvent,
    VideoSubmission,
)
from .ai.provider import AnalysisOutput, ProviderUnavailable, analyze_video
from .ai.vlm_analyzer import AnalysisFailed
from .ai.vlm_client import VLMError
from .report import generate_report
from .rule_engine import evaluate_rules
from .video import UploadRejected, transcode_for_playback

logger = logging.getLogger(__name__)

# 面向教师的阶段名与进度。数字是**估计值**，只用于画进度条；
# 真实的完成判据是 status 字段，不是这个百分比。
STAGE_TRANSCODE = (10, "正在转码视频")
STAGE_EXTRACT = (25, "正在抽取关键画面")
STAGE_PERCEIVE = (45, "正在识别画面内容")
STAGE_JUDGE = (75, "正在比对作业步骤")
STAGE_SCORE = (90, "正在按规则评分")
STAGE_REPORT = (96, "正在生成报告")

# 单个任务的墙钟上限。超过就强制失败，让 worker 去处理下一个——
# 一个卡死的任务不该拖垮整条队列（100 个学生等着，而队列是串行的）。
JOB_WALL_CLOCK_LIMIT_SEC = 300.0


class JobAborted(RuntimeError):
    """任务已被回收或转手，本 worker 必须立刻停手。

    这不是失败——是"别人接手了"。静默返回，绝不写任何结果，
    否则两个 worker 会互相覆盖对方的输出。
    """


@dataclass
class _Context:
    """阶段 A 读出来、阶段 B 需要的一切。刻意是纯数据，不带 session。"""

    job_id: int
    worker_id: str
    submission_id: int
    original_filename: str
    raw_path: Path
    play_path: Path
    needs_transcode: bool
    frames_dir: Path
    run_no: int
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 进度上报（阶段 B 里唯一的数据库写，每次都是独立短事务）
# ---------------------------------------------------------------------------


def _set_progress(job_id: int, worker_id: str, stage: tuple[int, str]) -> None:
    percent, label = stage
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        # 归属校验：僵尸回收器可能已经把任务转给了别的 worker，
        # 这时候继续往旧任务上写进度会覆盖别人的状态。
        if job is None or job.status != AnalysisStatus.running or job.worker_id != worker_id:
            return
        job.progress = percent
        job.stage = label
        # progress_at 与 heartbeat_at 分开：前者记录"任务真的在推进"。
        # 卡在 ffmpeg 里的进程仍然会更新 heartbeat_at（进程活着），
        # 只有 progress_at 能反映它其实一步没动。
        job.progress_at = now()
        session.add(job)
        session.commit()


# ---------------------------------------------------------------------------
# 预算闸门
# ---------------------------------------------------------------------------


def _today() -> str:
    return now().strftime("%Y-%m-%d")


def _budget_exceeded() -> str:
    """返回超限原因，未超限返回空串。

    防的不是单价（一个视频几分钱），是**失控放大**：一次重试风暴、
    或有人点了"批量重跑全库"，能把成本放大几十倍。超限时新任务直接判失败，
    而不是继续烧——宁可让教师看见"今日额度已用完"，也不要月底看见账单。
    """
    if settings.ai_provider != "vlm":
        return ""
    with Session(engine) as session:
        counter = session.exec(select(BudgetCounter).where(BudgetCounter.day == _today())).first()
        if counter and counter.calls >= settings.daily_call_budget:
            return (
                f"今日视觉模型调用额度已用完（{counter.calls}/{settings.daily_call_budget} 次）。"
                "这是防止费用失控的保护措施，请联系管理员调整额度或明天再试。"
            )
    return ""


def _record_budget(provider: str, prompt_tokens: int, completion_tokens: int, calls: int) -> None:
    if provider != "vlm" or calls <= 0:
        return
    with Session(engine) as session:
        day = _today()
        counter = session.exec(select(BudgetCounter).where(BudgetCounter.day == day)).first()
        if counter is None:
            counter = BudgetCounter(day=day)
        counter.calls += calls
        counter.prompt_tokens += prompt_tokens
        counter.completion_tokens += completion_tokens
        session.add(counter)
        session.commit()


# ---------------------------------------------------------------------------
# 阶段 A：短事务读取
# ---------------------------------------------------------------------------


def _prepare(job_id: int, worker_id: str) -> _Context:
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        if not job or job.status != AnalysisStatus.running or job.worker_id != worker_id:
            raise JobAborted(f"任务 {job_id} 已不属于本 worker")

        submission = session.get(VideoSubmission, job.submission_id)
        if submission is None:
            raise RuntimeError("提交记录不存在，任务无法继续。")

        # 重跑按 run_no 分目录。共用目录会让上一轮的帧被覆盖，
        # 磁盘不翻倍，但教师核对历史报告时会发现缩略图对不上。
        run_no = max(1, job.attempts)
        context = _Context(
            job_id=job_id,
            worker_id=worker_id,
            submission_id=submission.id,
            original_filename=submission.original_filename,
            raw_path=UPLOAD_DIR / submission.stored_filename,
            play_path=UPLOAD_DIR / f"{Path(submission.stored_filename).stem}.play.mp4",
            needs_transcode=submission.transcode_state != "done",
            frames_dir=FRAMES_DIR / str(job_id) / str(run_no),
            run_no=run_no,
        )

        job.stage = STAGE_TRANSCODE[1]
        job.progress = STAGE_TRANSCODE[0]
        job.progress_at = now()
        session.add(job)
        session.commit()
        return context


# ---------------------------------------------------------------------------
# 阶段 B：无 session
# ---------------------------------------------------------------------------


def _source_for_analysis(context: _Context) -> Path:
    """确保有一个 H.264/AAC 的副本，返回应当用于抽帧的文件。

    **转码不是可选项**，它同时解决三件事：学生用手机拍的 HEVC 在 Windows 版
    Chrome 里放不出画面（教师复核看到黑屏会以为视频坏了）、抽帧输入被规范化后
    场景检测阈值才稳定、以及时长/分辨率上限有了统一执行点。
    """
    # 已经有转码产物就直接用：重跑不必再转一次，省下几十秒。
    if context.play_path.exists():
        return context.play_path

    if not context.raw_path.exists():
        raise UploadRejected("上传的视频文件已丢失，无法分析。请联系管理员确认存储是否正常。")

    if context.needs_transcode:
        transcode_for_playback(context.raw_path, context.play_path)
        _mark_transcode(context.submission_id, "done", "")
    return context.play_path


def _mark_transcode(submission_id: int, state: str, error: str) -> None:
    """短事务写转码状态。

    状态单独落库而不是只存在于内存：转码要几十秒，
    这期间教师刷新页面应当看到"转码中"而不是"排队中"。
    """
    with Session(engine) as session:
        submission = session.get(VideoSubmission, submission_id)
        if submission is None:
            return
        submission.transcode_state = state
        submission.transcode_error = error[:500]
        session.add(submission)
        session.commit()


def _run(context: _Context) -> AnalysisOutput:
    source = _source_for_analysis(context)
    _set_progress(context.job_id, context.worker_id, STAGE_EXTRACT)

    def on_stage(name: str) -> None:
        _set_progress(
            context.job_id,
            context.worker_id,
            {"perceive": STAGE_PERCEIVE, "judge": STAGE_JUDGE}.get(name, STAGE_EXTRACT),
        )

    return analyze_video(source, context.frames_dir, on_stage=on_stage)


# ---------------------------------------------------------------------------
# 阶段 C：短事务写入
# ---------------------------------------------------------------------------


def _persist(context: _Context, output: AnalysisOutput) -> None:
    with Session(engine) as session:
        job = session.get(AnalysisJob, context.job_id)
        # 再次确认归属：从阶段 A 到现在过了几十秒，僵尸回收器可能已经
        # 判定本任务超时并转给了别的 worker。这时候写结果就是覆盖别人的。
        if (
            job is None
            or job.status != AnalysisStatus.running
            or job.worker_id != context.worker_id
        ):
            raise JobAborted(f"任务 {context.job_id} 在分析期间被回收")

        _set_progress(context.job_id, context.worker_id, STAGE_SCORE)

        for step in output.steps:
            session.add(
                StepEvent(
                    job_id=context.job_id,
                    step_code=step.step_code,
                    step_name=step.step_name,
                    start_sec=step.start_sec,
                    end_sec=step.end_sec,
                    confidence=step.confidence,
                    evidence=step.reason,
                    evidence_frames=list(step.evidence_frames),
                    source="vlm",
                    verdict=step.verdict,
                    needs_review=step.needs_review,
                    validation_note=step.validation_note,
                )
            )
        session.flush()  # 让规则引擎在同一个事务里看到刚写入的步骤

        evaluation = evaluate_rules(session, context.job_id)
        report = generate_report(session, context.job_id, evaluation)
        session.add(report)

        needs_review = evaluation.needs_review or output.needs_review
        job.score = evaluation.score
        job.status = AnalysisStatus.completed
        job.completed_at = now()
        job.error_code = ""
        job.error_message = ""
        job.provider = output.provider
        job.model = output.model
        job.prompt_tokens = output.prompt_tokens
        job.completion_tokens = output.completion_tokens
        job.request_ids = "\n".join(output.request_ids)
        job.progress = 100
        job.stage = "已完成"
        job.worker_id = ""
        job.summary = _summary(output, needs_review, evaluation.review_reasons)
        session.add(job)
        session.commit()


def _summary(output: AnalysisOutput, needs_review: bool, reasons: list[str]) -> str:
    parts: list[str] = []
    if output.scene_summary:
        parts.append(output.scene_summary)

    completed = sum(1 for step in output.steps if step.verdict == "completed")
    invisible = sum(1 for step in output.steps if step.verdict == "not_visible")
    parts.append(
        f"识别到 {completed}/{len(output.steps)} 个步骤完成，{invisible} 个步骤画面不可见。"
    )

    if needs_review:
        parts.append("本次结果需要教师人工复核。")
        if reasons:
            parts.append("；".join(reasons[:3]))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 失败
# ---------------------------------------------------------------------------


def _classify_failure(exc: BaseException) -> str:
    """把异常翻译成教师可读、且**不泄露目录结构**的中文原因。

    V1 直接把 `str(exc)` 存进 error_message 投给前端，那会把绝对路径
    甚至堆栈一起送出去——"哪台机器、装在哪、跑什么用户"全在里面。
    """
    for kind in (UploadRejected, ProviderUnavailable, VLMError, AnalysisFailed):
        if isinstance(exc, kind):
            return str(exc)
    return "分析过程中发生未预期的错误，请联系管理员查看后台日志。"


def _fail(job_id: int, exc: BaseException) -> None:
    """写失败状态。**绝不写分数** —— 不变量 #1。

    V1 的失败路径不重置 score，而它默认是 0，于是分析失败的作业在前端
    显示"0 分"，等于无故指控学生。
    """
    teacher_message = _classify_failure(exc)
    internal = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        if job is None:
            return
        job.status = AnalysisStatus.failed
        job.score = None          # 显式置空，不靠默认值
        job.summary = ""
        job.stage = "分析失败"
        job.error_code = teacher_message
        job.error_message = internal[-4000:]
        job.completed_at = now()
        job.worker_id = ""
        session.add(job)
        session.commit()
    logger.warning("任务 %s 失败：%s", job_id, teacher_message)


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def write_frames_meta(context: _Context, output: AnalysisOutput) -> None:
    """把本轮抽帧的台账写进 frames 目录。

    **数据出境台账**：送出去的每一帧、送给了哪个模型、prompt 是哪个版本，
    都必须留档。这既是合规要求（视频含学生人脸），也是调 prompt 时
    唯一能复现"当时模型看到的是什么"的依据。
    """
    meta = {
        "job_id": context.job_id,
        "run_no": context.run_no,
        "provider": output.provider,
        "model": output.model,
        "frame_times": output.frame_times,
        "request_ids": output.request_ids,
        "prompt_tokens": output.prompt_tokens,
        "completion_tokens": output.completion_tokens,
        "observation_table": output.observation_table,
        "written_at": now().isoformat(),
    }
    try:
        context.frames_dir.mkdir(parents=True, exist_ok=True)
        (context.frames_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # 台账写不进去不该让整个任务失败——视频已经分析完了、钱也花了，
        # 因为台账落盘失败而丢掉结果，对学生是不公平的。
        logger.warning("写入抽帧台账失败：%s", context.frames_dir, exc_info=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def process(job_id: int, worker_id: str) -> None:
    """处理一个已被本 worker 认领的任务。

    异常一律内部消化：调用方是 worker 主循环，它不该因为单个任务出错而退出。
    """
    try:
        context = _prepare(job_id, worker_id)
    except JobAborted:
        return
    except Exception as exc:  # 读取阶段就失败（如提交记录被删）
        _fail(job_id, exc)
        return

    reason = _budget_exceeded()
    if reason:
        _fail(job_id, UploadRejected(reason))
        return

    try:
        output = _run(context)
    except JobAborted:
        return
    except Exception as exc:
        if context.needs_transcode:
            _mark_transcode(context.submission_id, "failed", _classify_failure(exc))
        _fail(job_id, exc)
        return

    write_frames_meta(context, output)
    _record_budget(output.provider, output.prompt_tokens, output.completion_tokens, output.call_count)

    try:
        _persist(context, output)
    except JobAborted:
        logger.info("任务 %s 的结果因被回收而丢弃", job_id)
    except Exception as exc:
        _fail(job_id, exc)


__all__ = ["process", "JobAborted", "JOB_WALL_CLOCK_LIMIT_SEC"]
