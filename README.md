# 铁道供电作业视频智能评价与实训考核平台 V1.0

<p align="center">
  <img height="20" src="https://img.shields.io/badge/vue-3.5.13-42B883?logo=vuedotjs&amp;logoColor=white" />
  <img height="20" src="https://img.shields.io/badge/vite-6.0.3-646CFF?logo=vite&amp;logoColor=white" />
  <img height="20" src="https://img.shields.io/badge/fastapi-0.115.6-009688?logo=fastapi&amp;logoColor=white" />
  <img height="20" src="https://img.shields.io/badge/sqlmodel-0.0.22-E92063" />
  <img height="20" src="https://img.shields.io/badge/sqlite-used-003B57?logo=sqlite&amp;logoColor=white" />
  <img height="20" src="https://img.shields.io/badge/ultralytics-optional-111F68" />
</p>

这是一个可运行的 MVP 项目，用于演示“视频理解 AI + 铁道供电作业知识库 + 规则评分引擎 + 多模态解释生成 + 平台管理”的核心闭环。

当前版本重点完成项目骨架和核心业务流程：

- 教师创建实训评价任务
- 学生上传铁道供电作业视频
- 系统创建视频分析任务
- AI 分析服务抽取视频基础事实并生成可替换的检测、动作、步骤事件
- 规则评分引擎根据标准步骤和评分规则生成扣分项
- 报告服务生成评价意见、扣分原因、整改建议
- 前端展示视频、任务状态、AI 证据、扣分项、评分报告和班级统计

> 默认 AI 分析器是可运行的启发式/模拟实现。真实模型接入架子已经搭好，位于 `backend/app/services/ai/`，可按环境变量切换 YOLO、MediaPipe、MMAction2 和多模态解释。

## 技术栈

- 前端：Vue 3 + Vite
- 后端：FastAPI + SQLModel + SQLite
- 视频处理：FFmpeg/OpenCV 预留接口，当前版本不强依赖本机安装
- 存储：本地 `backend/data/uploads`

## 快速启动

### 1. 后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8090
```

接口文档：http://localhost:8090/docs

### 2. 前端

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 4090
```

前端地址：http://localhost:4090

## 项目结构

```text
backend/
  app/
    main.py                 FastAPI 入口
    database.py             SQLite 初始化
    models.py               数据模型
    seed.py                 默认标准步骤和评分规则
    routers/                API 路由
    services/
      analyzer.py           视频分析器兼容入口
      ai/                   YOLO、MediaPipe、MMAction2、多模态模型适配器
      rule_engine.py        规则评分引擎
      report.py             评价报告生成
  data/uploads/             上传视频目录
frontend/
  src/
    App.vue                 主工作台
    services/api.js         API 封装
```

## AI 分析：三个提供方，绝不静默回退

由 `AI_PROVIDER` 选择，**默认 `vlm`**：

| 值 | 用途 | 说明 |
|---|---|---|
| `vlm` | **生产** | 通义千问-VL 两段式分析，见下 |
| `fake` | 自动化测试 | 固定剧本，不联网不花钱，结果可断言 |
| `demo` | 演示/培训 | 按**文件名关键词**猜结果，不读视频内容 |

**配置成 `vlm` 但缺少 API Key 时，worker 拒绝启动**，不会退回演示数据。
V1 的病根正是"真实模型没装上就自动退回 mock，保证平台流程不断"——
流程不断的代价是所有人恒定满分，且没人会发现。

### 两段式分析（`AI_PROVIDER=vlm`）

让模型边看图边判 9 个步骤，会诱发**一致性偏置**：它倾向把整张表填成
`completed`，因为那样"任务完成得最干净"。所以拆成两步：

1. **Pass 1 感知**：按时间窗分 3 次调用，每次 5-6 帧，**只描述不判断** ——
   每帧客观记录人数、安全帽/绝缘手套/绝缘靴各为 `yes/no/unclear`、
   身体是否完整入画、正在做什么。
2. **Pass 2 判定**：1 次**纯文本**调用，喂入 Pass 1 的观察表 + 9 步定义，
   输出每步判定。它**不可能引用 Pass 1 里没有的证据**，证据链可机械校验。

关键防线（`schema.py`，不靠 prompt 祈求）：

- 帧号必须真实存在，越界 = 幻觉 → 剔除并记录
- 判 `completed` / `not_completed` 却拿不出证据帧 → **降级为 `not_visible`**
- `not_visible` 判为默认值："看不清一律标不可见，这是正常的，
  多数实训视频因机位原因会有若干步骤不可见"

### 四条贯穿全局的不变量

1. **没有证据支撑时 `score` 必须是空**，不能是 0 也不能是 100。
   失败的任务在前端显示"分析失败"，而不是"0 分"。
2. **大模型只输出事实，绝不输出分数**。分数永远由 `rule_engine.py` 算，
   保证可复现、可审计、可解释。
