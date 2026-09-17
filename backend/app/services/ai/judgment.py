"""Pass 2（判定）：拿 Pass 1 的观察表做**纯文本**判定，不看图。

为什么不接着看图判：边看图边填 9 步表格时，模型会把"这份表格该长什么样"
的预期带进来，倾向于全填 completed。换成读一份已经写好的观察表，
它的任务变成了"依据这些事实做推断"——一个它做得可靠得多的任务。

更关键的是证据链：纯文本判定**不可能引用观察表里没有的事实**，
所以每个判定引用的帧号都可以机械校验（见 schema.validate_judgment）。

**本模块绝不输出分数。** 分数永远由 rule_engine 计算。
模型只回答"这一步做了没有"，不回答"这值多少分"。这条边界一旦破了，
分数就变得不可复现、不可审计、不可解释——而它是要进教务系统的。
"""

from __future__ import annotations

from typing import Any

from .labels import STEP_NAMES, steps_prompt_block
from .schema import JudgmentOutcome, validate_judgment
from .vlm_client import VLMClient, VLMResult

JUDGMENT_TOOL_NAME = "submit_step_judgment"

_SYSTEM_PROMPT = """你是铁道供电作业实训的考评专家。

你会收到一份**由画面前方观察员填写的逐帧观察记录**。你没有看过视频本身，
只能依据这份记录作答。这是刻意的设计：**记录中没有的事实，你不知道，也不许推测。**

你的职责是判断 9 个标准步骤各自属于以下哪一种：

- **completed**：观察记录中有明确证据表明该步骤完成了
- **not_completed**：观察记录中有明确证据表明该做的动作没有做
  （例如：人的双手清晰可见，但没有戴绝缘手套）
- **not_visible**：观察记录不足以判断该步骤是否完成
- **not_applicable**：该步骤在本次作业中本就不适用

## 关于 not_visible（请务必读完）

实训视频受机位限制，**通常会有若干步骤拍不到**，这是拍摄条件决定的，不是异常。
如实标注 not_visible 是**正确且有价值**的回答。

请特别注意：
- 观察记录里某一步骤相关的动作根本没出现过 → 那是 not_visible，不是 not_completed。
  "没看到"和"没做"是两件完全不同的事。
- 画面太远、被遮挡、关键部位不在画内 → not_visible。
- **把看不清的步骤写成 completed 是严重错误**：它会让一个实际没被验证的步骤
  变成"已确认完成"，掩盖真实问题。反过来，把看不清的写成 not_completed
  会让学生被无故扣分。两种情况都要避免。

## 证据要求（会被程序校验）

- 判 **completed** 或 **not_completed** 时，必须在 evidence_frames 里给出
  支撑该判定的帧号。这些帧号**必须真实存在于我给你的观察记录中**，
  引用不存在的帧号会被程序剔除，判定随之降级。
- 判 **not_visible** 时，evidence_frames 留空。

只输出判定，**不要输出分数、等级或名次**。分数由考核规则另行计算。"""


def _tool_schema() -> dict[str, Any]:
    from .schema import VideoJudgment

    schema = VideoJudgment.model_json_schema()
    defs = schema.pop("$defs", {})
    return {
        "type": "function",
        "function": {
            "name": JUDGMENT_TOOL_NAME,
            "description": "提交 9 个标准步骤的判定结果。",
            "parameters": _inline_refs(schema, defs),
        },
    }


def _inline_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 12:
        return {}
    if isinstance(node, dict):
        if "$ref" in node:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            merged = dict(defs.get(name, {}))
            merged.update({key: value for key, value in node.items() if key != "$ref"})
            return _inline_refs(merged, defs, depth + 1)
        return {
            key: _inline_refs(value, defs, depth + 1)
            for key, value in node.items()
            if key != "title"
        }
    if isinstance(node, list):
        return [_inline_refs(item, defs, depth + 1) for item in node]
    return node


def _build_messages(observation_table: str, frame_count: int) -> list[dict[str, Any]]:
    step_block = steps_prompt_block()
    codes = ", ".join(STEP_NAMES)
    user = f"""## 逐帧观察记录

{observation_table}

## 标准步骤（step_code：名称）

{step_block}

## 要求

对上面全部 9 个步骤逐一给出判定，step_code 只能使用：{codes}。
每个步骤都要出现，不要遗漏，也不要重复。

判断安全帽/绝缘手套这类防护用品时请依据观察记录中的对应列：
- 只要**至少有一帧**明确看到作业人员佩戴 → completed（并把该帧号写进证据）
- 明确看到未佩戴（如双手清晰入画但没有手套）→ not_completed
- 全部为 unclear，或人员始终未完整入画 → not_visible

另外请给出：
- video_quality：整体画质（usable / partially_obscured / unusable）
- scene_summary：一句话概括这段作业在做什么（给教师看的，不要写评价）
- overall_note：需要教师注意的情况

再次强调：观察记录里**没有出现过的动作**，不要判成 completed，
也不要判成 not_completed，应判 not_visible。"""

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def judge(
    client: VLMClient,
    observation_table: str,
    *,
    frame_count: int,
    review_confidence: float,
    json_object: bool = False,
) -> tuple[JudgmentOutcome, VLMResult]:
    """跑 Pass 2 并做确定性校验。返回 (判定结果, 调用明细)。"""
    result = client.chat(
        _build_messages(observation_table, frame_count),
        tool=None if json_object else _tool_schema(),
        json_object=json_object,
        # 9 步判定 + 理由，输出量比 Pass 1 大
        max_tokens=3000,
    )
    outcome = validate_judgment(
        result.payload,
        allowed_frames=set(range(1, frame_count + 1)),
        review_confidence=review_confidence,
    )
    return outcome, result
