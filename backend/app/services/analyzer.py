from pathlib import Path

from .ai.contracts import ActionFact, AnalysisFacts, DetectionFact, StepFact
from .ai.pipeline import RailwayPowerVideoAnalyzer


class Analyzer:
    """Compatibility facade used by the existing analysis job router."""

    def __init__(self):
        self.pipeline = RailwayPowerVideoAnalyzer()

    def analyze(self, video_path: Path, original_filename: str) -> AnalysisFacts:
        return self.pipeline.analyze(video_path, original_filename)


__all__ = ["Analyzer", "ActionFact", "AnalysisFacts", "DetectionFact", "StepFact"]
