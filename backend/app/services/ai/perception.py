"""Pass 1（感知）：让模型**只描述画面，不做判断**。

这里的设计意图值得说清楚，因为它看起来像是绕远路。

如果直接问模型"这 9 个步骤完成了吗"，会稳定地得到一份几乎全 `completed` 的报告。
原因不是模型能力不足，而是**一致性偏置**：一份"全部完成"的表格在它见过的语料里
是更常见、更"整洁"的输出。这个偏差可复现，且用 prompt 很难压住。

所以拆成两段。第一段只让它回答"你看到了什么"——有没有人、戴没戴手套、手里拿的什么。
这些都是**具体、可核验、互相独立**的观察，没有"整份表格该长什么样"的压力。
第二段拿这份观察表做纯文本判定，此时它无法引用观察表以外的事实，
证据链因此可以机械校验。

代价是调用次数从 1 次变成 4 次，成本约 3 倍，绝对量级仍是分/视频。
用钱换可靠性是这里唯一正确的选择。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ffmpeg import ExtractedFrame
from .vlm_client import VLMClient, VLMResult, encode_image
from .schema import WindowObservation, parse_window

# 每个时间窗送几帧。5-6 帧是"够看清一个人手上的动作"与"不超出模型注意力"的折中。
DEFAULT_WINDOW_COUNT = 3
MAX_FRAMES_PER_WINDOW = 6

WINDOW_TOOL_NAME = "submit_window_observation"

_SYSTEM_PROMPT = """你是铁道供电实训视频的画面观察员。

你的**唯一任务**是客观记录画面中实际可见的内容。你不需要判断操作是否规范，
不需要评价动作质量，不需要推测画面之外发生了什么。**只记录你看到的。**

逐帧填写，看不清就填 unclear。规则：

- **填 unclear 不会被追究，猜错才会。** 有遮挡、逆光、机位太远、画面模糊，
  一律如实填 unclear。
- 没看到人就说 person_count=0，不要为了"把表填满"而虚构作业人员。
- holding 只写画面里确实拿在手上的东西；空手就写"无"。
- action 用一句话描述可见的动作（如"弯腰接近接地线支架"），
  **不要**写"动作规范/不规范"这类评价。
- 不要推测这一步"应该"在做什么。只看画面。"""

_FRAME_FIELDS_HINT = """对每一帧返回这些字段：
- frame_index：帧号，必须是我标给你的帧号之一，不要自己编号
- person_fully_in_frame：作业人员身体是否完整入画（yes / no / unclear）
- helmet：是否佩戴安全帽（yes / no / unclear）
- insulating_gloves：是否佩戴绝缘手套（yes / no / unclear）
- insulating_boots：是否穿绝缘靴（yes / no / unclear）
- holding：手中拿的物品，没有就写"无"
- action：这一帧里人在做什么（一句话）
- note：影响判读的情况（遮挡、模糊、逆光等），没有就留空

