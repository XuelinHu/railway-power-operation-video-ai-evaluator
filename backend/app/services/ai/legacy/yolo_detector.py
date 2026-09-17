from pathlib import Path
from typing import Any

from .config import AISettings, model_path
from .contracts import DetectionFact, VideoFrame


class YoloDetector:
    def __init__(self, settings: AISettings):
        self.settings = settings
        self.model: Any | None = None
        self.error: str | None = None
        if not settings.yolo_enabled:
            self.error = "YOLO 未启用或未配置 YOLO_MODEL_PATH"
            return
        try:
            from ultralytics import YOLO

            path = model_path(settings.yolo_model_path)
            self.model = YOLO(str(path))
        except Exception as exc:
            self.error = f"YOLO 初始化失败：{exc}"

    def detect(self, frames: list[VideoFrame]) -> tuple[list[DetectionFact], list[str]]:
        if not self.model:
            return [], [self.error or "YOLO detector unavailable"]

        detections: list[DetectionFact] = []
        notes: list[str] = []
        for frame in frames:
            try:
                results = self.model.predict(str(frame.path), conf=self.settings.yolo_confidence, verbose=False)
            except Exception as exc:
                notes.append(f"YOLO 推理失败 {frame.path.name}: {exc}")
                continue

            for result in results:
                names = result.names
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue
                for box in boxes:
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])
                    xyxy = [float(value) for value in box.xyxy[0].tolist()]
                    label = str(names.get(cls_id, cls_id))
                    detections.append(
                        DetectionFact(
                            label=label,
                            confidence=conf,
                            timestamp_sec=frame.timestamp_sec,
                            bbox=",".join(f"{value:.4f}" for value in xyxy),
                        )
                    )
        return detections, notes