3. **绝不静默回退**（见上）。
4. **标签词表单一来源**（`labels.py`）。若 VLM 返回"安全帽"而规则引擎
   找 `helmet`，集合差会让**所有人都扣 15 分**——这类 bug 必须靠
   单一常量同时喂 prompt 和规则引擎来根除。

### 关键文件

```text
backend/app/services/ai/
  provider.py       三个提供方的统一入口与输出契约（AnalysisOutput）
  vlm_analyzer.py   两段式链路编排（extract → perceive → judge）
  perception.py     Pass 1：观察表
  judgment.py       Pass 2：纯文本判定
  schema.py         Pydantic 校验 + 反幻觉（帧号越界、无证据降级）
  labels.py         标签与步骤词表的**唯一来源**
  ffmpeg.py         抽帧、转码、probe（全部带 timeout）
  vlm_client.py     DashScope 兼容接口客户端
  legacy/           V1 的 YOLO/MediaPipe/MMAction2 适配器，**已废弃**
```

`legacy/` 里的适配器只会产出 `action_0` 这类占位结果，保留仅为存档。
新代码不要引用它们。

## 部署（学校服务器）

单机、内网、百人以内、**无 GPU**。两个 systemd 服务 + SQLite + 本地磁盘。

### 前置

- Python 3.11+、ffmpeg（含 ffprobe）、Node 18+（仅构建前端时需要）
- 一个阿里云百炼（DashScope）API Key，并**在控制台设好消费告警**
- 服务器能直连外网（VLM 调用需要）

### 安装

```bash
sudo apt install ffmpeg sqlite3
git clone <repo> /opt/railway-power && cd /opt/railway-power
scripts/deploy.sh
```

首次运行会生成 `/opt/railway-power/.env`（权限 600）并停下，要求你填写
`DASHSCOPE_API_KEY` 与 `ADMIN_PASSWORD`，填完再跑一次即可。

`scripts/deploy.sh` 会：装依赖 → 同步代码 → **升级数据库** → 构建前端 →
装 systemd 服务 → 重启 → 等健康检查通过。**升级和首装走同一条路径**是刻意的：
分两条路的话，升级那条一年只走几次，等你需要它的时候它已经坏了。

### 验收（每次部署完都跑一次）

```bash
cd /opt/railway-power/backend
.venv/bin/python -m scripts.smoke_live --base http://127.0.0.1:8090
```

它对着**真实运行的服务**跑完整链路：登录 → 导名册开通账号 → 学生上传 →
等 worker（另一个进程）分析 → 取证据帧 → 越权检查 → 教师复核终审 →
导出 CSV。进程内测试（`pytest`）验证不了 cookie 的真实收发、跨进程 worker、
ffmpeg 与静态托管，而这几样恰恰是部署时最容易坏的。

### 备份

```bash
scripts/backup.sh                  # 备份到 data/backups
scripts/backup.sh /mnt/usb/railway # 备份到外部磁盘
```

**绝不要用 `cp app.db`**：数据库跑在 WAL 模式下，已有内容可能还在 `-wal`
里没落盘，`cp` 出来的副本可能直接是损坏的——而"备份是坏的"通常要到真的
需要恢复时才发现。脚本用 `VACUUM INTO` 生成一致快照，并做 `integrity_check`。

建议 cron 每天一次，保留 14 份。**上线前务必真实恢复演练一次。**

### 数据库升级

`scripts/migrate.py` 是 schema 驱动的，不是手写 SQL 文件——手写 `.sql`
会与 `models.py` 悄悄走散，而本项目没有 Alembic。它做三件事：

1. 对比模型与现有表，补缺失的列（`create_all` **不会**给已存在的表加列，
   这是第一次升级最容易翻车的地方）
2. 需要改列约束时走 SQLite 的整表重建（SQLite 没有 `ALTER COLUMN`）
3. 数据修复：清掉失败任务遗留的 0 分（不变量 #1）、同步报告分数、
   把 V1 的旧步骤标为待人工确认

```bash
.venv/bin/python -m scripts.migrate          # 预览，只读
.venv/bin/python -m scripts.migrate --apply  # 应用（自动先备份）
```

<!-- codex-runtime-notes:start -->

## Runtime Ports And Database Configuration

### Database
- Primary database: SQLite.
- Default database file: `backend/data/app.db`.
- SQLite has no network port; the file is created automatically when the backend starts.

### Default Ports
- Backend FastAPI service: `8090`.
- Frontend Vite dev server: `4090`.
- 端口刻意选在 FRP 转发范围之外，避免开发机服务被映射到公网。

### Notes
- Uploaded videos are stored under `backend/data/uploads`; keep generated runtime data out of Git unless explicitly required.

### Source Files Checked
- `backend/app/database.py`
- `frontend/vite.config.js`
- `README.md`

<!-- codex-runtime-notes:end -->
