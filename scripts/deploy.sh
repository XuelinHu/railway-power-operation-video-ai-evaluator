#!/usr/bin/env bash
#
# 首次部署与升级。
#
# **升级和首装走同一条路径**是刻意的：分两条路的话，
# 升级那条一年只走几次，等你需要它的时候它已经坏了。
#
#   scripts/deploy.sh            # 安装/升级
#   scripts/deploy.sh --no-build # 跳过前端构建（只改了后端时用）
#
# 前置：Python 3.11+、ffmpeg（带 ffprobe）、Node 18+（仅构建前端时需要）。

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/railway-power}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_FRONTEND=1

[[ "${1:-}" == "--no-build" ]] && BUILD_FRONTEND=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31m错误：%s\033[0m\n' "$1" >&2; exit 1; }

# --- 依赖检查 -------------------------------------------------------------
# 这三样缺一个，系统都只会在"学生上传之后"才报错，
# 而那时你面对的是一屋子等着交作业的学生。所以在部署时就挡住。
step "检查依赖"
command -v ffmpeg  >/dev/null || fail "缺少 ffmpeg。apt install ffmpeg"
command -v ffprobe >/dev/null || fail "缺少 ffprobe（通常随 ffmpeg 一起装）"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "需要 Python 3.11 或更高版本"

ffmpeg -hide_banner -filters 2>/dev/null | grep -q drawtext \
    || echo "提示：ffmpeg 未编译 drawtext 滤镜，证据帧上不会烧入时间码（不影响其它功能）"

# --- 目录与配置 -----------------------------------------------------------
step "准备目录与配置"
mkdir -p "$APP_DIR"/{data/{uploads,frames,backups}}
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    fail "已生成 $APP_DIR/.env，请填写 DASHSCOPE_API_KEY 与 ADMIN_PASSWORD 后重新运行。"
fi
# .env 里同时有 API Key 和管理员口令，权限必须是 600。
chmod 600 "$APP_DIR/.env"

# --- 后端 -----------------------------------------------------------------
step "安装后端依赖"
cd "$REPO_DIR/backend"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

step "同步代码到 $APP_DIR"
mkdir -p "$APP_DIR/backend"
rsync -a --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
    "$REPO_DIR/backend/" "$APP_DIR/backend/"
ln -sfn "$REPO_DIR/backend/.venv" "$APP_DIR/backend/.venv"

# --- 数据库升级 -----------------------------------------------------------
# 先预览再应用。**必须在重启服务之前**：新代码遇到旧表结构会直接报错，
# 而 systemd 的 Restart=always 会让它一直重启一直失败。
step "数据库升级"
cd "$APP_DIR/backend"
export DATA_DIR="$APP_DIR/data"
.venv/bin/python -m scripts.migrate
.venv/bin/python -m scripts.migrate --apply

# --- 前端 -----------------------------------------------------------------
if [[ "$BUILD_FRONTEND" == "1" ]]; then
    step "构建前端"
    command -v npm >/dev/null || fail "缺少 npm（构建前端需要 Node 18+）"
    cd "$REPO_DIR/frontend"
    [[ -d node_modules ]] || npm ci
    npm run build

    # FastAPI 直接托管构建产物，前后端同源——这是 cookie 会话能工作的前提。
    mkdir -p "$APP_DIR/frontend"
    rsync -a --delete "$REPO_DIR/frontend/dist/" "$APP_DIR/frontend/dist/"
fi

# --- 服务 -----------------------------------------------------------------
step "安装 systemd 服务"
sudo cp "$REPO_DIR"/deploy/railway-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable railway-api railway-worker
sudo systemctl restart railway-api
sleep 2
sudo systemctl restart railway-worker

step "等待启动"
for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
        echo "服务已就绪。"
        curl -s http://127.0.0.1:8090/api/health | head -c 400
        echo
        exit 0
    fi
    sleep 1
done

echo "服务未在 20 秒内就绪，最近日志：" >&2
sudo journalctl -u railway-api -n 40 --no-pager >&2
exit 1
