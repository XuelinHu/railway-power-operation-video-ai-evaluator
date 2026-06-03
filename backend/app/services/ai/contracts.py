from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoFrame:
    path: Path
    timestamp_sec: float


@dataclass
class DetectionFact:
    label: str
    confidence: float
    timestamp_sec: float
    bbox: str = ""


@dataclass
class PoseFact:
    timestamp_sec: float
    confidence: float
    landmarks_json: str


@dataclass
class ActionFact:
    action: str
    confidence: float
    start_sec: float
    end_sec: float


@dataclass
class StepFact:
    step_code: str
    step_name: str
    start_sec: float
    end_sec: float
    confidence: float
    evidence: str


@dataclass
class AnalysisFacts:
    detections: list[DetectionFact]
    actions: list[ActionFact]
    steps: list[StepFact]
    notes: list[str]
