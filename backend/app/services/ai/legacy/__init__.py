"""已弃用的适配器。**不要在这里加新代码。**

这些文件是从 `ai/` 顶层移进来的，每一份都有具体的、真实的缺陷，
留着它们是为了两件事：一是让 `git log` 能查到当初为什么写，
二是万一需要回滚时有参照。它们**不参与任何生产路径**。

## 各自的问题

- `multimodal.py` —— **已删除**。它从未发送过任何图像，
  而且调用的 `responses.create` 在 DashScope 兼容模式下根本不存在。
  属于"没接上"，不是"接不上"，修它没有意义。
- `action_recognizer.py` —— 输出 `action_0`、`action_1` 这类无语义标签，
  与规则引擎期望的中文步骤名对不上。它是"标签词表必须单一来源"
  这条不变量被违反的实例，现在由 `labels.py` 统一收口。
- `yolo_detector.py` / `pose_estimator.py` —— 需要 ultralytics / mediapipe
  和模型权重，而这台机器没有 GPU。更要紧的是：VLM 路径**不产 bbox**，
  所以围绕 bbox 做的展示需求已整体删除。
- `frame_extractor.py` —— 它的 `subprocess.run` **没有 timeout**，
  一个损坏的视频就能永久挂死一个 worker；而且缺 ffmpeg 时返回空列表，
  整条链路会静默退化成 mock。这两点正是 `ffmpeg.py` 要修的。
- `pipeline.py` —— 上面这些东西的编排，含"真实模型没装上就自动退回 mock"
  的回退逻辑。那正是"所有人恒定满分而没人发现"的病根。
- `contracts.py` / `steps.py` —— 旧的契约与词表。词表已并入 `labels.py`；
  契约由 `schema.py`（VLM 输出）和 `provider.AnalysisOutput`（统一出口）承担。
- `config.py` —— 旧的 `AISettings`，其 `AI_ANALYZER_PROVIDER` 默认值是
  `"mock"`。新的 `app/config.py` 默认 `vlm`，绝不静默回退。
- `mock_analyzer.py` —— **唯一还在被引用的一个**：`AI_PROVIDER=demo`
  时由 `provider._analyze_demo()` 调用。保留它是因为演示场景确实需要
  "任何视频都能跑出结果"，但那条路必须由运维显式选择，
  且界面上会打"演示数据"横幅。
"""
