"""测试夹具。

**环境变量必须在导入 `app.*` 之前设置**，因为 `app.config.settings` 是
模块级加载一次的冻结数据类，而 `app.database` 在导入时就会建目录和 engine。
这个文件顶部的那段就是为此存在的——pytest 保证 conftest 先于测试模块导入，
所以只要测试不自己 import app.*，顺序就是安全的。

测试一律用 `AI_PROVIDER=fake`：
- 不联网、不花钱、结果固定，所以断言可以写得很具体；
- 更重要的是**它不会因为网络抖动而变成"偶发失败"**，
  一个会随机红的测试套件很快就会被所有人忽略。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="rpo-tests-"))

os.environ["DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["AI_PROVIDER"] = "fake"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "AdminPass123"
os.environ["COOKIE_SECURE"] = "false"
# 测试里要传的视频很短，默认 5 秒下限会把它拒掉。
os.environ["MIN_DURATION_SEC"] = "1"
os.environ["MAX_UPLOAD_BYTES"] = str(8 * 1024 * 1024)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import DATA_DIR, engine, init_db  # noqa: E402
from app.models import User, UserRole  # noqa: E402
from app.seed import seed_defaults  # noqa: E402


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def sample_video() -> Path:
    """一段真实可解码的短视频。

    **不用假文件**：上传校验的判据是 ffprobe 能否读出时长，
    而"伪造一个 mp4"恰恰是它要挡住的东西。用真视频才能测到真路径。
    """
    path = _TMP_ROOT / "sample.mp4"
    if not path.exists():
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                str(path),
            ],
            check=True,
            timeout=120,
        )
    return path


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    init_db()
    with Session(engine) as session:
        seed_defaults(session)


@pytest.fixture
def db() -> Session:
    with Session(engine) as session:
        yield session


@pytest.fixture
def app_client() -> TestClient:
    """一个干净的 TestClient（独立 cookie jar）。

    每个角色用各自的 client，因为会话是靠 httpOnly cookie 维持的——
    共用一个 client 会让"学生看不到别人的数据"这类断言失去意义。
    """
    from app.main import app

    with TestClient(app) as client:
        yield client


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


BOOTSTRAP_PASSWORD = "AdminPass123"
ADMIN_PASSWORD = "AdminPass456"


@pytest.fixture
def admin(app_client: TestClient) -> TestClient:
    """已登录、且已过强制改密的管理员。

    **必须兼容"口令已被前一个测试改掉"**：数据库是整个测试会话共用的，
    而首登强制改密是真实行为，第一个用到管理员的测试就会把引导口令换掉。
    只认引导口令的话，整套用例里只有第一条能过，其余全部 401——
    而症状看起来像"鉴权坏了"，实际是夹具不幂等。
    """
    response = login(app_client, "admin", BOOTSTRAP_PASSWORD)
    if response.status_code != 200:
        response = login(app_client, "admin", ADMIN_PASSWORD)
    assert response.status_code == 200, response.text

    if response.json()["user"]["must_change_password"]:
        changed = app_client.post(
            "/api/auth/change-password",
            json={"old_password": BOOTSTRAP_PASSWORD, "new_password": ADMIN_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
    return app_client


def make_user(username: str, password: str, role: UserRole, **extra) -> User:
    """直接建账号。**不走 API**：测试的意图是验证业务逻辑，
    不是验证"管理员建账号"这个接口——那有它自己的测试。"""
    with Session(engine) as session:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            display_name=extra.pop("display_name", username),
            must_change_password=False,
            **extra,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


__all__ = ["login", "make_user", "DATA_DIR", "BOOTSTRAP_PASSWORD", "ADMIN_PASSWORD"]
