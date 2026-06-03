from pathlib import Path
from typing import Any

from .config import AISettings
from .contracts import ActionFact


class MMActionRecognizer:
    def __init__(self, settings: AISettings):
        self.settings = settings
        self.recognizer: Any | None = None
        self.inference_recognizer: Any | None = None
        self.error: str | None = None
        if not settings.mmaction_enabled:
            self.error = "MMAction2 未启用或未配置 MMACTION_CONFIG/MMACTION_CHECKPOINT"
            return
        try:
            from mmaction.apis import inference_recognizer, init_recognizer

            self.recognizer = init_recognizer(settings.mmaction_config, settings.mmaction_checkpoint, device="cuda:0")
            self.inference_recognizer = inference_recognizer
        except Exception as exc:
            self.error = f"MMAction2 初始化失败：{exc}"

    def recognize(self, video_path: Path) -> tuple[list[ActionFact], list[str]]:
        if not self.recognizer or not self.inference_recognizer:
            return [], [self.error or "MMAction2 recognizer unavailable"]

        try:
            result = self.inference_recognizer(self.recognizer, str(video_path))
        except Exception as exc:
            return [], [f"MMAction2 推理失败：{exc}"]

        predictions = self._normalize_result(result)
        actions = [
            ActionFact(action=label, confidence=score, start_sec=index * 8.0, end_sec=index * 8.0 + 6.0)
            for index, (label, score) in enumerate(predictions[:5])
        ]
        return actions, []

    @staticmethod
    def _normalize_result(result: Any) -> list[tuple[str, float]]:
        pred_scores = getattr(result, "pred_score", None)
        if pred_scores is None:
            return []
        values = pred_scores.tolist()
        ranked = sorted(enumerate(values), key=lambda item: item[1], reverse=True)
        return [(f"action_{index}", float(score)) for index, score in ranked]
