"""上传限制与分析能力必须对得上。

这里钉的是一条**跨模块的算术关系**，它此前是断的：

    上传放行的最短时长 5 秒  →  实际只能抽到 1 帧
    但分析至少要 4 帧

于是 5-19 秒的视频会**通过上传校验**、排进队列、轮到它时才失败，
报"视频可用画面不足"。学生白等一场，还占掉一次分析配额；而这件事
在他点上传的那一秒就已经确定了。

实测（testsrc，budget=16，min_gap=5）：
    5 秒 → 1 帧   10 秒 → 2 帧   15 秒 → 3 帧   20 秒 → 4 帧   30 秒 → 6 帧

所以下面这条断言不是"抄一遍常量"，而是把那三个数字之间的依赖关系
写死：谁调大了抽帧间隔、或调小了上传下限，这里立刻红。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.config import DEFAULT_MIN_DURATION_SEC
from app.services.ai.ffmpeg import DEFAULT_FRAME_BUDGET, DEFAULT_MIN_GAP_SEC, extract_frames
from app.services.ai.vlm_analyzer import MIN_FRAMES_TO_ANALYZE


def test_min_duration_yields_enough_frames():
    """纯算术版本：不给任何"正好差一帧"的余地。

    帧要落在 [0, duration] 上且两两间隔 >= min_gap，
    所以能放下的帧数是 floor(duration / min_gap) + 1。
    """
    capacity = int(DEFAULT_MIN_DURATION_SEC // DEFAULT_MIN_GAP_SEC) + 1
    assert capacity >= MIN_FRAMES_TO_ANALYZE, (
        f"上传放行 {DEFAULT_MIN_DURATION_SEC:.0f} 秒，按 {DEFAULT_MIN_GAP_SEC:.0f} 秒间隔"
        f"最多只能放 {capacity} 帧，而分析需要 {MIN_FRAMES_TO_ANALYZE} 帧。"
        "结果是：上传成功、排队、然后失败。"
    )


def test_min_duration_really_produces_enough_frames():
    """真跑一遍抽帧，而不是只信上面的算术。

    算术版本依赖"帧正好铺满时长"这个理想假设；实际能不能抽到还取决于
    ffmpeg 的行为。这里用一段恰好等于上传下限的视频实测，把假设验掉。
    """
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / f"min-{DEFAULT_MIN_DURATION_SEC:.0f}s.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc=size=320x240:rate=10:duration={DEFAULT_MIN_DURATION_SEC:.0f}",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                str(video),
            ],
            check=True,
            timeout=180,
        )

        frames = extract_frames(video, Path(tmp) / "out", budget=DEFAULT_FRAME_BUDGET)

    assert len(frames) >= MIN_FRAMES_TO_ANALYZE, (
        f"一段正好 {DEFAULT_MIN_DURATION_SEC:.0f} 秒（= 上传下限）的视频只抽到 "
        f"{len(frames)} 帧，少于分析所需的 {MIN_FRAMES_TO_ANALYZE} 帧。"
        "上传下限必须保证能分析的视频才放行。"
    )


def test_upload_duration_check_is_actually_wired_up():
    """下限真的接到了上传校验上——常量改对了但忘了接线，同样会漏。

    这里用**当前生效的** `settings.min_duration_sec` 而不是那个默认常量：
    测试夹具会把 MIN_DURATION_SEC 调到 1（否则 4 秒的样本视频传不上去），
    拿生产默认值来断言只会测出一个环境差异，而不是接线对不对。
    生产默认值本身够不够，由上面两条算术/实测用例负责。
    """
    from app.config import settings
    from app.services.video import UploadRejected, validate_duration

    # 低于下限：必须拒，且理由要能读懂。
    try:
        validate_duration(settings.min_duration_sec - 1)
    except UploadRejected as exc:
        assert "太短" in str(exc), str(exc)
    else:
        raise AssertionError("低于上传下限的时长竟然通过了校验")

    # 恰好等于下限：必须放行（边界不能写成 < 而不是 <=）。
    validate_duration(settings.min_duration_sec)
