"""反幻觉与标签一致性的测试。

这些测试覆盖的是**没有它们就会静默出错**的地方。共同特征是：
出问题时系统不会崩，而是安静地产出一份看着合理的错误结果——
全员满分、全员扣分、或者凭幻觉给学生扣分。这类 bug 在真实使用中
要等到有学生来申诉才会被发现。

测试不需要 API key、不联网、不碰数据库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ai import labels
from app.services.ai.schema import (
    LabelValidationError,
    parse_window,
    validate_judgment,
)
from app.services.ai.vlm_client import VLMResponseInvalid, _loads_lenient
from app.services.ai.perception import split_windows

ALL_FRAMES = set(range(1, 17))


def _judgment(**overrides) -> dict:
    """构造一份合法的 Pass 2 返回体，供各测试按需破坏其中一处。"""
    base = {
        "video_quality": "usable",
        "scene_summary": "作业人员在电杆旁进行验电操作。",
        "overall_note": "",
        "steps": [
            {
                "step_code": "ppe",
                "verdict": "completed",
                "confidence": 0.9,
                "evidence_frames": [1, 2],
                "reason": "画面中人员佩戴安全帽与绝缘手套。",
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 不变量 #4：标签词表单一来源
# ---------------------------------------------------------------------------


class TestLabelVocabulary:
    def test_rule_engine_does_not_reimplement_the_vocabulary(self):
        """规则引擎**不得**再做标签集合差。

        V1 的 PPE 判定是 `PPE_LABELS - detected_labels`。这个写法有个
        静默失效的模式：模型返回 `"安全帽"` 或 `"helmet"` 时集合差永远非空
        → 全员扣 15 分；返回 `"安全帽和绝缘手套"` 复合串时集合差为空
        → 全员满分。两种都不报错，都要等到学生来申诉才会被发现。

        现在的分工是：模型出**步骤判定**（含三态），规则引擎只算分。
        所以正确的不变量不是"两边共用同一个集合"，
        而是"规则引擎根本不碰标签集合"。
        """
        from app.services import rule_engine

        assert not hasattr(rule_engine, "PPE_LABELS"), (
            "规则引擎又出现了 PPE 标签集合——集合差判定不能回到评分链路里"
        )
        # 只看**代码**，不看模块文档字符串：文档里正解释着这个反模式叫什么，
        # 那是有意保留的记录，不该被自己的断言误伤。
        assert "detected_labels" not in _code_without_docstring(rule_engine)

    def test_rule_engine_consumes_verdicts(self):
        """规则引擎认的是三态判定，这是"看不见 ≠ 没做"的落地点。"""
        from app.services import rule_engine

        code = _code_without_docstring(rule_engine)
        assert "not_visible" in code
        assert "not_completed" in code


    @pytest.mark.parametrize("value", ["安全帽", "helmet", "safety helmet", "SafetyHelmet", ""])
    def test_rejects_non_vocabulary_labels(self, value):
        """模型返回的任何非枚举写法都必须被拒绝，而不是静默进入集合差。"""
        with pytest.raises(LabelValidationError):
            labels.validate_detection_label(value)

    def test_accepts_vocabulary_labels(self):
        for value in labels.PPE_LABELS:
            assert labels.validate_detection_label(value) == value

    def test_prompt_block_is_generated_from_vocabulary(self):
        """喂给 prompt 的枚举必须由词表生成。

        如果 prompt 里手写字符串，改词表时就会漏改 prompt，
        模型随即开始返回旧值——这正是要防的漂移。
        """
        block = labels.ppe_prompt_block()
        for value in labels.PPE_LABELS:
            assert value in block

    def test_all_step_names_have_codes(self):
        assert len(labels.STEP_CODES) == 9
        assert len(set(labels.STEP_CODES)) == 9
        for code in labels.STEP_CODES:
            assert labels.STEP_NAMES[code]


# ---------------------------------------------------------------------------
# 反幻觉：帧号校验
# ---------------------------------------------------------------------------


class TestFrameValidation:
    def test_completed_without_evidence_is_downgraded(self):
        """判 completed 却指不出证据 → 降级，而不是采信。

        这是整个反幻觉设计的支点。没有它，模型可以随手把每一步都判成
        completed 而无需承担任何后果，学生被凭空扣分也无法追溯。
        """
        payload = _judgment(
            steps=[
                {
                    "step_code": "ppe",
                    "verdict": "completed",
                    "confidence": 0.9,
                    "evidence_frames": [],
                    "reason": "看起来戴了。",
                }
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.verdict == "not_visible"
        assert any("无有效证据帧" in note for note in ppe.validation_notes)

    def test_hallucinated_frame_numbers_are_stripped(self):
        """引用不存在的帧号 = 幻觉的直接证据，必须剔除并留痕。"""
        payload = _judgment(
            steps=[
                {
                    "step_code": "ppe",
                    "verdict": "completed",
                    "confidence": 0.9,
                    "evidence_frames": [3, 999, 1000],
                    "reason": "…",
                }
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.evidence_frames == [3]
        assert any("999" in note for note in ppe.validation_notes)

    def test_completed_with_only_fake_frames_is_downgraded(self):
        """证据全是编的 → 剔除后无证据 → 同样降级。"""
        payload = _judgment(
            steps=[
                {
                    "step_code": "ppe",
                    "verdict": "completed",
                    "confidence": 0.9,
                    "evidence_frames": [777],
                    "reason": "…",
                }
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.verdict == "not_visible"

    def test_not_visible_needs_no_evidence(self):
        """not_visible 是"看不见"，本就拿不出证据，不该因此被降级。"""
        payload = _judgment(
            steps=[
                {
                    "step_code": "ppe",
                    "verdict": "not_visible",
                    "confidence": 0.8,
                    "evidence_frames": [],
                    "reason": "人员始终背对镜头。",
                }
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.verdict == "not_visible"


# ---------------------------------------------------------------------------
# 反幻觉：完整性
# ---------------------------------------------------------------------------


class TestCompleteness:
    def test_unknown_step_codes_are_ignored_not_fatal(self):
        payload = _judgment(
            steps=[
                {"step_code": "ppe", "verdict": "completed", "confidence": 0.9,
                 "evidence_frames": [1], "reason": "…"},
                {"step_code": "action_0", "verdict": "completed", "confidence": 0.9,
                 "evidence_frames": [1], "reason": "遗留适配器的输出格式"},
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        assert all(step.step_code != "action_0" for step in outcome.steps)
        assert any("action_0" in warning for warning in outcome.warnings)

    def test_missing_steps_are_filled_as_not_visible(self):
        """模型漏答的步骤必须补成 not_visible + needs_review。

        **绝不能沉默跳过**：少一步就等于那一步没被判过，而规则引擎的
        step_required 会把"查无此步"当成"没做"，直接扣分。
        """
        outcome = validate_judgment(_judgment(), allowed_frames=ALL_FRAMES, review_confidence=0.6)
        assert len(outcome.steps) == 9
        assert {step.step_code for step in outcome.steps} == set(labels.STEP_CODES)

        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.verdict == "completed"

        ticket = next(step for step in outcome.steps if step.step_code == "ticket")
        assert ticket.verdict == "not_visible"
        assert ticket.needs_review
        assert outcome.needs_review

    def test_all_invisible_flags_for_review(self):
        """整片全不可见 → 必须转人工，且不得产生分数。

        这条防的是机位完全拍废的情况：机器判不出任何东西时，
        若照常算分就会得出"0 分"，等于把拍摄问题记在学生头上。
        """
        payload = _judgment(
            steps=[
                {"step_code": "ppe", "verdict": "not_visible", "confidence": 0.8,
                 "evidence_frames": [], "reason": "人员全程背对镜头。"}
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        assert all(step.verdict == "not_visible" for step in outcome.steps)
        assert outcome.needs_review
        assert any("全部步骤均不可见" in reason for reason in outcome.review_reasons)

    def test_empty_step_list_is_flagged(self):
        """模型一个步骤都没返回（结构性失败）→ 全部补齐并转人工，绝不当成"全没做"。"""
        outcome = validate_judgment(_judgment(steps=[]), allowed_frames=ALL_FRAMES,
                                    review_confidence=0.6)
        assert len(outcome.steps) == 9
        assert all(step.verdict == "not_visible" for step in outcome.steps)
        assert outcome.needs_review

    def test_unusable_video_flags_for_review(self):
        payload = _judgment(video_quality="unusable")
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        assert outcome.needs_review
        assert any("画面质量" in reason for reason in outcome.review_reasons)

    def test_low_confidence_flags_for_review(self):
        payload = _judgment(
            steps=[
                {"step_code": "ppe", "verdict": "completed", "confidence": 0.3,
                 "evidence_frames": [1], "reason": "…"}
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = next(step for step in outcome.steps if step.step_code == "ppe")
        assert ppe.needs_review
        assert outcome.needs_review

    def test_duplicate_steps_keep_first(self):
        payload = _judgment(
            steps=[
                {"step_code": "ppe", "verdict": "completed", "confidence": 0.9,
                 "evidence_frames": [1], "reason": "第一次"},
                {"step_code": "ppe", "verdict": "not_completed", "confidence": 0.9,
                 "evidence_frames": [2], "reason": "第二次"},
            ]
        )
        outcome = validate_judgment(payload, allowed_frames=ALL_FRAMES, review_confidence=0.6)
        ppe = [s for s in outcome.steps if s.step_code == "ppe"]
        assert len(ppe) == 1
        assert ppe[0].reason == "第一次"


# ---------------------------------------------------------------------------
# Pass 1 的窗内帧号校验
# ---------------------------------------------------------------------------


class TestWindowParsing:
    def test_out_of_window_frames_dropped(self):
        """模型把帧号编到窗口外 → 丢弃该帧，但不毁掉整窗观察。"""
        payload = {
            "window_index": 0,
            "camera_quality": "usable",
            "subject_count": 1,
            "frames": [
                {"frame_index": 1, "helmet": "yes", "insulating_gloves": "yes"},
                {"frame_index": 2, "helmet": "no", "insulating_gloves": "unclear"},
                {"frame_index": 99, "helmet": "yes"},  # 越界
            ],
            "window_note": "",
        }
        window = parse_window(payload, expected_window=0, allowed_frames={1, 2})
        assert [frame.frame_index for frame in window.frames] == [1, 2]
        assert "99" in window.window_note

    def test_unclear_is_a_valid_value(self):
        """unclear 必须可表达。没有这个选项，模型只能在 yes/no 里猜。"""
        payload = {
            "window_index": 0,
            "frames": [{"frame_index": 1, "helmet": "unclear", "insulating_gloves": "unclear"}],
        }
        window = parse_window(payload, expected_window=0, allowed_frames={1})
        assert window.frames[0].helmet == "unclear"

    def test_invalid_enum_value_is_rejected(self):
        """非法枚举值必须报错，而不是被静默接受成某个默认值。"""
        payload = {
            "window_index": 0,
            "frames": [{"frame_index": 1, "helmet": "大概戴了"}],
        }
        with pytest.raises(Exception):
            parse_window(payload, expected_window=0, allowed_frames={1})


# ---------------------------------------------------------------------------
# 脏输出解析
# ---------------------------------------------------------------------------


class TestLenientParsing:
    def test_strips_markdown_fence(self):
        assert _loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}

    def test_strips_surrounding_prose(self):
        assert _loads_lenient('好的，结果如下：{"b": 2} 以上。') == {"b": 2}

    def test_rejects_non_object(self):
        with pytest.raises(VLMResponseInvalid):
            _loads_lenient("[1, 2, 3]")

    def test_rejects_garbage(self):
        with pytest.raises(VLMResponseInvalid):
            _loads_lenient("我无法完成这个任务。")


# ---------------------------------------------------------------------------
# 时间窗切分
# ---------------------------------------------------------------------------


class TestWindowing:
    def test_windows_cover_all_frames_exactly_once(self):
        for count in (4, 5, 16, 17, 23):
            windows = split_windows(count, 3)
            covered: list[int] = []
            for start, end in windows:
                covered.extend(range(start, end))
            assert covered == list(range(count)), f"{count} 帧时切分有误：{windows}"

    def test_window_count_capped_by_frame_count(self):
        assert len(split_windows(2, 3)) == 2

    def test_empty_frames(self):
        assert split_windows(0, 3) == []


def _code_without_docstring(module) -> str:
    """模块的源码，去掉注释与文档字符串。

    放在文件末尾而不是中间：夹在类定义中间会把后面的方法挤出类体，
    变成 pytest 收集不到的模块级函数——测试会静默少跑，而输出仍然是"全绿"。
    """
    import ast
    import io
    import tokenize

    source = Path(module.__file__).read_text(encoding="utf-8")

    stripped: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            stripped.append(token.string)
    code = "\n".join(stripped)

    docstring = ast.get_docstring(ast.parse(source), clean=False)
    if docstring:
        code = code.replace(docstring, "", 1)
    return code
