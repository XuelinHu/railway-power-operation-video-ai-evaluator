"""通义千问-VL 客户端（阿里云百炼 DashScope 的 OpenAI 兼容模式）。

**为什么不用 openai SDK**：本项目只需要"发一个带 base64 图片的 POST"这一件事，
而自建 httpx 客户端换来三样 SDK 会包装掉的东西：

1. **精确超时**。连接 10 秒、读取 120 秒必须分开设。没有读取超时的 HTTP 调用
   会永久挂住——这与旧 `frame_extractor.py` 缺 `timeout` 是同一类 bug，
   区别只是挂死在网络等待而不是挂死在子进程。
2. **原样的 request_id 和错误体**。DashScope 出问题时，`request_id` 是唯一能拿去
   提工单的东西；错误体里的 `code`（如 `Arrearage` 欠费、`InvalidApiKey`）
   决定了该不该重试。
3. **少一个依赖**。学校服务器上少一个包就少一类版本冲突。

**关于超时值的取舍**：读取 120 秒不是随手定的。Pass 1 每窗要送 5-6 张图，
VL 模型在高峰期单次推理 30-60 秒是常态，设 30 秒会把正常请求判成失败。
但也不能不设——worker 是串行的，一次挂死就是整条队列停摆。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 默认模型。qwen3-vl-plus 是当前代次里性价比较合适的一档；
# qwen-vl-max 更贵，qwen-vl-plus 更便宜但小目标（绝缘手套）识别弱一些。
DEFAULT_MODEL = "qwen3-vl-plus"

CONNECT_TIMEOUT_SEC = 10.0
READ_TIMEOUT_SEC = 120.0
WRITE_TIMEOUT_SEC = 60.0  # 上传 16 帧 base64 约 1-3MB，慢网下 60 秒足够
POOL_TIMEOUT_SEC = 10.0

RETRY_BACKOFF_SEC = 5.0
MAX_ATTEMPTS = 2

# 以下单价为**估算值，必须核对账单页后修正**。
# 它们只用于给运维一个量级感（"跑完 300 个视频大概几块钱"），
# 绝不参与任何计费或额度判断——那是 token 计数和每日预算闸门的事。
PRICE_PER_1K_INPUT_TOKENS = 0.0015
PRICE_PER_1K_OUTPUT_TOKENS = 0.0045


class VLMError(RuntimeError):
    """VLM 调用失败的基类。所有子类都带教师可读的中文说明。"""

    def __init__(self, message: str, *, request_id: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.request_id = request_id
        self.retryable = retryable


class VLMNotConfigured(VLMError):
    """没配 API key。属于部署问题，应当在启动时就 fail-fast。"""


class VLMUnavailable(VLMError):
    """网络不通、超时、服务端 5xx、限流。可重试。"""


class VLMRejected(VLMError):
    """请求被拒绝：key 无效、欠费、模型名不对、参数非法。重试没有意义。"""


class VLMResponseInvalid(VLMError):
    """HTTP 200 但返回体不是我们期望的结构。"""


@dataclass
class VLMResult:
    """一次调用的完整结果。token 用量与 request_id 一并带回，用于成本台账与排障。"""

    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_id: str | None = None
    latency_ms: int = 0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def estimated_cost_cny(self) -> float:
        return (
            self.prompt_tokens / 1000 * PRICE_PER_1K_INPUT_TOKENS
            + self.completion_tokens / 1000 * PRICE_PER_1K_OUTPUT_TOKENS
        )


def encode_image(path: Path) -> str:
    """把图片编码成 data URI。"""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


class VLMClient:
    """极简 DashScope 客户端。线程不安全的部分只有 httpx.Client 的连接池，无状态。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DASHSCOPE_BASE_URL,
        read_timeout: float = READ_TIMEOUT_SEC,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not api_key or not api_key.strip():
            raise VLMNotConfigured(
                "未配置 DASHSCOPE_API_KEY，无法调用视觉模型。\n"
                "请在 backend/.env 中设置：DASHSCOPE_API_KEY=sk-xxxx"
            )
        self.model = model
        self.max_attempts = max(1, max_attempts)
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SEC,
                read=read_timeout,
                write=WRITE_TIMEOUT_SEC,
                pool=POOL_TIMEOUT_SEC,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VLMClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tool: dict[str, Any] | None = None,
        json_object: bool = False,
        max_tokens: int = 3000,
        temperature: float = 0.0,
        enable_thinking: bool = False,
    ) -> VLMResult:
        """发一次对话请求。

        `tool` 给定时走 function calling 并强制调用该工具（结构化输出的主路径）。
        某些兼容模式实现对强制 tool_choice 支持不稳，故保留 `json_object=True`
        作为退路：改用 response_format 取 JSON。两条路的解析入口是同一个
        （都从 message 里取出那个 JSON 对象），所以切换不影响下游。
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 非流式调用下，qwen3 系列开启思考模式会直接报错
            # （parameter.enable_thinking only support stream call），
            # 必须显式关掉。抽取任务也不需要思考链——我们要的是观察，不是推理。
            "enable_thinking": enable_thinking,
        }
        tool_name: str | None = None
        if tool is not None:
            tool_name = tool["function"]["name"]
            body["tools"] = [tool]
            body["tool_choice"] = {"type": "function", "function": {"name": tool_name}}
        elif json_object:
            body["response_format"] = {"type": "json_object"}

        raw = self._post_with_retry(body)
        payload = self._extract_payload(raw, tool_name=tool_name)
        usage = raw.get("usage") or {}
        return VLMResult(
            payload=payload,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            request_id=raw.get("request_id") or raw.get("id"),
            model=raw.get("model") or self.model,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: VLMError | None = None

        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.post("/chat/completions", json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = VLMUnavailable(
                    "连接视觉模型服务失败，请检查服务器能否访问外网"
                    "（dashscope.aliyuncs.com）。",
                    retryable=True,
                )
                logger.warning("VLM 连接失败（第 %d 次）：%s", attempt, exc)
            except httpx.ReadTimeout:
                last_error = VLMUnavailable(
                    f"视觉模型响应超时（超过 {READ_TIMEOUT_SEC:g} 秒）。"
                    "可能是网络慢或服务繁忙，可稍后重试。",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                last_error = VLMUnavailable(f"请求视觉模型时发生网络错误：{exc}", retryable=True)
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                if response.status_code == 200:
                    try:
                        raw = response.json()
                    except ValueError:
                        raise VLMResponseInvalid("视觉模型返回的不是合法 JSON。") from None
                    raw["_latency_ms"] = latency_ms
                    return raw
                last_error = self._classify_http_error(response)
                if not last_error.retryable:
                    raise last_error

            if attempt < self.max_attempts:
                logger.warning("VLM 调用失败，%.0f 秒后重试：%s", RETRY_BACKOFF_SEC, last_error)
                time.sleep(RETRY_BACKOFF_SEC)

        raise last_error or VLMUnavailable("视觉模型调用失败，原因未知。")

    @staticmethod
    def _classify_http_error(response: httpx.Response) -> VLMError:
        """把 HTTP 错误翻译成"该不该重试"和"老师看得懂的话"。"""
        request_id = response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or response.text[:300])

        # 欠费、key 无效、模型不存在：重试一万次也是同样的结果，而且白等 5 秒。
        fatal_codes = {
            "InvalidApiKey", "Arrearage", "ModelNotFound",
            "InvalidParameter", "InvalidRequest", "AccessDenied",
        }
        if response.status_code in (401, 403) or code in fatal_codes:
            hint = {
                "Arrearage": "账户已欠费",
                "InvalidApiKey": "API Key 无效或已失效",
                "ModelNotFound": "模型名不存在或当前账号无权调用",
            }.get(code, f"请求被拒绝（HTTP {response.status_code}）")
            return VLMRejected(f"视觉模型调用失败：{hint}。{message}", request_id=request_id)

        # 限流和服务端故障是可以自愈的。
        return VLMUnavailable(
            f"视觉模型服务暂时不可用（HTTP {response.status_code}）：{message}",
            request_id=request_id,
            retryable=response.status_code == 429 or response.status_code >= 500,
        )

    @staticmethod
    def _extract_payload(raw: dict[str, Any], *, tool_name: str | None) -> dict[str, Any]:
        """从响应里取出那个 JSON 对象。兼容 function calling 与纯 JSON 两条路。"""
        choices = raw.get("choices") or []
        if not choices:
            raise VLMResponseInvalid(f"视觉模型返回体缺少 choices 字段：{str(raw)[:300]}")

        message = choices[0].get("message") or {}

        if tool_name:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != tool_name:
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, dict):
                    return arguments
                return _loads_lenient(arguments)
            # 模型没按约定调工具，但可能把 JSON 写在了正文里。与其直接失败，
            # 不如尝试解析正文——这对已付费的调用是更划算的处理。
            content = message.get("content")
            if content:
                logger.warning("模型未调用工具 %s，改从正文解析 JSON。", tool_name)
                return _loads_lenient(content)
            raise VLMResponseInvalid("模型既未调用工具，正文也为空。")

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise VLMResponseInvalid("视觉模型返回的正文为空。")
        return _loads_lenient(content)


def _loads_lenient(text: Any) -> dict[str, Any]:
    """尽最大努力把一段文本解析成 JSON 对象。

    要处理三种真实存在的脏输出：```json 围栏、尾随逗号、前后夹带说明文字。
    这些都是"轻量修复"，不消耗额外调用；修不好才值得重试或失败。
    """
    if not isinstance(text, str):
        raise VLMResponseInvalid(f"期望 JSON 文本，实际拿到 {type(text).__name__}")

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        candidate = candidate.rsplit("```", 1)[0].strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].lstrip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # 尝试掐头去尾到第一个 { 和最后一个 }，剥掉模型的寒暄
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise VLMResponseInvalid(f"无法从模型输出中解析出 JSON：{text[:200]}") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise VLMResponseInvalid(f"模型输出的 JSON 不合法：{exc}；原文：{text[:200]}") from None

    if not isinstance(parsed, dict):
        raise VLMResponseInvalid(f"期望 JSON 对象，实际拿到 {type(parsed).__name__}")
    return parsed
