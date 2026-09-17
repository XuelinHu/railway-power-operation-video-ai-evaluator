"""独立 worker 进程：`python -m app.worker`。

## 为什么不用 FastAPI 的 BackgroundTasks

V1 用的是 `background_tasks.add_task(run_analysis, job.id)`。它有三个致命问题，
在百人并发上传的场景下每一个都会真的发生：

1. **没有排队**。BackgroundTasks 是"响应返回后立刻跑"，100 个学生同时上传
   就是 100 个并发分析任务，把 CPU 和内存一起打爆。而且**没有任何上限或顺序**。
2. **进程重启即永久丢失**。`systemctl restart` 之后，所有排队中的任务再也不会执行，
   而数据库里它们的状态还停在 `pending`——学生看到"排队中"一直到毕业。
3. **无法观测**。任务在 API 进程里跑，教师看不到进度，
   运维也分不清"是 API 挂了还是分析卡住了"。

## 认领必须是原子的

SQLite 没有 `SELECT ... FOR UPDATE SKIP LOCKED`。如果写成
"先 SELECT 出 pending 列表，再逐个 UPDATE"，两个 worker 会同时选中同一个任务
（TOCTOU），结果是同一份视频被分析两遍、费用翻倍、两次写入互相覆盖。

正确做法是**带条件的 UPDATE + 判 rowcount**：

    UPDATE analysisjob SET status='running', worker_id=? WHERE id=? AND status='pending'

SQLite 保证单条 UPDATE 是原子的，rowcount == 1 的那个 worker 才算认领成功。
僵尸回收同理——不判 rowcount 的话，两个回收者会把同一个任务重复入队。

## 双心跳

`heartbeat_at` 记录"worker 进程还活着"，`progress_at` 记录"任务真的在推进"。
最常见的卡死形态是**进程活着但卡在 ffmpeg 或 VLM 连接上**，
只看心跳会认为一切正常，而实际上那个任务已经永远不会完成。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import update
from sqlmodel import Session, select

from .clock import now
from .config import settings
from .database import DATA_DIR, engine, init_db
from .models import AnalysisJob, AnalysisStatus
from .services import jobs
from .services.ai.ffmpeg import FFmpegUnavailable, require_ffmpeg
from .services.ai.provider import ProviderUnavailable, resolve_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
logger = logging.getLogger("app.worker")

# 空闲时的轮询间隔。没有任务时 worker 每秒查一次库，代价可忽略，
# 而 1 秒的延迟换来"学生传完 1 秒内就开始分析"的体感。
IDLE_POLL_SEC = 1.0
HEARTBEAT_INTERVAL_SEC = 15.0

# 心跳超过这个时长没更新 → worker 进程已死，任务必须回收。
# 取 120 秒而不是 30 秒：worker 可能在跑一个 CPU 密集的转码，
# 线程调度会被拖慢，阈值太紧会把正常运行的任务误判为死亡并重复执行（要花钱）。
DEAD_AFTER_SEC = 120.0

# 心跳新鲜但进度长期不动 → 卡在某个阶段。
# 取 600 秒是因为最长的一个阶段是转码（15 分钟视频在无 GPU 机器上约 2-5 分钟），
# 阈值必须显著大于它，否则正常的慢转码会被反复打断，永远跑不完。
STALL_AFTER_SEC = 600.0

# 重试上限。没有它，一个必然让 worker 崩溃的视频会**永远循环**：
# 回收 → 重跑 → 崩溃 → 回收，把队列堵死并持续烧钱。
MAX_ATTEMPTS = 3

# worker 存活标记。健康检查靠它判断"分析功能是否可用"——
# 只看 /api/health 的话，worker 挂了但 API 还活着，教师会以为系统正常，
# 传完视频等到下课也没有结果。
HEARTBEAT_FILE = DATA_DIR / "worker.heartbeat"


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


# ---------------------------------------------------------------------------
# 认领与回收
# ---------------------------------------------------------------------------


def claim_next(worker_id: str) -> int | None:
    """认领一个待处理任务，返回 job_id；没有可认领的返回 None。

    先按 id 取候选（先进先出，学生按上传顺序拿到结果），
    再对候选逐个做条件 UPDATE 直到有一个 rowcount == 1。
    """
    with Session(engine) as session:
        candidates = session.exec(
            select(AnalysisJob.id)
            .where(AnalysisJob.status == AnalysisStatus.pending)
            .order_by(AnalysisJob.id)
            .limit(5)
        ).all()

    for job_id in candidates:
        if _try_claim(job_id, worker_id):
            return job_id
    return None


def _try_claim(job_id: int, worker_id: str) -> bool:
    stamp = now()
    with Session(engine) as session:
        result = session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisStatus.pending)
            .values(
                status=AnalysisStatus.running,
                worker_id=worker_id,
                started_at=stamp,
                heartbeat_at=stamp,
                progress_at=stamp,
                attempts=AnalysisJob.attempts + 1,
                progress=2,
                stage="已认领，准备开始",
                # 清掉上一轮的失败信息。不清的话，重跑成功的任务
                # 前端仍然会显示上一次的红色错误。
                error_code="",
                error_message="",
            )
        )
        session.commit()
        return result.rowcount == 1


def reap_zombies() -> int:
    """回收死掉或卡死的任务，返回释放的任务数。

    V1 完全没有这个机制：一个被 `kill -9` 打断的任务永远停在 running，
    前端一直转圈，只有手工改数据库才能恢复。
    """
    stamp = now()
    reaped = 0

    with Session(engine) as session:
        running = session.exec(
            select(AnalysisJob).where(AnalysisJob.status == AnalysisStatus.running)
        ).all()

    for job in running:
        heartbeat = job.heartbeat_at or job.started_at
        progress = job.progress_at or job.started_at
        if heartbeat is None:
            continue

        dead = stamp - heartbeat > timedelta(seconds=DEAD_AFTER_SEC)
        stalled = stamp - progress > timedelta(seconds=STALL_AFTER_SEC)
        if not (dead or stalled):
            continue

        reason = "worker 进程已退出" if dead else "任务长时间没有进展"
        if job.attempts >= MAX_ATTEMPTS:
            if _mark_failed(job.id, job.attempts, reason):
                reaped += 1
            continue
        if _requeue(job.id, job.attempts, reason):
            logger.warning("回收任务 %s（%s），重新排队（第 %s 次尝试）", job.id, reason, job.attempts)
            reaped += 1
    return reaped


def _requeue(job_id: int, attempts: int, reason: str) -> bool:
    """把任务放回 pending。**必须判 rowcount**，否则两个回收者会重复入队。"""
    with Session(engine) as session:
        result = session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status == AnalysisStatus.running,
                # attempts 一起比对，防止回收一个刚刚被重新认领的任务
                # （认领会把 attempts +1，于是这里匹配不上，安全退出）。
                AnalysisJob.attempts == attempts,
            )
            .values(
                status=AnalysisStatus.pending,
                worker_id="",
                progress=0,
                stage=f"排队中（上次{reason}，自动重试）",
                heartbeat_at=None,
                progress_at=None,
            )
        )
        session.commit()
        return result.rowcount == 1


def _mark_failed(job_id: int, attempts: int, reason: str) -> bool:
    with Session(engine) as session:
        result = session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status == AnalysisStatus.running,
                AnalysisJob.attempts == attempts,
            )
            .values(
                status=AnalysisStatus.failed,
                score=None,          # 不变量 #1：失败绝不留下分数
                stage="分析失败",
                worker_id="",
                error_code=(
                    f"分析连续 {attempts} 次未能完成（{reason}）。"
                    "请确认视频能正常播放后，由教师在作业列表中重新分析。"
                ),
                completed_at=now(),
            )
        )
        session.commit()
        return result.rowcount == 1


def release(job_id: int) -> None:
    """正常处理完后清掉 worker 占用标记。"""
    with Session(engine) as session:
        session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == job_id)
            .values(worker_id="", heartbeat_at=None)
        )
        session.commit()


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------


class Heartbeat(threading.Thread):
    """独立线程，因为处理任务的主线程是阻塞的（ffmpeg/VLM 都在里面）。"""

    def __init__(self) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._job_id: int | None = None

    def watch(self, job_id: int) -> None:
        with self._lock:
            self._job_id = job_id

    def unwatch(self) -> None:
        with self._lock:
            self._job_id = None

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _beat() -> None:
        """worker 级存活标记：让健康检查在**空闲时**也能判断 worker 是否活着。

        只看任务表的话，"没有任务"和"worker 死了"长得一模一样。
        """
        try:
            HEARTBEAT_FILE.write_text(now().isoformat(), encoding="utf-8")
        except OSError:
            pass

    def run(self) -> None:
        # **先敲一次再进循环。** 循环里的写发生在第一次 wait 超时之后，
        # 也就是启动后 15 秒——在那之前心跳文件不存在，健康检查会报
        # "worker 不可用"。于是一次正常的重启，会让部署脚本和运维面板
        # 有 15 秒看到"服务起不来"，而那时进程其实活得好好的。
        self._beat()

        while not self._stop.wait(HEARTBEAT_INTERVAL_SEC):
            self._beat()
            stamp = now()

            with self._lock:
                job_id = self._job_id
            if job_id is None:
                continue
            # 只更新 heartbeat_at，**不碰 progress_at**。
            # 这正是双心跳的意义：卡死的任务心跳是新鲜的、进度是停的，
            # 一旦这里顺手把 progress_at 也刷新了，卡死就再也检测不出来。
            try:
                with Session(engine) as session:
                    session.execute(
                        update(AnalysisJob)
                        .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisStatus.running)
                        .values(heartbeat_at=stamp)
                    )
                    session.commit()
            except Exception:  # 心跳失败不该让线程死掉
                logger.warning("心跳写入失败（job=%s）", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# 启动检查
# ---------------------------------------------------------------------------


def preflight() -> None:
    """启动即校验。**缺失时拒绝启动**，不是等到第一个学生上传才失败。

    这条的代价对比很悬殊：启动时失败，运维当场就能看到并装好 ffmpeg；
    等到上课时才失败，是 100 个学生上传完、等着看结果的时候发现全挂了。
    """
    try:
        require_ffmpeg()
    except FFmpegUnavailable as exc:
        raise SystemExit(f"[worker 启动失败] {exc}") from exc

    try:
        provider = resolve_provider()
    except ProviderUnavailable as exc:
        raise SystemExit(f"[worker 启动失败] {exc}") from exc

    if provider == "fake":
        logger.warning("AI_PROVIDER=fake：产出的是固定假数据，仅供测试，**不可用于真实教学**。")
    elif provider == "demo":
        logger.warning("AI_PROVIDER=demo：结果按文件名猜测，未分析视频内容。")

    logger.info("启动检查通过：ffmpeg 可用，AI_PROVIDER=%s", provider)


def main() -> None:
    preflight()
    init_db()

    worker_id = _worker_id()
    logger.info("worker 启动：%s（单并发，串行处理）", worker_id)

    stop = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，处理完当前任务后退出……", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    heartbeat = Heartbeat()
    heartbeat.start()

    try:
        while not stop.is_set():
            try:
                reap_zombies()
                job_id = claim_next(worker_id)
                if job_id is None:
                    stop.wait(IDLE_POLL_SEC)
                    continue

                logger.info("开始处理任务 %s", job_id)
                heartbeat.watch(job_id)
                started = time.monotonic()
                try:
                    jobs.process(job_id, worker_id)
                finally:
                    heartbeat.unwatch()
                    release(job_id)
                logger.info("任务 %s 处理结束，用时 %.1fs", job_id, time.monotonic() - started)
            except Exception:
                # 主循环绝不能因为单个任务的异常而退出——worker 退出的代价是
                # 整条队列停摆，而队列停摆是静默的（没人会发现）。
                logger.exception("worker 主循环异常，5 秒后继续")
                stop.wait(5.0)
    finally:
        heartbeat.stop()
        try:
            HEARTBEAT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("worker 已退出：%s", worker_id)


if __name__ == "__main__":
    main()
