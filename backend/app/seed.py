from sqlmodel import Session, select

from .models import KnowledgeDocument, ScoringRule, StandardStep


DEFAULT_STEPS = [
    ("ppe", "穿戴防护用品", "佩戴安全帽、绝缘手套、绝缘靴等个人防护用品。"),
    ("ticket", "确认工作票/操作票", "核对作业任务、人员、范围和安全措施。"),
    ("tool_check", "检查工器具", "检查验电器、接地线、操作杆等工器具状态。"),
    ("power_off", "停电确认", "确认设备处于停电状态并完成安全确认。"),
    ("voltage_test", "验电", "使用合格验电器对目标设备进行验电。"),
    ("ground_wire", "挂接地线", "验电后按规定装设接地线。"),
    ("switch_operation", "操作开关/隔离开关", "按操作票顺序完成开关、刀闸或隔离开关操作。"),
    ("review", "复核状态", "复核设备状态、接地线状态和现场安全状态。"),
    ("cleanup", "清理现场", "清点工具、撤离人员、恢复现场秩序。"),
]

DEFAULT_RULES = [
    ("missing_ppe", "未完整穿戴防护用品", "detection_required", "ppe", 15, "high", "进入作业区域前必须完成个人防护用品穿戴。"),
    ("missing_ticket", "未确认工作票/操作票", "step_required", "ticket", 10, "medium", "操作前应核对工作票或操作票。"),
    ("missing_voltage_test", "缺失验电步骤", "step_required", "voltage_test", 20, "critical", "挂接地线和设备操作前必须验电。"),
    ("missing_ground_wire", "缺失挂接地线步骤", "step_required", "ground_wire", 20, "critical", "验电后应按规定挂接地线。"),
    ("wrong_sequence_ground_before_test", "挂接地线早于验电", "sequence", "voltage_test>ground_wire", 25, "critical", "挂接地线必须发生在验电之后。"),
    ("missing_review", "未复核设备状态", "step_required", "review", 8, "medium", "操作完成后应复核设备和现场状态。"),
]

DEFAULT_DOCS = [
    (
        "铁道供电作业评分总则",
        "评分细则",
        "评价应以视频识别事实为基础，规则评分为硬性依据，教师复核为最终确认。安全防护、验电、接地线、操作顺序属于重点扣分项。",
    ),
    (
        "停电验电接地操作要求",
        "作业规程",
        "作业人员应先确认停电，再完成验电。确认无电后方可装设接地线，严禁未验电或验电前挂接地线。",
    ),
    (
        "个人防护用品要求",
        "安全规范",
        "进入铁道供电实训操作区域前，应按要求佩戴安全帽、绝缘手套、绝缘靴等防护用品。",
    ),
]


def seed_defaults(session: Session) -> None:
    if not session.exec(select(StandardStep)).first():
        for index, (code, name, description) in enumerate(DEFAULT_STEPS, start=1):
            session.add(StandardStep(code=code, name=name, order_index=index, description=description))

    if not session.exec(select(ScoringRule)).first():
        for code, title, rule_type, target_code, deduction, severity, description in DEFAULT_RULES:
            session.add(
                ScoringRule(
                    code=code,
                    title=title,
                    rule_type=rule_type,
                    target_code=target_code,
                    deduction=deduction,
                    severity=severity,
                    description=description,
                )
            )

    if not session.exec(select(KnowledgeDocument)).first():
        for title, category, content in DEFAULT_DOCS:
            session.add(KnowledgeDocument(title=title, category=category, content=content))

    session.commit()
