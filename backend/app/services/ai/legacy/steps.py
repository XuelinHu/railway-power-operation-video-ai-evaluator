STEP_NAMES = {
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


OBJECT_TO_STEP = {
    "safety_helmet": "ppe",
    "insulating_gloves": "ppe",
    "insulating_boots": "ppe",
    "work_ticket": "ticket",
    "operation_ticket": "ticket",
    "voltage_detector": "voltage_test",
    "ground_wire": "ground_wire",
    "switch": "switch_operation",
    "disconnect_switch": "switch_operation",
    "circuit_breaker": "switch_operation",
}


ACTION_TO_STEP = {
    "穿戴防护用品": "ppe",
    "确认工作票/操作票": "ticket",
    "检查工器具": "tool_check",
    "停电确认": "power_off",
    "验电": "voltage_test",
    "挂接地线": "ground_wire",
    "操作开关/隔离开关": "switch_operation",
    "复核状态": "review",
    "清理现场": "cleanup",
}
