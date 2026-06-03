from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from .contracts import VideoFrame


class FrameExtractor:
    def __init__(self, sample_fps: float, max_frames: int):
        self.sample_fps = sample_fps
        self.max_frames = max_frames

    def extract(self, video_path: Path) -> tuple[TemporaryDirectory, list[VideoFrame]]:
        temp_dir = TemporaryDirectory(prefix="railway-ai-frames-")
        output_dir = Path(temp_dir.name)

        if not shutil.which("ffmpeg"):
            return temp_dir, []

        pattern = output_dir / "frame_%05d.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps={self.sample_fps}",
            "-frames:v",
            str(self.max_frames),
            str(pattern),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return temp_dir, []

        interval = 1 / self.sample_fps if self.sample_fps > 0 else 5
        frames = [
            VideoFrame(path=path, timestamp_sec=index * interval)
            for index, path in enumerate(sorted(output_dir.glob("frame_*.jpg")))
        ]
        return temp_dir, frames
