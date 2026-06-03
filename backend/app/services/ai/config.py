from dataclasses import dataclass
import os
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AISettings:
    provider: str = os.getenv("AI_ANALYZER_PROVIDER", "mock")
    frame_sample_fps: float = float(os.getenv("AI_FRAME_SAMPLE_FPS", "0.2"))
    max_frames: int = int(os.getenv("AI_MAX_FRAMES", "12"))
    yolo_model_path: str = os.getenv("YOLO_MODEL_PATH", "")
    yolo_confidence: float = float(os.getenv("YOLO_CONFIDENCE", "0.35"))
    mediapipe_enabled: bool = _bool_env("MEDIAPIPE_ENABLED", False)
    mmaction_config: str = os.getenv("MMACTION_CONFIG", "")
    mmaction_checkpoint: str = os.getenv("MMACTION_CHECKPOINT", "")
    multimodal_provider: str = os.getenv("MULTIMODAL_PROVIDER", "none")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    @property
    def yolo_enabled(self) -> bool:
        return self.provider in {"yolo", "hybrid", "full"} and bool(self.yolo_model_path)

    @property
    def mmaction_enabled(self) -> bool:
        return self.provider in {"mmaction", "hybrid", "full"} and bool(self.mmaction_config and self.mmaction_checkpoint)


def model_path(value: str) -> Path | None:
    return Path(value).expanduser().resolve() if value else None
