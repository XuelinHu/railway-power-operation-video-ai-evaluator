"""运行配置。全部来自环境变量，`.env` 由部署脚本加载。

集中在一处的理由：这些值的默认值**决定了系统是安全的还是危险的**，
散落在各处时没人能一眼看出当前配置的全貌。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 上传放行的最短时长。
#
# **这个值不是拍脑袋定的，它由抽帧参数反推出来**：抽帧按固定 5 秒间隔去重
# （ffmpeg.DEFAULT_MIN_GAP_SEC），而分析至少要 4 帧
# （vlm_analyzer.MIN_FRAMES_TO_ANALYZE）才能开工。实测：
#
#     5 秒 → 1 帧   10 秒 → 2 帧   15 秒 → 3 帧   20 秒 → 4 帧 ✓
#
# 所以最小值低于 20 秒，就会出现"上传校验放行 → 排队 → 才告诉你视频太短"
# 这种最招人烦的失败：学生白等一场，还占了一次分析配额。
# 宁可在他点上传的那一刻就说清楚。
#
# 两个常量之间的这个关系由 tests/test_upload_limits.py 钉住，
# 谁改了抽帧参数而没同步改这里，测试会直接红。
DEFAULT_MIN_DURATION_SEC = 20


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- 部署 ---
    cookie_secure: bool
    session_ttl_hours: int
    # cookie 名必须全局唯一：同机约 10 个项目共用 host，
    # 而 cookie 作用域是 host 不是 host:port，重名会互相覆盖导致随机掉登录。
    cookie_name: str

    # --- 引导管理员 ---
    admin_username: str
    admin_password: str

    # --- AI ---
    ai_provider: str          # vlm | fake | demo
    dashscope_api_key: str
    vlm_model: str
    daily_call_budget: int

    # --- 上传限制 ---
    max_upload_bytes: int
    max_duration_sec: float
    min_duration_sec: float

    # --- 路径 ---
    data_dir: Path
    upload_dir: Path
    frames_dir: Path
    backup_dir: Path


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "").strip() or (BASE_DIR / "data"))

    # AI_PROVIDER 的默认值刻意是 `vlm` 而不是 `demo`。
    # 不变量 #3：绝不静默回退。默认成 demo 意味着配置漏了的时候，
    # 系统会用假数据跑得很好看——那正是 V1 的病根。
    provider = (os.getenv("AI_PROVIDER", "vlm").strip() or "vlm").lower()

    return Settings(
        cookie_secure=_bool("COOKIE_SECURE", False),
        session_ttl_hours=_int("SESSION_TTL_HOURS", 12),
        cookie_name=os.getenv("COOKIE_NAME", "rpo_session").strip() or "rpo_session",
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.getenv("ADMIN_PASSWORD", "").strip(),
        ai_provider=provider,
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        vlm_model=os.getenv("VLM_MODEL", "").strip(),
        daily_call_budget=_int("DAILY_CALL_BUDGET", 2000),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024),
        max_duration_sec=_float("MAX_DURATION_SEC", 15 * 60),
        min_duration_sec=_float("MIN_DURATION_SEC", DEFAULT_MIN_DURATION_SEC),
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        frames_dir=data_dir / "frames",
        backup_dir=data_dir / "backups",
    )


settings = load_settings()