另外在窗口级别返回：
- window_index：窗口序号
- camera_quality：整体画质（usable / partially_obscured / unusable）
- subject_count：本窗口中最多同时出现的作业人员数
- window_note：本窗口的整体情况说明"""


def _tool_schema() -> dict[str, Any]:
    """由 Pydantic 模型生成 function calling 的 JSON Schema。

    Pydantic 对嵌套模型会输出 $ref/$defs，而部分兼容模式实现不解析 $ref，
    会把参数当成空对象。所以这里把引用**摊平**成内联定义，
    并去掉对模型无意义的 title 字段。
    """
    schema = WindowObservation.model_json_schema()
    defs = schema.pop("$defs", {})
    return {
        "type": "function",
        "function": {
            "name": WINDOW_TOOL_NAME,
            "description": "提交本时间窗的逐帧客观观察记录。",
            "parameters": _inline_refs(schema, defs),
        },
    }


def _inline_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """递归展开 $ref 并剔除 title。depth 防御自引用导致的无限递归。"""
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


def split_windows(frame_count: int, window_count: int = DEFAULT_WINDOW_COUNT) -> list[tuple[int, int]]:
    """把帧切成若干连续时间窗，返回 0 基半开区间。

    16 帧切 3 窗 → (0,6) (6,11) (11,16)。前几窗多分一帧，
    保证每一窗都有足够的帧可看，而不是最后一窗只剩一两帧。
    """
    if frame_count <= 0:
        return []
    window_count = max(1, min(window_count, frame_count))
    base, remainder = divmod(frame_count, window_count)
    windows: list[tuple[int, int]] = []
    start = 0
    for index in range(window_count):
        size = base + (1 if index < remainder else 0)
        windows.append((start, start + size))
        start += size
    return windows


def _format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _build_messages(
    frames: list[ExtractedFrame], start: int, end: int, window_index: int
) -> list[dict[str, Any]]:
    """把帧和它的编号、时间拼成一条多模态消息。

    每张图前面都贴上"帧 N（MM:SS）"的文本标签。这一步不能省：
    模型看到一串图时无法自己知道编号，编号是我们后续做证据校验的唯一锚点。
    """
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"这是同一段实训视频按时间顺序抽取的第 {start + 1} 到第 {end} 帧"
                f"（共 {len(frames)} 帧中的第 {start + 1}~{end} 帧，本窗第 {window_index + 1} 窗）。"
                "相邻两张之间可能相隔数秒到数十秒，中间的动作没有被拍下来。\n\n"
                + _FRAME_FIELDS_HINT
            ),
        }
    ]
    for index in range(start, end):
        frame = frames[index]
        content.append(
            {
                "type": "text",
                "text": f"帧 {index + 1}（{_format_clock(frame.timestamp_sec)}）：",
            }
        )
        content.append({"type": "image_url", "image_url": {"url": encode_image(frame.path)}})

    content.append(
        {
            "type": "text",
            "text": (
                f"请逐帧记录以上 {end - start} 帧的观察结果，"
                f"帧号请使用 {list(range(start + 1, end + 1))}，"
                "然后调用工具提交。"
            ),
        }
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def observe(
    client: VLMClient,
    frames: list[ExtractedFrame],
    *,
    window_count: int = DEFAULT_WINDOW_COUNT,
    json_object: bool = False,
) -> tuple[list[WindowObservation], list[VLMResult]]:
    """跑完 Pass 1 的全部分窗，返回观察表与每次调用的用量明细。"""
    if not frames:
        raise ValueError("没有任何帧可供观察；调用方应当在此之前就判定失败。")

    allowed = set(range(1, len(frames) + 1))
    observations: list[WindowObservation] = []
    results: list[VLMResult] = []

    for window_index, (start, end) in enumerate(split_windows(len(frames), window_count)):
        result = client.chat(
            _build_messages(frames, start, end, window_index),
            tool=None if json_object else _tool_schema(),
            json_object=json_object,
            # 6 帧逐帧描述的输出量不小，给足预算；16 帧全片也不会超过这个数太多。
            max_tokens=2500,
        )
        results.append(result)
        observations.append(
            parse_window(
                result.payload,
                expected_window=window_index,
                allowed_frames=set(range(start + 1, end + 1)) & allowed,
            )
        )

    return observations, results


def render_observation_table(
    observations: list[WindowObservation], frames: list[ExtractedFrame]
) -> str:
    """把观察表渲染成 Pass 2 要读的文本表格。

    刻意用**纯文本表格**而不是 JSON：判定阶段读自然语言表格更稳，
    而且这份文本还会原样存进证据台账，人能直接读。
    """
    by_index: dict[int, Any] = {}
    for window in observations:
        for frame in window.frames:
            by_index[frame.frame_index] = frame

    lines = [
        "帧号 | 时间 | 人数 | 完整入画 | 安全帽 | 绝缘手套 | 绝缘靴 | 手中物品 | 动作 | 备注",
        "--- | --- | --- | --- | --- | --- | --- | --- | --- | ---",
    ]
    for index, frame in enumerate(frames, start=1):
        observed = by_index.get(index)
        clock = _format_clock(frame.timestamp_sec)
        if observed is None:
            lines.append(f"{index} | {clock} | - | - | - | - | - | - | （模型未给出本帧观察） | -")
            continue
        lines.append(
            " | ".join(
                [
                    str(index),
                    clock,
                    str(observed.person_count),
                    observed.person_fully_in_frame,
                    observed.helmet,
                    observed.insulating_gloves,
                    observed.insulating_boots,
                    observed.holding or "无",
                    observed.action or "-",
                    observed.note or "-",
                ]
            )
        )

    quality = "；".join(
        f"第 {window.window_index + 1} 窗画质={window.camera_quality}"
        + (f"（{window.window_note}）" if window.window_note else "")
        for window in observations
    )
    return "\n".join(lines) + f"\n\n画质说明：{quality}"
