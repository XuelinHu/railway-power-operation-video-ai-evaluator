"""数据模型。

V1（现有）→ V2（本次上线）的字段变更都记在各自的注释里，
对应的 DDL 在 `scripts/v1_to_v2.sql`。

**为什么必须有那份 SQL**：SQLModel 的 `create_all` **只建新表，不给已有表加列**。
第二次部署时它不会报错，而是在第一次查询时抛 `no such column`——
这是第一次升级最容易翻车的地方。
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from .clock import now


class AnalysisStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class UserRole(str, Enum):
    admin = "admin"
    teacher = "teacher"
    student = "student"


class ReviewStatus(str, Enum):
    pending = "pending"            # 机器出了分，教师还没看
    confirmed = "confirmed"        # 教师已终审，分数锁定
    needs_review = "needs_review"  # 机器自己觉得不可靠，必须人工看


class ViolationStatus(str, Enum):
    auto = "auto"            # 机器判的，未被教师处理
    confirmed = "confirmed"  # 教师确认扣分
    dismissed = "dismissed"  # 教师驳回（误判）


class TaskStatus(str, Enum):
    open = "open"
    closed = "closed"


# ---------------------------------------------------------------------------
# 账号与会话
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: UserRole = Field(default=UserRole.student, index=True)
    display_name: str = ""
    student_no: str = Field(default="", index=True)
    class_name: str = ""
    is_active: bool = True
    # 管理员新建账号、或教师重置密码后置 True，强制本人首次登录改密。
    # 否则"教师知道学生密码"这件事会一直存在，学生可以用"不是我做的"抗辩。
    must_change_password: bool = True
    created_at: datetime = Field(default_factory=now)
    last_login_at: Optional[datetime] = None


class UserSession(SQLModel, table=True):
    """服务端会话。

    **为什么不用 JWT**：(a) 每个请求本来就要查库；(b) JWT 无法在改密码/停用账号/
    改角色时立即失效，而学校场景里管理员停用账号必须立刻生效；
    (c) 代码更少，还白得"当前在线用户/强制下线"。

    表名避开 SQL 保留字 `session`。
    """

    __tablename__ = "user_session"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 存哈希而不是明文 token：库被读走也无法直接拿来登录。
    token_hash: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime = Field(index=True)
    last_seen_at: datetime = Field(default_factory=now)
    revoked: bool = Field(default=False, index=True)


# ---------------------------------------------------------------------------
# 教学组织
# ---------------------------------------------------------------------------


class TrainingTask(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    course: str
    class_name: str
    teacher: str
    description: str = ""
    # V2 新增：建任务的教师。教师间可见性目前是"全员可见"（已确认），
    # 但字段先留着——事后要隔离时，加权限判断是 1 小时的事，
    # 事后补数据归属是 1 天的事。
    owner_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    status: TaskStatus = Field(default=TaskStatus.open, index=True)
    due_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class RosterEntry(SQLModel, table=True):
    """名册条目：任务的应参加学生名单。

    V2 新增。**这是防冒名与成绩对账的前提**：V1 里 `student_name`/`student_no`
    是自由文本表单字段，会出现同一学号多种写法、错字造出幽灵学生，
    导致成绩对账和防冒名提交全部无从谈起。

    学生登录后绑定到名册条目再上传，未在名册内的学生上传会被拒绝。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="trainingtask.id", index=True)
    student_no: str = Field(index=True)
    student_name: str
    # 绑定后指向实际账号；为空表示"名单里有但还没绑定账号"
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=now)


