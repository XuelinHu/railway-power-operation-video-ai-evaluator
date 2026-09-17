"""提交视频的校验、指纹与转码。

分层说明：`ai/ffmpeg.py` 管"怎么调用 ffmpeg"，本模块管"我们对上传的视频有什么要求"。
混在一起会让后者被前者的实现细节淹没。

**转码不是可选项**，它同时解决三个问题：
1. 学生用手机拍的常是 HEVC/H.265，在 Windows 版 Chrome 里**放不出画面**——
   教师复核时看到黑屏，会以为视频坏了。
2. 抽帧输入被规范化，`select=gt(scene,...)` 的阈值表现才稳定。
3. 时长/分辨率上限有了统一的执行点，而不是每处各判一次。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from ..config import settings
from ..services.ai.ffmpeg import VideoUnreadable, find_ffmpeg, probe_media, require_ffmpeg

logger = logging.getLogger(__name__)

_HASH_CHUNK = 1024 * 1024
_TRANSCODE_TIMEOUT_SEC = 1800


class UploadRejected(ValueError):
    """上传的视频不符合要求。消息面向学生，必须是人话。"""


def sha256_of(path: Path) -> str:
    """流式计算指纹。**不要 read_bytes()** —— 2GB 的视频会直接把进程内存打爆。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def validate_duration(duration: float) -> None:
    """时长校验。

    上限比任何优化都省钱：实训视频 3-10 分钟，超过 15 分钟必然是误传
    （拍成整节课、或传错文件），直接拒绝能省下一整轮 VLM 调用。
    """
    if duration < settings.min_duration_sec:
        raise UploadRejected(
            f"视频只有 {duration:.0f} 秒，太短了，无法评价。"
            f"请上传完整的作业过程录像（至少 {settings.min_duration_sec:.0f} 秒）。"
        )
    if duration > settings.max_duration_sec:
        raise UploadRejected(
            f"视频时长 {duration / 60:.1f} 分钟，超过 {settings.max_duration_sec / 60:.0f} 分钟上限。"
            "请截取完整的作业过程后再上传。"
        )


def probe_or_reject(path: Path) -> float:
    """探测时长与视频轨并做校验。不合格 → 拒绝。

    这一步挡住"把 .txt 改名成 .mp4"这类伪装。**判据不能只是扩展名，
    也不能只是时长**：ffprobe 的格式探测很宽松，一段纯文本会被它猜成
    AMR-NB 音频并报出一个像模像样的时长，只看时长就会放行，
    然后在转码或抽帧时才失败——那时已经白烧了一轮资源和一次 AI 预算，
    而学生看到的是一个延迟很久、语焉不详的报错。所以在这里就要求
    **存在真正的视频轨**，报错也就落在了学生刚点完上传的那一刻。
    """
    try:
        info = probe_media(path)
    except VideoUnreadable as exc:
        raise UploadRejected(str(exc)) from exc

    if not info.has_video:
        raise UploadRejected(
            "这个文件里没有画面，看起来不是视频。"
            "请确认上传的是作业过程录像（手机拍摄的 MP4/MOV 即可），而不是音频或其它文件。"
        )
    validate_duration(info.duration)
    return info.duration


def transcode_for_playback(source: Path, target: Path) -> None:
    """转成 H.264/AAC + faststart。

    `-preset veryfast` 是刻意的：这台机器没有 GPU 且要同时跑 API 和 worker，
    转码把它自己饿死比转得慢一点糟糕得多。画质用 crf 23——复核是看动作，
    不是看画质，而源视频本身多是手机拍的。
    """
    require_ffmpeg()
    target.parent.mkdir(parents=True, exist_ok=True)
    args = [
        find_ffmpeg() or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-i", str(source),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",   # 兼容性关键：4:4:4 或 10bit 在浏览器里放不出
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",  # 让浏览器不必下完整个文件就能起播
        str(target),
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_TRANSCODE_TIMEOUT_SEC, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadRejected(
            f"视频转码超时（超过 {_TRANSCODE_TIMEOUT_SEC // 60} 分钟），文件可能损坏或过长。"
        ) from exc

    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        # stderr 可能带绝对路径，只取末行做内部日志；对外给固定文案。
        logger.error("转码失败 %s: %s", source.name, (proc.stderr or "").strip()[-500:])
        raise UploadRejected("视频转码失败，文件可能已损坏。请重新导出后再上传。")
