"""视频理解适配层。

**生产链路只有这一条**：`ffmpeg` → `vlm_client` → `perception`（Pass 1）
→ `judgment`（Pass 2）→ `schema` 校验 → `provider` 统一出口。
词表在 `labels.py`，是唯一可信来源。

`legacy/` 里是 V1 的适配器（YOLO / MediaPipe / MMAction2 / 一个从未发过图像的
multimodal 客户端）。它们不参与任何生产路径，也不在这里被导入——
留着的唯一理由是让 `git log` 说得清当初为什么写。

**这里不导出任何东西。** 曾经这里写着 `from .pipeline import
RailwayPowerVideoAnalyzer`，后果是"能不能 import 一个词表常量"这件事，
取决于几个最终要删掉的文件里的可选依赖守卫写得对不对。
生产链路不该被待删除的代码绑架。
"""

from __future__ import annotations

__all__: list[str] = []
