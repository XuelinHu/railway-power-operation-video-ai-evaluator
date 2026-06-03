import json
from typing import Any

from .config import AISettings
from .contracts import PoseFact, VideoFrame


class MediaPipePoseEstimator:
    def __init__(self, settings: AISettings):
        self.settings = settings
        self.detector: Any | None = None
        self.error: str | None = None
        if not settings.mediapipe_enabled:
            self.error = "MediaPipe 未启用"
            return
        try:
            import mediapipe as mp

            self.mp = mp
            self.detector = mp.solutions.pose.Pose(static_image_mode=True, model_complexity=1)
        except Exception as exc:
            self.error = f"MediaPipe 初始化失败：{exc}"

    def estimate(self, frames: list[VideoFrame]) -> tuple[list[PoseFact], list[str]]:
        if not self.detector:
            return [], [self.error or "MediaPipe pose unavailable"]

        try:
            import cv2
        except Exception as exc:
            return [], [f"OpenCV 不可用，跳过 MediaPipe 姿态识别：{exc}"]

        poses: list[PoseFact] = []
        notes: list[str] = []
        for frame in frames:
            image = cv2.imread(str(frame.path))
            if image is None:
                continue
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            result = self.detector.process(rgb)
            if not result.pose_landmarks:
                continue
            landmarks = [
                {"x": item.x, "y": item.y, "z": item.z, "visibility": item.visibility}
                for item in result.pose_landmarks.landmark
            ]
            visibility = sum(item["visibility"] for item in landmarks) / len(landmarks)
            poses.append(PoseFact(frame.timestamp_sec, float(visibility), json.dumps(landmarks, ensure_ascii=False)))
        return poses, notes
