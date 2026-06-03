# 铁道供电作业视频智能评价与实训考核平台 V1.0

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
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

接口文档：http://localhost:8000/docs

### 2. 前端

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

前端地址：http://localhost:5173

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

## 真实 AI 接入架子

当前后端保留统一事实结构：

- `DetectionFact`：目标检测事实，对应 YOLO/RT-DETR 输出
- `PoseFact`：人体关键点事实，对应 MediaPipe Pose
- `ActionFact`：动作识别事实，对应 MMAction2/VideoMAE
- `StepFact`：标准操作步骤事件，供规则引擎评分

适配器位置：

```text
backend/app/services/ai/
  config.py             环境变量配置
  frame_extractor.py    FFmpeg 抽帧
  yolo_detector.py      Ultralytics YOLO 目标检测适配器
  pose_estimator.py     MediaPipe Pose 姿态识别适配器
  action_recognizer.py  MMAction2 动作识别适配器
  multimodal.py         OpenAI/本地多模态解释适配器
  pipeline.py           统一视频分析流水线
  mock_analyzer.py      无模型时的演示回退
```

默认使用 mock：

```bash
export AI_ANALYZER_PROVIDER=mock
```

接入 YOLO：

```bash
pip install ultralytics opencv-python
export AI_ANALYZER_PROVIDER=yolo
export YOLO_MODEL_PATH=/path/to/railway-power-yolo.pt
export YOLO_CONFIDENCE=0.35
```

接入 YOLO + MediaPipe + MMAction2：

```bash
pip install ultralytics opencv-python mediapipe
export AI_ANALYZER_PROVIDER=hybrid
export YOLO_MODEL_PATH=/path/to/railway-power-yolo.pt
export MEDIAPIPE_ENABLED=true
export MMACTION_CONFIG=/path/to/action_config.py
export MMACTION_CHECKPOINT=/path/to/action_checkpoint.pth
```

接入多模态解释：

```bash
pip install openai
export MULTIMODAL_PROVIDER=openai
export OPENAI_API_KEY=你的密钥
export OPENAI_MODEL=gpt-4.1-mini
```

如果真实模型没有安装、权重没有配置或没有产出可评分事实，系统会自动回退到 mock 分析器，保证平台流程不断。

## 后续训练和替换真实 AI 的位置

1. 在 `backend/app/services/ai/` 中完善真实视频分析：
   - 抽帧：`frame_extractor.py`
   - 目标检测：`yolo_detector.py`
   - 姿态识别：`pose_estimator.py`
   - 动作识别：`action_recognizer.py`
   - 多模态解释：`multimodal.py`
2. 将模型输出统一成：
   - `DetectionResult`
   - `ActionResult`
   - `StepEvent`
3. 规则评分无需重写，只要保证事实字段和步骤事件稳定即可。

## 默认演示逻辑

默认分析器会根据视频文件名生成稳定的演示结果：

- 文件名包含 `bad`、`error`、`违规`、`错误` 时，会生成较多违规项
- 文件名包含 `no-glove` 或 `无手套` 时，会判定未佩戴绝缘手套
- 文件名包含 `skip-ground` 或 `未接地` 时，会判定缺失挂接地线步骤

这方便在没有真实模型和样本视频时演示完整业务闭环。
