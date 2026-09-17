from pathlib import Path

from .action_recognizer import MMActionRecognizer
from .config import AISettings
from .contracts import ActionFact, AnalysisFacts, DetectionFact, StepFact
from .frame_extractor import FrameExtractor
from .mock_analyzer import MockAnalyzer
from .multimodal import MultimodalExplainer
from .pose_estimator import MediaPipePoseEstimator
from .steps import ACTION_TO_STEP, OBJECT_TO_STEP, STEP_NAMES
from .yolo_detector import YoloDetector


class RailwayPowerVideoAnalyzer:
    def __init__(self, settings: AISettings | None = None):
        self.settings = settings or AISettings()
        self.mock = MockAnalyzer()
        self.frame_extractor = FrameExtractor(self.settings.frame_sample_fps, self.settings.max_frames)
        self.yolo = YoloDetector(self.settings)
        self.pose = MediaPipePoseEstimator(self.settings)
        self.actions = MMActionRecognizer(self.settings)
        self.explainer = MultimodalExplainer(self.settings)

    def analyze(self, video_path: Path, original_filename: str) -> AnalysisFacts:
        if self.settings.provider == "mock":
            return self.mock.analyze(video_path, original_filename)

        temp_dir, frames = self.frame_extractor.extract(video_path)
        notes: list[str] = []
        try:
            detections, detection_notes = self.yolo.detect(frames)
            notes.extend(detection_notes)

            _, pose_notes = self.pose.estimate(frames)
            notes.extend(pose_notes)

            actions, action_notes = self.actions.recognize(video_path)
            notes.extend(action_notes)

            if not detections and not actions:
                fallback = self.mock.analyze(video_path, original_filename)
                fallback.notes.extend(["真实模型未产出可评分事实，已回退 mock 分析"] + notes)
                return fallback

            steps = self._build_steps(detections, actions)
            if not steps:
                fallback = self.mock.analyze(video_path, original_filename)
                fallback.detections = detections or fallback.detections
                fallback.actions = actions or fallback.actions
                fallback.notes.extend(["模型事实无法映射标准步骤，已回退 mock 步骤"] + notes)
                return fallback

            steps, explain_notes = self.explainer.enrich_steps(frames, detections, actions, steps)
            notes.extend(explain_notes)
            return AnalysisFacts(detections=detections, actions=actions, steps=steps, notes=notes)
        finally:
            temp_dir.cleanup()

    def _build_steps(self, detections: list[DetectionFact], actions: list[ActionFact]) -> list[StepFact]:
        events: dict[str, tuple[float, float, float, str]] = {}

        for detection in detections:
            step_code = OBJECT_TO_STEP.get(detection.label)
            if not step_code:
                continue
            current = events.get(step_code)
            evidence = f"{_format_time(detection.timestamp_sec)} 检测到 {detection.label}，置信度 {detection.confidence:.2f}。"
            if current is None or detection.timestamp_sec < current[0]:
                events[step_code] = (detection.timestamp_sec, detection.timestamp_sec + 3.0, detection.confidence, evidence)

        for action in actions:
            step_code = ACTION_TO_STEP.get(action.action, action.action if action.action in STEP_NAMES else "")
            if not step_code:
                continue
            current = events.get(step_code)
            evidence = f"{_format_time(action.start_sec)} 到 {_format_time(action.end_sec)} 识别到动作“{action.action}”。"
            if current is None or action.confidence > current[2]:
                events[step_code] = (action.start_sec, action.end_sec, action.confidence, evidence)

        steps = [
            StepFact(
                step_code=code,
                step_name=STEP_NAMES[code],
                start_sec=value[0],
                end_sec=value[1],
                confidence=value[2],
                evidence=value[3],
            )
            for code, value in events.items()
            if code in STEP_NAMES
        ]
        return sorted(steps, key=lambda item: item.start_sec)


def _format_time(seconds: float) -> str:
    minute = int(seconds // 60)
    second = int(seconds % 60)
    return f"{minute:02d}:{second:02d}"
