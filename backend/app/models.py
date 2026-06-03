from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class AnalysisStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class TrainingTask(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    course: str
    class_name: str
    teacher: str
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StandardStep(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str
    order_index: int = Field(index=True)
    description: str = ""
    required: bool = True


class ScoringRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    title: str
    rule_type: str
    target_code: str = ""
    deduction: float
    severity: str = "medium"
    description: str = ""


class VideoSubmission(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="trainingtask.id", index=True)
    student_name: str
    student_no: str = ""
    original_filename: str
    stored_filename: str
    content_type: str = ""
    size_bytes: int = 0
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


class AnalysisJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    submission_id: int = Field(foreign_key="videosubmission.id", index=True)
    status: AnalysisStatus = Field(default=AnalysisStatus.pending, index=True)
    score: float = 0
    summary: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class DetectionResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    label: str
    confidence: float
    timestamp_sec: float
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
    start_sec: float
    end_sec: float
    confidence: float
    evidence: str = ""


class Violation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True)
    rule_code: str
    title: str
    deduction: float
    severity: str
    timestamp_sec: Optional[float] = None
    reason: str
    suggestion: str


class EvaluationReport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="analysisjob.id", index=True, unique=True)
    score: float
    conclusion: str
    strengths: str
    problems: str
    suggestions: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeDocument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    category: str = "规程"
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
