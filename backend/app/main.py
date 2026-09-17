"""API 进程入口：`uvicorn app.main:app`。

## 三件在 V1 里做错、且在真实部署中会立刻出事的事

### 1. CORS 中间件必须删掉，不是"收紧"

V1 是 `allow_origins=["*"]` 配 `allow_credentials=True`。这个组合
**浏览器会直接拒绝**——加了 cookie 登录之后，不是"权限太宽"，而是
登录会静默失败。而且前后端同源部署时 CORS 本来就不需要。

同源不是可选优化，是**部署不变量**：前后端一旦分属不同 host:port，
`SameSite=Lax` 的 cookie 就是第三方 cookie，XHR 一律不发。
前端由本进程托管，这条才成立。

### 2. 启动必须 fail-fast

ffmpeg 缺失、AI key 没配，这两个问题在**启动时**发现，运维当场就能修；
等到上课时第一个学生上传才发现，代价是 100 个人等着看结果。
`/api/health` 也必须是真检查——V1 那个永远返回 `{"status": "ok"}` 的接口
没有任何信息量，监控接上去也发现不了 worker 已经死了三天。

### 3. 未登录的一切都返回 401

V1 实测 `curl 127.0.0.1:8024/api/tasks` 无需任何凭证即返回全量数据，
连 `/api/submissions/{id}/video` 都能遍历下载。现在所有 `/api` 路由
都挂了 `current_user` 依赖，漏掉一个就会在验收清单里暴露出来。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from .auth import bootstrap_admin, current_user_optional
from .clock import now
from .config import settings
from .database import DATA_DIR, engine, init_db
from .models import AnalysisJob, AnalysisStatus
from .routers import analysis, auth, catalog, reviews, roster, submissions, tasks, users
from .seed import seed_defaults
from .services.ai.ffmpeg import FFmpegUnavailable, require_ffmpeg
from .services.ai.provider import ProviderUnavailable, resolve_provider
from .worker import HEARTBEAT_FILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app.main")

# 前端构建产物。由本进程托管才能保证同源，进而保证 cookie 能发出去。
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# worker 心跳超过这个时长没更新即视为"分析功能不可用"。
# 比 worker 自己的 DEAD_AFTER_SEC 大一些，避免临界抖动导致健康检查闪断。
WORKER_STALE_SEC = 180


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with Session(engine) as session:
        seed_defaults(session)
        bootstrap_admin(
            session, username=settings.admin_username, password=settings.admin_password
        )
    _preflight()
    yield


def _preflight() -> None:
    """启动自检。**失败即拒绝启动**，不带病运行。

    一个能启动但分析不了的服务，比一个启动失败的服务危险得多：
    后者运维马上知道，前者会安静地收下 100 份作业然后什么都不产出。
    """
    try:
        require_ffmpeg()
    except FFmpegUnavailable as exc:
        raise SystemExit(f"[启动失败] {exc}") from exc

    try:
        provider = resolve_provider()
    except ProviderUnavailable as exc:
        raise SystemExit(f"[启动失败] {exc}") from exc

    if provider in {"fake", "demo"}:
        logger.warning(
            "AI_PROVIDER=%s：产出的不是对视频内容的真实分析，**不可用于真实教学**。", provider
        )
    if settings.admin_password:
        logger.warning(
            "检测到 ADMIN_PASSWORD 环境变量。仅在首次部署引导管理员时需要，"
            "创建完成后建议从 .env 中移除。"
        )
    if not (FRONTEND_DIST / "index.html").exists():
        logger.warning("未找到前端构建产物 %s，界面将无法访问。请先执行 npm run build。", FRONTEND_DIST)


app = FastAPI(
    title="铁道供电作业视频智能评价平台",
    version="2.0.0",
    lifespan=lifespan,
    # 生产环境不暴露交互式文档：它会列出全部接口与参数结构，
    # 对一个存着学生人脸视频的系统来说没有必要。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def guard(request: Request, call_next):
    """两道 HTTP 层的前置检查。

    1. **上传体积**：在 multipart 解析**之前**看 Content-Length。
       只看不拦的话，一个 20GB 的请求会先把磁盘写满，
       而磁盘满是全系统故障，不只是上传失败。
    2. **首登强制改密**：未改密前除认证接口外一律拒绝。
       没有这道闸，初始密码（写在 .env 里、教师知道）就一直有效，
       学生可以用"不是我做的"抗辩。
    """
    if request.url.path.startswith("/api/"):
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_upload_bytes + 1024 * 1024:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"上传内容超过 {settings.max_upload_bytes / 1024 / 1024 / 1024:.1f}GB 上限。"
                    },
                )

        if not request.url.path.startswith("/api/auth/"):
            user = _peek_user(request)
            if user is not None and user.must_change_password:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "首次登录需要先修改密码。", "must_change_password": True},
                )

    return await call_next(request)


def _peek_user(request: Request):
    """在中间件里取当前用户。独立开一个短 session——中间件没有依赖注入。"""
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    try:
        with Session(engine) as session:
            return current_user_optional(session=session, token=token)
    except Exception:  # 数据库抖动不该让所有请求变成 500
        logger.warning("中间件解析会话失败", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    """真检查，不是"进程还在"。

    V1 的实现永远返回 `{"status": "ok"}`。接到监控上，worker 死了三天
    也不会有任何告警——学生传完视频等到下课，而所有仪表盘都是绿的。

    四项检查各自对应一种真实的、会让系统"看起来正常但不可用"的故障：
    DB 不可写、ffmpeg 丢了、AI 没配、worker 不在了。
    """
    checks: dict[str, object] = {}
    healthy = True

    # 1. 数据库可写。只读检查发现不了磁盘满和权限问题。
    try:
        with Session(engine) as session:
            session.exec(select(AnalysisJob.id).limit(1)).all()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"失败：{type(exc).__name__}"
        healthy = False

    # 2. 抽帧依赖。
    try:
        require_ffmpeg()
        checks["ffmpeg"] = "ok"
    except FFmpegUnavailable:
        checks["ffmpeg"] = "缺失"
        healthy = False

    # 3. AI 可用性。配置成 vlm 但没 key 时，服务是"能跑但出不了结果"的。
    try:
        checks["ai_provider"] = resolve_provider()
    except ProviderUnavailable as exc:
        checks["ai_provider"] = str(exc).splitlines()[0]
        healthy = False

    # 4. worker 心跳。
    checks["worker"] = _worker_status()
    if checks["worker"] == "不可用":
        healthy = False

    # 5. 队列积压。积压本身不算故障，但它是"要不要加人"的唯一依据。
    try:
        with Session(engine) as session:
            pending = len(
                session.exec(
                    select(AnalysisJob.id).where(AnalysisJob.status == AnalysisStatus.pending)
                ).all()
            )
            running = len(
                session.exec(
                    select(AnalysisJob.id).where(AnalysisJob.status == AnalysisStatus.running)
                ).all()
            )
        checks["queue"] = {"pending": pending, "running": running}
    except Exception:
        pass

    return JSONResponse(status_code=200 if healthy else 503, content={"status": "ok" if healthy else "degraded", "checks": checks})


def _worker_status() -> str:
    if not HEARTBEAT_FILE.exists():
        return "不可用"
    try:
        stamp = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
        last = now().fromisoformat(stamp)
    except (OSError, ValueError):
        return "不可用"
    return "ok" if (now() - last).total_seconds() <= WORKER_STALE_SEC else "不可用"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roster.router)
app.include_router(catalog.router)
app.include_router(tasks.router)
app.include_router(submissions.router)
app.include_router(analysis.router)
app.include_router(reviews.router)


# ---------------------------------------------------------------------------
# 前端静态托管
# ---------------------------------------------------------------------------

if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    """把非 /api 的路径交给前端路由。

    `/api` 开头的未匹配路径**必须返回 404**，不能回落到 index.html：
    回落到 HTML 会让前端的 `res.json()` 抛"Unexpected token <"，
    一个后端 404 就变成了一句看不懂的前端报错。
    """
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "接口不存在。"})

    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        return JSONResponse(
            status_code=503,
            content={"detail": "前端尚未构建。请在 frontend 目录执行 npm run build。"},
        )

    # 只放行确实存在的静态文件，其余一律回落到 index.html 交给前端路由——
    # 同时挡掉 `../../` 这类路径穿越。
    if full_path:
        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "资源不存在。"})
        if candidate.is_file():
            return FileResponse(candidate)

    return FileResponse(index, media_type="text/html")


__all__ = ["app"]
