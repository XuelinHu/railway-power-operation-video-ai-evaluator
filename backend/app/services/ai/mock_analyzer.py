from pathlib import Path

from .contracts import ActionFact, AnalysisFacts, DetectionFact, StepFact
from .steps import STEP_NAMES


class MockAnalyzer:
    def analyze(self, video_path: Path, original_filename: str) -> AnalysisFacts:
        marker = original_filename.lower()
        is_bad = any(word in marker for word in ["bad", "error", "违规", "错误"])
        no_glove = is_bad or any(word in marker for word in ["no-glove", "noglove", "无手套"])
        skip_ground = any(word in marker for word in ["skip-ground", "noground", "未接地"])
        wrong_order = any(word in marker for word in ["wrong-order", "顺序错误"])

        detections = [
            DetectionFact("person", 0.98, 3.0, "0.20,0.12,0.42,0.86"),
            DetectionFact("safety_helmet", 0.94, 4.0, "0.25,0.05,0.38,0.18"),
            DetectionFact("voltage_detector", 0.88, 34.0, "0.58,0.22,0.68,0.48"),
            DetectionFact("ground_wire", 0.84, 50.0, "0.48,0.42,0.75,0.66"),
            DetectionFact("switch", 0.91, 68.0, "0.70,0.16,0.90,0.50"),
        ]
        if not no_glove:
            detections.append(DetectionFact("insulating_gloves", 0.92, 5.0, "0.30,0.32,0.45,0.52"))

        ordered_codes = [
            "ppe",
            "ticket",
            "tool_check",
            "power_off",
            "voltage_test",
            "ground_wire",
            "switch_operation",
            "review",
            "cleanup",
        ]
        if skip_ground and "ground_wire" in ordered_codes:
            ordered_codes.remove("ground_wire")
        if wrong_order:
            ordered_codes = [
                "ppe",
                "ticket",
                "tool_check",
                "power_off",
                "ground_wire",
                "voltage_test",
                "switch_operation",
                "review",
                "cleanup",
            ]
        if is_bad and "review" in ordered_codes:
            ordered_codes.remove("review")

        steps: list[StepFact] = []
        actions: list[ActionFact] = []
        for index, code in enumerate(ordered_codes):
            start = 6.0 + index * 10.0
            end = start + 6.0
            confidence = 0.90 if code in {"ppe", "voltage_test", "ground_wire"} else 0.82
            steps.append(
                StepFact(
                    step_code=code,
                    step_name=STEP_NAMES[code],
                    start_sec=start,
                    end_sec=end,
                    confidence=confidence,
                    evidence=f"在 {_format_time(start)} 到 {_format_time(end)} 识别到“{STEP_NAMES[code]}”相关动作。",
                )
            )
            actions.append(ActionFact(STEP_NAMES[code], confidence, start, end))

        return AnalysisFacts(detections=detections, actions=actions, steps=steps, notes=["使用 mock 分析器生成演示结果"])


def _format_time(seconds: float) -> str:
    minute = int(seconds // 60)
    second = int(seconds % 60)
    return f"{minute:02d}:{second:02d}"
