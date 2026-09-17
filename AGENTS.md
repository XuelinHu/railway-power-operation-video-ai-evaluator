# Agent Instructions

## Default Conda Environment
- Environment name: `rc-llm-eval`
- Environment path: `/home/xuelin/miniconda3/envs/rc-llm-eval`
- Prefer running Python commands with `conda run -n rc-llm-eval ...` or `/home/xuelin/miniconda3/envs/rc-llm-eval/bin/python`.

<!-- codex-agent-runtime:start -->

## Runtime Ports And Database Configuration

- Keep this section aligned with the root README when database names, ports, or service defaults change.
- Do not copy secrets from local `.env` files into commits; document only placeholders or compose defaults.

### Database
- Primary database: SQLite.
- Default database file: `backend/data/app.db`.
- SQLite has no network port; the file is created automatically when the backend starts.

### Default Ports
- Backend FastAPI service: `8090`.
- Frontend Vite dev server: `4090`.
- **这两个端口刻意选在 FRP 转发范围（8020-8040 / 4020-4040）之外**，避免开发机服务被 `frpc` 映射到公网。改动端口前请先读 `~/.claude/skills/frp/references/project-ports.md`。

### Notes For Codex Agents
- Uploaded videos are stored under `backend/data/uploads`; keep generated runtime data out of Git unless explicitly required.
- Before committing, check `git status --short --branch` and avoid staging unrelated runtime artifacts.

### Source Files Checked
- `backend/app/database.py`
- `frontend/vite.config.js`
- `README.md`

<!-- codex-agent-runtime:end -->

## GitHub Commit Language

- Use English for all GitHub commit messages and pull/push related commit notes.