class AuditLog(SQLModel, table=True):
    """审计留痕。

    改分是这套系统里最敏感的写操作——它直接决定学生成绩。
    "谁、在什么时候、把什么、从多少改成了多少"必须可追溯，
    否则一次误判就会变成一场说不清的纠纷。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    action: str = Field(index=True)
    target_type: str = ""
    target_id: Optional[int] = None
    detail: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=now, index=True)


# ---------------------------------------------------------------------------
# 作业提交与分析
# ---------------------------------------------------------------------------


class VideoSubmission(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="trainingtask.id", index=True)
    student_name: str
    student_no: str = ""
    original_filename: str
    stored_filename: str
    content_type: str = ""
    size_bytes: int = 0
    # V2 新增
    student_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    roster_entry_id: Optional[int] = Field(default=None, foreign_key="rosterentry.id", index=True)
    duration_sec: Optional[float] = None
    # 内容指纹。同名不同内容的重复提交、以及"到底是不是同一个文件"的争议，
    # 都靠它说清楚。
    sha256: str = Field(default="", index=True)
    # pending / running / done / failed：转码状态。转码解决两件事：
    # HEVC 在 Windows Chrome 里播不出，以及给抽帧一个规范化的输入。
    transcode_state: str = Field(default="pending", index=True)
    transcode_error: str = ""
    uploaded_at: datetime = Field(default_factory=now)


class AnalysisJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    submission_id: int = Field(foreign_key="videosubmission.id", index=True)
    status: AnalysisStatus = Field(default=AnalysisStatus.pending, index=True)

    # V2：score 改为**可空**。
    # 不变量 #1：没有证据支撑时 score 必须是空——永远不能是 100，也不能是 0。
    # V1 里它默认 0 且失败路径不重置，导致分析失败的作业在前端显示"0 分"，
    # 等于无故指控学生。
    score: Optional[float] = None

    summary: str = Field(default="", sa_column=Column(Text))
    # 内部排障用，**不投给前端**（可能带绝对路径）。
    error_message: str = Field(default="", sa_column=Column(Text))
    # 教师可读的失败原因（已脱敏）。
    error_code: str = Field(default="", index=True)

    # V2 新增：进度与 worker 协作
    progress: int = 0     # 0-100
    stage: str = ""       # 面向教师的中文阶段名
    worker_id: str = Field(default="", index=True)
    # 双心跳。最常见的卡死形态是**进程活着但卡在 ffmpeg 或 VLM 连接上**，
    # 只看"进程死没死"抓不到；progress_at 记录任务是否真的在推进。
    heartbeat_at: Optional[datetime] = Field(default=None, index=True)
    progress_at: Optional[datetime] = Field(default=None, index=True)
    attempts: int = 0

    # V2 新增：成本与可审计台账
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_ids: str = Field(default="", sa_column=Column(Text))

    created_at: datetime = Field(default_factory=now, index=True)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class DetectionResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    label: str
    confidence: float
    timestamp_sec: float
    # VLM 路径不产 bbox，保留字段以兼容旧数据，新链路一律留空。
    bbox: str = ""


class ActionResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    action: str
    confidence: float
    start_sec: float
    end_sec: float


class StepEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    step_code: str = Field(index=True)
    step_name: str
    # V2：start_sec 改为可空。
    # `not_visible` 的步骤没有时间——"看不见"本就指不出第几秒。
    # 而 sequence 规则是拿 start_sec 比大小，必须把这类步骤排除在顺序比较之外，
    # 否则就是在拿 None 比大小。
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    confidence: float = 0.0
    evidence: str = Field(default="", sa_column=Column(Text))

    # V2 新增
    # 支撑该判定的帧号（JSON 数组）。这是反幻觉的核心：
    # 每个"完成"都必须有具体所指，而不是一句空口断言。
    evidence_frames: list = Field(default_factory=list, sa_column=Column(JSON))
    source: str = Field(default="vlm", index=True)   # vlm | human
    verdict: str = Field(default="", index=True)     # completed|not_completed|not_visible|not_applicable
    needs_review: bool = Field(default=False, index=True)
    validation_note: str = Field(default="", sa_column=Column(Text))


class Violation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    rule_code: str
    title: str
    deduction: float
    severity: str
    timestamp_sec: Optional[float] = None
    reason: str = Field(default="", sa_column=Column(Text))
    suggestion: str = Field(default="", sa_column=Column(Text))

    # V2 新增：教师复核
    status: ViolationStatus = Field(default=ViolationStatus.auto, index=True)
    reviewer_id: Optional[int] = Field(default=None, foreign_key="user.id")
    review_comment: str = Field(default="", sa_column=Column(Text))
    reviewed_at: Optional[datetime] = None


class EvaluationReport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True, unique=True)
    # 机器分。教师改判**不覆盖**它——两个分数都要留着，
    # 否则事后无法回答"到底改了多少、改了什么"。
    score: Optional[float] = None
    conclusion: str = Field(default="", sa_column=Column(Text))
    strengths: str = Field(default="", sa_column=Column(Text))
    problems: str = Field(default="", sa_column=Column(Text))
    suggestions: str = Field(default="", sa_column=Column(Text))
    generated_at: datetime = Field(default_factory=now)

    # V2 新增：终分与终审
    final_score: Optional[float] = None
    review_status: ReviewStatus = Field(default=ReviewStatus.pending, index=True)
    reviewer_id: Optional[int] = Field(default=None, foreign_key="user.id")
    reviewed_at: Optional[datetime] = None
    review_comment: str = Field(default="", sa_column=Column(Text))


class ReviewRequest(SQLModel, table=True):
    """学生的"申请复核"。

    哪怕只做到"按钮 + 一条记录"也要有：第一次误判时，
    学生的申诉路径不该是"去找系主任"。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    student_id: int = Field(foreign_key="user.id", index=True)
    message: str = Field(default="", sa_column=Column(Text))
    resolved: bool = Field(default=False, index=True)
    resolver_id: Optional[int] = Field(default=None, foreign_key="user.id")
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# 标准与规则（参考数据）
# ---------------------------------------------------------------------------


class StandardStep(SQLModel, table=True):
    """标准作业步骤。9 步的**权威定义在 `services/ai/labels.py`**。

    这张表是给界面展示用的副本（教师能看到"标准流程是哪 9 步"），
    不参与判定——判定用的是 labels.py 的常量。
    两份数据源是有意的取舍：让界面不必 import 代码常量，
    代价是改步骤时要同时改两处（`labels.py` 与 `seed.py`），
    所以 `tests/` 里有一条断言两者一致。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str
    order_index: int = 0
    description: str = Field(default="", sa_column=Column(Text))


class ScoringRule(SQLModel, table=True):
    """评分规则。

    `rule_type` 决定怎么用 `target_code`：

    - `step_required` / `detection_required`：`target_code` 是**步骤代码**，
      判定为该步 `not_completed` 时扣分。注意 `detection_required` 是 V1 的
      遗留类型名，语义已并入 `step_required`（见 rule_engine 的说明）。
    - `sequence`：`target_code` 形如 `"voltage_test>ground_wire"`，
      表示前者必须先于后者发生。

    `deduction` 是扣分值，不是权重。规则引擎的算式是
    `100 - Σ(生效扣分)`，简单到教师可以手算核对——这很重要，
    因为学生问"为什么是 82 分"时，教师必须能当场解释。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    title: str
    rule_type: str = Field(index=True)
    target_code: str
    deduction: float = 0.0
    severity: str = "medium"
    description: str = Field(default="", sa_column=Column(Text))


class KnowledgeDocument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    category: str = "规程"
    content: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=now)


class BudgetCounter(SQLModel, table=True):
    """每日 VLM 调用预算闸门。

    真正要防的不是单价，是**失控放大**：一次重试风暴、或有人点了"批量重跑全库"，
    就能把成本放大几十倍。超限时新任务直接判失败，而不是继续烧钱。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True, unique=True)  # YYYY-MM-DD（本地时间）
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
