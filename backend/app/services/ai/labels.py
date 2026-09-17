"""标签词表的单一来源（single source of truth）。

**为什么这个文件必须存在**：`rule_engine` 判定防护用品用的是集合差

    missing = PPE_LABELS - detected_labels

只要模型返回的字符串和 `PPE_LABELS` 里的任何一个不完全相等，就会落进 `missing`。
后果是静默且对称的灾难：

- 模型返回 `"安全帽"` / `"helmet"` / `"safety helmet"` → 集合差永远非空 → **每个人扣 15 分**
- 模型返回 `"安全帽和绝缘手套"` 这种复合串 → 集合差为空 → **每个人满分**

两种都在旧代码里真实发生过（`action_recognizer.py` 输出 `action_0`，与中文动作映射表
完全对不上）。所以在 VLM 链路上必须做到两件事：

1. **喂给 prompt 的枚举值就是这里定义的常量**，不允许在 prompt 里手写字符串。
2. **模型返回值必须经过 `validate_*` 校验**，不在枚举内的一律拒绝或降级，
   绝不允许带着未知标签进入数据库。

修改本文件时请同步检查 `rule_engine.py` 的判定分支。
"""

from __future__ import annotations

# --- 标准步骤（9 步，与 seed.py 的 DEFAULT_STEPS 保持一致）--------------------

STEP_NAMES: dict[str, str] = {
    "ppe": "穿戴防护用品",
    "ticket": "确认工作票/操作票",
    "tool_check": "检查工器具",
    "power_off": "停电确认",
    "voltage_test": "验电",
    "ground_wire": "挂接地线",
    "switch_operation": "操作开关/隔离开关",
    "review": "复核状态",
    "cleanup": "清理现场",
}

STEP_CODES: tuple[str, ...] = tuple(STEP_NAMES)

# --- 防护用品标签 -------------------------------------------------------------
# 这三个是检测事实里唯一被规则引擎消费的对象标签，必须与 VLM prompt 的枚举一致。
PPE_LABELS: frozenset[str] = frozenset({"safety_helmet", "insulating_gloves"})

# 绝缘靴在规则里不参与扣分（机位常拍不到脚部），但保留在词表中，
# 供 Pass 1 感知阶段记录，避免模型自由发挥出别的写法。
PPE_OPTIONAL_LABELS: frozenset[str] = frozenset({"insulating_boots"})

# 检测事实允许出现的全部对象标签。不在此列的一律拒绝入库。
DETECTION_LABELS: frozenset[str] = PPE_LABELS | PPE_OPTIONAL_LABELS | frozenset(
    {"person", "voltage_detector", "ground_wire", "switch", "work_ticket", "tool"}
)

# 面向教师的中文名。扣分理由里绝不能出现 safety_helmet 这种标识符 ——
# 那是给机器看的，投到报告里等于让老师去猜。
LABEL_DISPLAY_NAMES: dict[str, str] = {
    "safety_helmet": "安全帽",
    "insulating_gloves": "绝缘手套",
    "insulating_boots": "绝缘靴",
    "person": "作业人员",
    "voltage_detector": "验电器",
    "ground_wire": "接地线",
    "switch": "开关/隔离开关",
    "work_ticket": "工作票",
    "tool": "工器具",
}


def display_name(label: str) -> str:
    """把标签翻成中文；词表外的值原样返回，便于在报告里暴露问题而不是掩盖。"""
    return LABEL_DISPLAY_NAMES.get(label, label)

# --- 判定与观察枚举 -----------------------------------------------------------

# 步骤判定的四态。not_visible 是一等公民：感知源换成会看走眼的 VLM 之后，
# 「看不见」和「没做」是两件完全不同的事，前者不该自动扣分，应转人工复核。
VERDICTS: frozenset[str] = frozenset({"completed", "not_completed", "not_visible", "not_applicable"})

# Pass 1 每帧的三态观察。unclear 必须能被表达出来，否则模型只会二选一。
TRISTATE: frozenset[str] = frozenset({"yes", "no", "unclear"})

CAMERA_QUALITY: frozenset[str] = frozenset({"usable", "partially_obscured", "unusable"})

# 低于该置信度的判定要打上 needs_review，提示教师重点看。
REVIEW_CONFIDENCE_THRESHOLD = 0.6


class LabelValidationError(ValueError):
    """模型返回了词表之外的值。"""


def validate_step_code(value: str) -> str:
    if value not in STEP_NAMES:
        raise LabelValidationError(f"未知步骤码：{value!r}（合法值：{', '.join(STEP_CODES)}）")
    return value


def validate_verdict(value: str) -> str:
    if value not in VERDICTS:
        raise LabelValidationError(f"未知判定：{value!r}（合法值：{', '.join(sorted(VERDICTS))}）")
    return value


def validate_tristate(value: str) -> str:
    if value not in TRISTATE:
        raise LabelValidationError(f"未知观察值：{value!r}（合法值：{', '.join(sorted(TRISTATE))}）")
    return value


def validate_detection_label(value: str) -> str:
    if value not in DETECTION_LABELS:
        raise LabelValidationError(
            f"未知检测标签：{value!r}。若确需新增类别，请先在本文件登记，"
            "否则规则引擎的集合差判定会静默失效。"
        )
    return value


def steps_prompt_block() -> str:
    """生成喂给 prompt 的步骤清单。**不要在 prompt 里手写步骤名**，统一从这里取。"""
    return "\n".join(f"- {code}：{name}" for code, name in STEP_NAMES.items())


def ppe_prompt_block() -> str:
    """生成喂给 prompt 的防护用品枚举，确保模型输出的字符串与词表完全一致。"""
    return "、".join(sorted(PPE_LABELS))
