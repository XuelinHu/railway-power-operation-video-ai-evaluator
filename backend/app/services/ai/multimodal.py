from .config import AISettings
from .contracts import ActionFact, DetectionFact, StepFact, VideoFrame


class MultimodalExplainer:
    def __init__(self, settings: AISettings):
        self.settings = settings

    def enrich_steps(
        self,
        frames: list[VideoFrame],
        detections: list[DetectionFact],
        actions: list[ActionFact],
        steps: list[StepFact],
    ) -> tuple[list[StepFact], list[str]]:
        if self.settings.multimodal_provider == "openai":
            return self._openai_enrich(frames, detections, actions, steps)
        return steps, ["多模态解释未启用，使用规则模板生成证据说明"]

    def _openai_enrich(
        self,
        frames: list[VideoFrame],
        detections: list[DetectionFact],
        actions: list[ActionFact],
        steps: list[StepFact],
    ) -> tuple[list[StepFact], list[str]]:
        if not self.settings.openai_api_key:
            return steps, ["OpenAI 多模态解释未配置 OPENAI_API_KEY，跳过"]
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.settings.openai_api_key)
            prompt = (
                "你是铁道供电作业实训考评助手。请基于检测对象、动作识别和步骤事件，"
                "生成简短、可审计的中文证据说明。不要新增没有证据支持的事实。"
            )
            summary = {
                "detections": [item.__dict__ for item in detections[:20]],
                "actions": [item.__dict__ for item in actions[:10]],
                "steps": [item.__dict__ for item in steps],
                "sampled_frames": [str(frame.path.name) for frame in frames[:4]],
            }
            response = client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"结构化证据：{summary}"},
                ],
            )
            text = getattr(response, "output_text", "")
            if text and steps:
                steps[0].evidence = f"{steps[0].evidence} 多模态说明：{text[:180]}"
            return steps, ["已调用 OpenAI 多模态解释生成证据说明"]
        except Exception as exc:
            return steps, [f"OpenAI 多模态解释失败，已回退模板说明：{exc}"]
