"""FFmpeg/ffprobe 封装：能力探测、时长探测、抽帧。

这是整条 VLM 链路的地基。三条硬性约束写在这里，不要绕过：

1. **绝不静默返回空结果**。ffmpeg 不可用或视频解不开一律抛异常，由调用方转成
   教师可读的中文失败原因。旧实现 `frame_extractor.py` 在缺 ffmpeg 时返回空列表，
   导致整条链路静默退化成 mock —— 那正是"AI 看起来在工作其实什么都没做"的根源。
2. **每个子进程都要有 timeout**。损坏的视频能让 ffmpeg 永久挂住，在没有 timeout 的
   情况下会永久占死一个 worker 进程。
3. **时间只来自帧序号**，不接受模型估计的时间。抽帧时就把每一帧的
   `timestamp_sec` 确定下来，后续所有环节（prompt、证据展示、"跳到视频第几秒"）
   都以此为准。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 目标帧数预算：固定值，不随时长线性增长。
# 一次配错就是 30 倍 API 成本，所以这里是硬上限而不是建议值。
DEFAULT_FRAME_BUDGET = 16

# 长边上限。1024 长边约 576 token/帧（qwen 系列每 32×32 像素一个 token），
# 同时保证 16 帧 base64 后远低于 DashScope 的 6MB 请求体上限。
DEFAULT_MAX_LONG_SIDE = 1024

# mjpeg 的 -q:v 取 2(最好)~31(最差)，6 是"肉眼够用且体积可控"的一档。
DEFAULT_JPEG_QSCALE = 6

# 相邻保留帧的最小时间间隔（秒），避免场景切换帧与均匀帧挤在一起。
DEFAULT_MIN_GAP_SEC = 5.0

# 场景切换判定阈值。单纯均匀采样对"戴手套"这种几秒的动作召回率很低，
# 而 ppe 恰恰是最常触发的扣分项，所以必须叠加场景帧。
DEFAULT_SCENE_THRESHOLD = 0.3

_PROBE_TIMEOUT_SEC = 30
_EXTRACT_TIMEOUT_SEC = 180
_PTS_PATTERN = re.compile(r"pts_time:([0-9.]+)")


class FFmpegUnavailable(RuntimeError):
    """ffmpeg / ffprobe 不可用。属于环境问题，应当在启动时就 fail-fast。"""


class VideoUnreadable(RuntimeError):
    """视频无法解码：文件损坏、编码不受支持，或时长为 0。"""


@dataclass(frozen=True)
class ExtractedFrame:
    path: Path
    timestamp_sec: float
    reason: str  # "uniform"（均匀采样）或 "scene"（场景切换）


def _resolve(env_key: str, binary: str) -> str | None:
    """按 环境变量 → PATH 的顺序定位二进制。环境变量指向不存在的文件时视为未配置。"""
    explicit = os.getenv(env_key, "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    return shutil.which(binary)


def find_ffmpeg() -> str | None:
    return _resolve("FFMPEG_BIN", "ffmpeg")


def find_ffprobe() -> str | None:
    return _resolve("FFPROBE_BIN", "ffprobe")


def require_ffmpeg() -> tuple[str, str]:
    """返回 (ffmpeg, ffprobe)；缺失时抛出带修复指引的异常，供启动时 fail-fast。"""
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe

    missing = "、".join(name for name, found in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not found)
    raise FFmpegUnavailable(
        f"缺少 {missing}，无法进行视频抽帧与时长探测。\n"
        "安装方式二选一：\n"
        "  1. 系统包管理器：apt install ffmpeg\n"
        "  2. 静态构建（无需 root）：下载后解压，在 .env 中指定绝对路径\n"
        "     FFMPEG_BIN=/path/to/ffmpeg\n"
        "     FFPROBE_BIN=/path/to/ffprobe"
    )


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool


def probe_media(video: Path, *, timeout: float = _PROBE_TIMEOUT_SEC) -> MediaInfo:
    """一次 ffprobe 取回时长，并判断**是否存在真正的视频轨**。

    **只判时长挡不住伪装文件**：ffprobe 的格式探测相当宽松，
    一段纯文本会被它猜成 AMR-NB 音频并报出 6.5 秒的"时长"，
    于是改名为 .mp4 的 .txt 能一路通过上传校验，直到转码才炸——
    白烧一次转码，还可能在 VLM 那一步才失败。判据必须是视频轨本身。

    时长读不出来 = 文件损坏或编码不受支持。
    """
    _, ffprobe = require_ffmpeg()
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,width,height",
                "-of", "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoUnreadable(f"读取视频信息超时（>{timeout:g} 秒），文件可能已损坏。") from exc

    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        payload = {}

    try:
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise VideoUnreadable(
            "无法读取视频时长：文件可能损坏、未上传完整，或使用了不受支持的编码。请重新导出后再上传。"
        ) from None

    if duration <= 0:
        raise VideoUnreadable("视频时长为 0，无法分析。请确认文件内容正常。")

    # 封面图会被 ffprobe 报成一条 video 轨，所以还要求它有真实尺寸；
    # 纯音频文件（mp3 改名成 mp4）因此也会被挡在这里。
    has_video = any(
        stream.get("codec_type") == "video"
        and (stream.get("width") or 0) > 0
        and (stream.get("height") or 0) > 0
        for stream in payload.get("streams", [])
    )
    return MediaInfo(duration=duration, has_video=has_video)


def probe_duration(video: Path, *, timeout: float = _PROBE_TIMEOUT_SEC) -> float:
    """只要时长。已经过转码的视频用这个即可。"""
    return probe_media(video, timeout=timeout).duration


def _scale_filter(max_long_side: int) -> str:
    """限制长边尺寸，且绝不放大：竖屏限制高度，横屏限制宽度。"""
    return (
        f"scale=w='if(gt(iw,ih),min({max_long_side},iw),-2)':"
        f"h='if(gt(iw,ih),-2,min({max_long_side},ih))'"
    )


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise VideoUnreadable(f"视频处理超时（>{timeout:g} 秒），已中止。") from exc


def _extract_uniform(
    video: Path, out_dir: Path, *, count: int, duration: float,
    max_long_side: int, qscale: int, timeout: float,
) -> list[ExtractedFrame]:
    """均匀采样：把整段视频等分成 count 份，每份取一帧。保证覆盖到片尾。"""
    fps = count / duration if duration > 0 else 1.0
    pattern = out_dir / "u_%04d.jpg"
    args = [
        find_ffmpeg() or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps={fps:.6f},{_scale_filter(max_long_side)}",
        "-frames:v", str(count),
        "-q:v", str(qscale),
        "-fps_mode", "vfr",
        str(pattern),
    ]
    proc = _run(args, timeout=timeout)
    files = sorted(out_dir.glob("u_*.jpg"))
    if proc.returncode != 0 and not files:
        raise VideoUnreadable("抽帧失败：视频无法解码。请确认文件能正常播放后重传。")

    interval = duration / count if count else 0.0
    return [
        ExtractedFrame(path=path, timestamp_sec=index * interval, reason="uniform")
        for index, path in enumerate(files)
    ]


def _extract_scene(
    video: Path, out_dir: Path, *, threshold: float, max_long_side: int,
    qscale: int, limit: int, timeout: float,
) -> list[ExtractedFrame]:
    """场景切换采样：抓动作边界。showinfo 的 pts_time 是时间戳的唯一来源。"""
    pattern = out_dir / "s_%04d.jpg"
    args = [
        find_ffmpeg() or "ffmpeg",
        "-hide_banner", "-loglevel", "info",
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo,{_scale_filter(max_long_side)}",
        "-frames:v", str(limit),
        "-q:v", str(qscale),
        "-fps_mode", "vfr",
        str(pattern),
    ]
    proc = _run(args, timeout=timeout)
    timestamps = [float(value) for value in _PTS_PATTERN.findall(proc.stderr or "")]
    files = sorted(out_dir.glob("s_*.jpg"))

    # showinfo 与产物文件应当一一对应；数量不一致时以文件为准并放弃时间戳，
    # 避免把错误的秒数写进证据链（宁可少几帧场景帧，也不能给出错的时间）。
    if len(timestamps) != len(files):
        return []
    return [
        ExtractedFrame(path=path, timestamp_sec=ts, reason="scene")
        for path, ts in zip(files, timestamps)
    ]


def _evenly(items: list[ExtractedFrame], count: int) -> list[ExtractedFrame]:
    """从列表中等距取 count 个，**首尾必取**。用 round 而非 int 以免整体向前偏移。"""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[0]]

    last = len(items) - 1
    picked = {0, last}
    for step in range(1, count - 1):
        picked.add(round(step * last / (count - 1)))
    return [items[index] for index in sorted(picked)]


def _apply_min_gap(frames: list[ExtractedFrame], min_gap: float) -> list[ExtractedFrame]:
    """按时间顺序去掉挨得太近的帧；同样距离下**场景帧胜出**。"""
    kept: list[ExtractedFrame] = []
    for frame in frames:
        if kept and frame.timestamp_sec - kept[-1].timestamp_sec < min_gap:
            if frame.reason == "scene" and kept[-1].reason == "uniform":
                kept[-1] = frame
            continue
        kept.append(frame)
    return kept


def _dedupe_and_trim(
    frames: list[ExtractedFrame], *, budget: int, min_gap: float
) -> list[ExtractedFrame]:
    """按时间排序去重，超预算时**场景帧优先、均匀帧保覆盖**。

    两个都不能丢，理由不同：

    - **均匀帧丢不得**，因为它保证时间轴覆盖。截断会丢掉片尾，而"复核状态""清理现场"
      恰恰是标准流程的最后两步，丢了就直接导致这两步被判成 not_visible。
    - **场景帧丢不得**，因为它是唯一能抓到"戴绝缘手套"这类几秒动作的来源，
      而 ppe 是最常触发的扣分项。若把场景帧和均匀帧混在一起等分抽样，
      场景帧会被均匀地稀释掉——那就等于白抓了（实测 16 帧里只剩 3 个场景帧）。

    所以超预算时给场景帧**预留名额**（不超过一半，防止动作密集的片段挤掉全片覆盖），
    余下名额按时间等距分给均匀帧，首尾必取。
    """
    kept = _apply_min_gap(sorted(frames, key=lambda item: item.timestamp_sec), min_gap)

    if len(kept) <= budget:
        return kept
    if budget == 1:
        return [kept[0]]

    scene = [frame for frame in kept if frame.reason == "scene"]
    uniform = [frame for frame in kept if frame.reason == "uniform"]

    # 上限取一半：动作密集的视频可能几十个场景帧，全留就没有时间轴覆盖了。
    scene_quota = min(len(scene), max(1, budget // 2))
    selected = _evenly(scene, scene_quota)
    selected += _evenly(uniform, budget - len(selected))

    # 合并后两类帧可能挨得很近（同一时刻的图像重复），再过滤一次。
    # 这一步可能略少于预算，但换来的是信息密度——重复帧对模型毫无价值。
    return _apply_min_gap(sorted(selected, key=lambda item: item.timestamp_sec), min_gap)


def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    budget: int = DEFAULT_FRAME_BUDGET,
    max_long_side: int = DEFAULT_MAX_LONG_SIDE,
    qscale: int = DEFAULT_JPEG_QSCALE,
    min_gap: float = DEFAULT_MIN_GAP_SEC,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    timeout: float = _EXTRACT_TIMEOUT_SEC,
) -> list[ExtractedFrame]:
    """抽帧并持久化为 `f001.jpg`…，返回按时间排序的帧列表。

    调用方负责保证 out_dir 存在且为本轮分析独占（重跑要分目录，否则互相覆盖）。
    """
    require_ffmpeg()
    duration = probe_duration(video, timeout=timeout)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = out_dir / "_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    uniform = _extract_uniform(
        video, scratch, count=budget, duration=duration,
        max_long_side=max_long_side, qscale=qscale, timeout=timeout,
    )
    scene = _extract_scene(
        video, scratch, threshold=scene_threshold, max_long_side=max_long_side,
        qscale=qscale, limit=budget, timeout=timeout,
    )

    selected = _dedupe_and_trim(uniform + scene, budget=budget, min_gap=min_gap)

    # 落到最终文件名。时间戳与文件名的对应关系一旦确定就不再变动，
    # 后续 prompt、数据库、前端展示全部引用这份映射。
    persisted: list[ExtractedFrame] = []
    for index, frame in enumerate(selected, start=1):
        target = out_dir / f"f{index:03d}.jpg"
        shutil.move(str(frame.path), target)
        persisted.append(ExtractedFrame(path=target, timestamp_sec=frame.timestamp_sec, reason=frame.reason))

    shutil.rmtree(scratch, ignore_errors=True)
    return persisted
