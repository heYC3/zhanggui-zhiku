"""真实模型草稿节点。

固定流程里的一个可替换步骤：读取证据，请模型只填写允许的字段，再用 JSON Schema 检查。
不在这里写入台账。规则校验和人工确认仍由原来的流程负责。

用量只记录接口真正返回的数字。接口没给的字段保持空值，不能补成 0。
没有调用模型时，费用才是 0，并标明来源是 not_called。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.listing_agent.catalog import get_product
from app.listing_agent.paths import PACKAGE_ROOT


logger = logging.getLogger("listing_agent.llm_draft")

PROMPT_VERSION = "listing-draft-v1"
ALLOWED_FIELDS = ("title", "bullets", "gaps")
DRAFT_MODES = ("offline", "llm")

# 这些失败类型要在日志里能对上。解析失败、超时、空输出、无证据是验收里点名的四种。
FAILURE_PARSE = "parse_error"
FAILURE_TIMEOUT = "timeout"
FAILURE_EMPTY = "empty_output"
FAILURE_NO_EVIDENCE = "no_evidence"
FAILURE_TRANSPORT = "transport_error"


class DraftCallError(Exception):
    """模型调用没有拿到可解析正文。failure_type 写入打点日志。"""

    def __init__(
        self,
        failure_type: str,
        message: str,
        *,
        parameters: dict | None = None,
        model: str | None = None,
        model_name_source: str = "unknown",
    ):
        super().__init__(message)
        self.failure_type = failure_type
        self.parameters = parameters
        self.model = model
        self.model_name_source = model_name_source


class LlmDraftBody(BaseModel):
    """模型只允许交这三个字段。多出来的键会被拒绝，避免它另写一个无证据属性。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    bullets: list[str] = Field(min_length=1, max_length=5)
    gaps: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("标题是空的")
        return text

    @field_validator("bullets")
    @classmethod
    def bullets_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("要点是空的")
        if len(cleaned) > 5:
            raise ValueError("要点超过 5 条")
        return cleaned

    @field_validator("gaps")
    @classmethod
    def clean_gaps(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


def draft_json_schema() -> dict:
    return LlmDraftBody.model_json_schema()


def resolve_draft_mode(value: str | None = None) -> str:
    """offline 用原句模板；llm 才调用模型。别的值直接拒绝。"""
    if value is not None and str(value).strip():
        raw = str(value).strip().lower()
    else:
        raw = os.getenv("LISTING_DRAFT_MODE", "offline").strip().lower() or "offline"
    if raw not in DRAFT_MODES:
        raise ValueError("草稿来源只能是 offline 或 llm")
    return raw


def load_project_env() -> None:
    """读取 knowledge_base/.env。已经存在的环境变量不覆盖。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PACKAGE_ROOT / ".env", override=False)


@dataclass
class ModelReply:
    text: str
    model: str | None
    model_name_source: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    parameters: dict = field(default_factory=dict)


class ModelClient(Protocol):
    def complete(self, *, messages: list[dict], timeout_s: float) -> ModelReply:
        """发送已经拼好的消息。超时、空输出由调用方或本方法标成失败类型。"""


@dataclass
class DraftOutcome:
    draft: dict | None
    telemetry: dict
    message: str


def _token(usage: dict | None, key: str) -> int | None:
    """缺字段、空值或无法辨认的值保持 None。接口明确返回的 0 才记 0。"""
    if not isinstance(usage, dict) or key not in usage:
        return None
    value = usage[key]
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _prices() -> tuple[float, float] | None:
    prompt = os.getenv("LISTING_LLM_USD_PER_MILLION_PROMPT", "").strip()
    completion = os.getenv("LISTING_LLM_USD_PER_MILLION_COMPLETION", "").strip()
    if not prompt or not completion:
        return None
    try:
        return float(prompt), float(completion)
    except ValueError:
        logger.warning("Listing 单价配置无法读取，费用留空")
        return None


def _cost(prompt_tokens: int | None, completion_tokens: int | None, usage_source: str) -> tuple[float | None, bool, str]:
    if usage_source == "not_called":
        return 0, True, "没有调用模型，费用是 0。"
    if prompt_tokens is None or completion_tokens is None:
        return None, False, "用量未知，费用留空，不记成 0。"
    prices = _prices()
    if prices is None:
        return None, True, "已经拿到 token，但没有可用单价，费用留空，不记成 0。"
    prompt_price, completion_price = prices
    cost = prompt_tokens / 1_000_000 * prompt_price + completion_tokens / 1_000_000 * completion_price
    return round(cost, 8), True, "费用按返回的 token 和配置的单价计算。"


def _telemetry(
    *,
    failure_type: str | None,
    message: str,
    model: str | None,
    model_name_source: str,
    parameters: dict | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    usage_source: str,
    duration_ms: float,
    prompt_sha256: str | None,
) -> dict:
    if usage_source == "not_called":
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
    cost, usage_known, note = _cost(prompt_tokens, completion_tokens, usage_source)
    if usage_source != "not_called" and (prompt_tokens is None or completion_tokens is None):
        usage_known = False
        cost = None
    return {
        "mode": "llm",
        "model": model,
        "model_name_source": model_name_source,
        "parameters": parameters,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "usage_known": usage_known,
        "usage_source": usage_source,
        "cost": cost,
        "duration_ms": round(duration_ms, 3),
        "failure_type": failure_type,
        "message": message,
        "usage_note": note,
    }


def offline_telemetry(duration_ms: float) -> dict:
    """原句模板没有请求模型。这里的 0 是已知的未调用，不是把未知用量填成 0。"""
    return {
        "mode": "offline",
        "model": None,
        "model_name_source": "not_called",
        "parameters": None,
        "prompt_version": None,
        "prompt_sha256": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_known": True,
        "usage_source": "not_called",
        "cost": 0,
        "duration_ms": round(duration_ms, 3),
        "failure_type": None,
        "message": "",
        "usage_note": "没有调用模型，费用是 0。",
    }


def normalize_allowed_fields(fields: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not fields:
        return ALLOWED_FIELDS
    chosen = tuple(name for name in ALLOWED_FIELDS if name in fields)
    return chosen or ALLOWED_FIELDS


def build_messages(
    query: str,
    product: dict,
    evidence: list[dict],
    platform: str,
    allowed_fields: tuple[str, ...],
) -> list[dict]:
    evidence_lines = []
    for row in evidence:
        evidence_lines.append(f"[{row.get('id')}] {str(row.get('text') or '').strip()}")
    system = (
        "你只根据证据写 Listing 草稿，并且只输出一个 JSON 对象。"
        f"允许写入的字段只有：{'、'.join(allowed_fields)}。"
        "bullets 里的每一条必须整句抄自证据原文，不能改字。"
        "不能补写证据里没有的属性、数字或承诺。"
        "用户问到但证据没有的内容，只写进 gaps，不要写进 title 或 bullets。"
        "不要输出 JSON 以外的解释。"
    )
    user = "\n".join(
        [
            f"平台：{platform}",
            f"资料品牌：{product['brand']}",
            f"资料型号：{product['model']}",
            f"问题：{query}",
            "证据：",
            "\n".join(evidence_lines),
            "JSON Schema：",
            json.dumps(draft_json_schema(), ensure_ascii=False),
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _fingerprint(messages: list[dict]) -> str:
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _extract_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        first = raw.find("\n")
        raw = raw[first + 1 :] if first >= 0 else raw
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("草稿必须是 JSON 对象", raw, 0)
    return payload


def append_telemetry(directory, record: dict):
    from pathlib import Path

    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "draft_telemetry.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


class OpenAICompatibleClient:
    """OpenAI 兼容的 chat/completions。模型名以响应里的 model 为准。"""

    def __init__(self, *, api_key: str, base_url: str, model: str, temperature: float, timeout_s: float):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> "OpenAICompatibleClient":
        load_project_env()
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        model = os.getenv("LISTING_OPENAI_MODEL", "").strip() or os.getenv("LLM_DEFAULT_MODEL", "").strip()
        if not api_key or api_key == "your key":
            raise DraftCallError(FAILURE_TRANSPORT, "没有配置 OPENAI_API_KEY，不能调用模型")
        if not base_url:
            raise DraftCallError(FAILURE_TRANSPORT, "没有配置 OPENAI_BASE_URL，不能调用模型")
        if not model:
            raise DraftCallError(FAILURE_TRANSPORT, "没有配置 LLM_DEFAULT_MODEL，不能调用模型")
        raw_temperature = os.getenv("LLM_DEFAULT_TEMPERATURE", "").strip()
        try:
            temperature = float(raw_temperature) if raw_temperature else 1.0
        except ValueError:
            temperature = 1.0
        raw_timeout = os.getenv("LISTING_LLM_TIMEOUT_S", "").strip()
        try:
            timeout_s = float(raw_timeout) if raw_timeout else 30.0
        except ValueError:
            timeout_s = 30.0
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            timeout_s=timeout_s,
        )

    @classmethod
    def from_bailian_env(cls) -> "OpenAICompatibleClient":
        """百炼的对话接口。重排序模型 qwen3-rerank 不拿来写草稿。"""
        load_project_env()
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise DraftCallError(FAILURE_TRANSPORT, "没有配置 DASHSCOPE_API_KEY，不能调用百炼")
        model = os.getenv("LISTING_BAILIAN_MODEL", "").strip() or "qwen-plus"
        base_url = os.getenv("LISTING_BAILIAN_BASE_URL", "").strip() or (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        raw_temperature = os.getenv("LISTING_BAILIAN_TEMPERATURE", "").strip()
        try:
            temperature = float(raw_temperature) if raw_temperature else 0.1
        except ValueError:
            temperature = 0.1
        raw_timeout = os.getenv("LISTING_LLM_TIMEOUT_S", "").strip()
        try:
            timeout_s = float(raw_timeout) if raw_timeout else 30.0
        except ValueError:
            timeout_s = 30.0
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            timeout_s=timeout_s,
        )

    def complete(self, *, messages: list[dict], timeout_s: float) -> ModelReply:
        limit = timeout_s or self.timeout_s
        parameters = {
            "temperature": self.temperature,
            "timeout_s": limit,
            "response_format": "json_object",
        }
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._post(body, limit)
        except httpx.TimeoutException as exc:
            raise DraftCallError(
                FAILURE_TIMEOUT,
                "模型超时",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            ) from exc
        except httpx.HTTPError as exc:
            raise DraftCallError(
                FAILURE_TRANSPORT,
                "模型接口没有连上",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            ) from exc
        if response.status_code in {408, 504}:
            raise DraftCallError(
                FAILURE_TIMEOUT,
                f"模型接口超时（{response.status_code}）",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            )
        if response.status_code >= 400 and "response_format" in response.text.lower():
            # 有的模型不接受 JSON 模式。去掉这个参数再请求一次，并记下实际发出的参数。
            body.pop("response_format", None)
            parameters["response_format"] = None
            try:
                response = self._post(body, limit)
            except httpx.TimeoutException as exc:
                raise DraftCallError(
                    FAILURE_TIMEOUT,
                    "模型超时",
                    parameters=parameters,
                    model=self.model,
                    model_name_source="request",
                ) from exc
            except httpx.HTTPError as exc:
                raise DraftCallError(
                    FAILURE_TRANSPORT,
                    "模型接口没有连上",
                    parameters=parameters,
                    model=self.model,
                    model_name_source="request",
                ) from exc
        if response.status_code in {408, 504}:
            raise DraftCallError(
                FAILURE_TIMEOUT,
                f"模型接口超时（{response.status_code}）",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            )
        if response.status_code >= 400:
            raise DraftCallError(
                FAILURE_TRANSPORT,
                f"模型接口返回 {response.status_code}",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise DraftCallError(
                FAILURE_PARSE,
                "模型接口返回的不是 JSON",
                parameters=parameters,
                model=self.model,
                model_name_source="request",
            ) from exc
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        response_model = data.get("model")
        if isinstance(response_model, str) and response_model.strip():
            model = response_model.strip()
            source = "response"
        else:
            model = self.model
            source = "request"
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        text = ""
        if choices and isinstance(choices[0], dict):
            content = (choices[0].get("message") or {}).get("content")
            text = _content_text(content)
        return ModelReply(
            text=text,
            model=model,
            model_name_source=source,
            prompt_tokens=_token(usage, "prompt_tokens"),
            completion_tokens=_token(usage, "completion_tokens"),
            total_tokens=_token(usage, "total_tokens"),
            parameters=parameters,
        )

    def _post(self, body: dict, timeout_s: float) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=httpx.Timeout(timeout_s),
        )


def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content)


def generate_llm_draft(
    query: str,
    sku: str,
    evidence: list[dict],
    platform: str,
    *,
    client: ModelClient | None = None,
    allowed_fields: list[str] | tuple[str, ...] | None = None,
    timeout_s: float | None = None,
) -> DraftOutcome:
    """有证据才调用模型。输出先过 Schema，调用方再做规则校验和人工确认。"""
    started = time.perf_counter()
    fields = normalize_allowed_fields(allowed_fields)
    product = get_product(sku)
    if product is None or not evidence:
        elapsed = (time.perf_counter() - started) * 1000
        telemetry = _telemetry(
            failure_type=FAILURE_NO_EVIDENCE,
            message="没有证据，不能生成草稿",
            model=None,
            model_name_source="not_called",
            parameters=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            usage_source="not_called",
            duration_ms=elapsed,
            prompt_sha256=None,
        )
        logger.info("Listing 模型草稿未调用：无证据 sku=%s", sku)
        return DraftOutcome(None, telemetry, telemetry["message"])

    messages = build_messages(query, product, evidence, platform, fields)
    fingerprint = _fingerprint(messages)
    limit = timeout_s
    if limit is None:
        raw_timeout = os.getenv("LISTING_LLM_TIMEOUT_S", "").strip()
        try:
            limit = float(raw_timeout) if raw_timeout else 30.0
        except ValueError:
            limit = 30.0
    try:
        active = client or OpenAICompatibleClient.from_env()
        reply = active.complete(messages=messages, timeout_s=limit)
    except DraftCallError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        telemetry = _telemetry(
            failure_type=exc.failure_type,
            message=str(exc),
            model=exc.model,
            model_name_source=exc.model_name_source,
            parameters=exc.parameters,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            usage_source="unknown",
            duration_ms=elapsed,
            prompt_sha256=fingerprint,
        )
        logger.info("Listing 模型草稿失败：%s", exc.failure_type)
        return DraftOutcome(None, telemetry, str(exc))
    except TimeoutError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        telemetry = _telemetry(
            failure_type=FAILURE_TIMEOUT,
            message="模型超时",
            model=None,
            model_name_source="unknown",
            parameters={"timeout_s": limit},
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            usage_source="unknown",
            duration_ms=elapsed,
            prompt_sha256=fingerprint,
        )
        logger.info("Listing 模型草稿超时")
        return DraftOutcome(None, telemetry, telemetry["message"])
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logger.exception("Listing 模型草稿调用异常")
        telemetry = _telemetry(
            failure_type=FAILURE_TRANSPORT,
            message=str(exc) or "模型调用失败",
            model=None,
            model_name_source="unknown",
            parameters={"timeout_s": limit},
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            usage_source="unknown",
            duration_ms=elapsed,
            prompt_sha256=fingerprint,
        )
        return DraftOutcome(None, telemetry, telemetry["message"])

    elapsed = (time.perf_counter() - started) * 1000
    if not reply.text or not reply.text.strip():
        telemetry = _telemetry(
            failure_type=FAILURE_EMPTY,
            message="模型返回是空的",
            model=reply.model,
            model_name_source=reply.model_name_source,
            parameters=reply.parameters,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            total_tokens=reply.total_tokens,
            usage_source="response" if reply.prompt_tokens is not None or reply.completion_tokens is not None else "unknown",
            duration_ms=elapsed,
            prompt_sha256=fingerprint,
        )
        # 空输出即使带回了部分用量，缺的那一项也不能写成 0。_telemetry 已按字段处理。
        if reply.prompt_tokens is None or reply.completion_tokens is None:
            telemetry["usage_source"] = "unknown"
            telemetry["usage_known"] = False
            telemetry["cost"] = None
            telemetry["usage_note"] = "用量未知，费用留空，不记成 0。"
        logger.info("Listing 模型草稿为空输出 model=%s", reply.model)
        return DraftOutcome(None, telemetry, telemetry["message"])

    try:
        payload = _extract_json(reply.text)
        body = LlmDraftBody.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        telemetry = _telemetry(
            failure_type=FAILURE_PARSE,
            message="模型输出没有通过 JSON Schema",
            model=reply.model,
            model_name_source=reply.model_name_source,
            parameters=reply.parameters,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            total_tokens=reply.total_tokens,
            usage_source="response" if reply.prompt_tokens is not None and reply.completion_tokens is not None else "unknown",
            duration_ms=elapsed,
            prompt_sha256=fingerprint,
        )
        logger.info("Listing 模型草稿解析失败 model=%s", reply.model)
        return DraftOutcome(None, telemetry, telemetry["message"])

    usage_source = "response" if reply.prompt_tokens is not None and reply.completion_tokens is not None else "unknown"
    telemetry = _telemetry(
        failure_type=None,
        message="",
        model=reply.model,
        model_name_source=reply.model_name_source,
        parameters=reply.parameters,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        total_tokens=reply.total_tokens,
        usage_source=usage_source,
        duration_ms=elapsed,
        prompt_sha256=fingerprint,
    )
    draft = {
        "platform": platform,
        "sku": sku,
        "brand": product["brand"],
        "model": product["model"],
        "title": body.title,
        "bullets": body.bullets,
        "gaps": body.gaps,
        "evidence_ids": [row.get("id") for row in evidence],
        "policy": "llm-draft",
        "prompt_version": PROMPT_VERSION,
        "allowed_fields": list(fields),
        "duration_ms": telemetry["duration_ms"],
        "call": telemetry,
    }
    logger.info(
        "Listing 模型草稿完成 model=%s usage_known=%s failure=%s",
        telemetry["model"],
        telemetry["usage_known"],
        telemetry["failure_type"],
    )
    return DraftOutcome(draft, telemetry, "")


_PROVIDER_LABELS = {"openai": "OpenAI", "bailian": "百炼", "knowledge": "知识库"}


def provider_order() -> list[str]:
    """先走配置的那一家。失败再换另一家。默认先百炼，再 OpenAI。"""
    primary = os.getenv("LISTING_LLM_PROVIDER", "bailian").strip().lower() or "bailian"
    if primary == "openai":
        return ["openai", "bailian"]
    return ["bailian", "openai"]


def _client_for_provider(name: str) -> OpenAICompatibleClient:
    if name == "bailian":
        return OpenAICompatibleClient.from_bailian_env()
    if name == "openai":
        return OpenAICompatibleClient.from_env()
    raise DraftCallError(FAILURE_TRANSPORT, f"不认识的模型来源：{name}")


def _attempt_record(provider: str, telemetry: dict) -> dict:
    return {
        "provider": provider,
        "model": telemetry.get("model"),
        "failure_type": telemetry.get("failure_type"),
        "prompt_tokens": telemetry.get("prompt_tokens"),
        "completion_tokens": telemetry.get("completion_tokens"),
        "cost": telemetry.get("cost"),
        "usage_known": telemetry.get("usage_known"),
        "usage_source": telemetry.get("usage_source"),
        "duration_ms": telemetry.get("duration_ms"),
        "message": telemetry.get("message") or "",
    }


def _fallback_note(failed: list[str], winner: str) -> str:
    names = "和".join(_PROVIDER_LABELS.get(name, name) for name in failed)
    landed = _PROVIDER_LABELS.get(winner, winner)
    return f"{names}没有给出可用草稿，已改用{landed}。请确认后再写入。"


def generate_draft_with_fallback(
    query: str,
    sku: str,
    evidence: list[dict],
    platform: str,
    *,
    client: ModelClient | None = None,
    clients: dict | None = None,
    allow_env: bool | None = None,
    allowed_fields: list[str] | tuple[str, ...] | None = None,
) -> DraftOutcome:
    """OpenAI 和百炼互相接替。两家都没有可用草稿时，改用知识库原句。

    无论最后用了哪一家，调用方仍然先做规则校验，再等人工确认后才写入。
    注入了测试客户端时，不会悄悄去连真实接口。
    """
    if get_product(sku) is None or not evidence:
        return generate_llm_draft(
            query,
            sku,
            evidence,
            platform,
            client=client,
            allowed_fields=allowed_fields,
        )

    order = provider_order()
    supplied = dict(clients or {})
    if client is not None and order[0] not in supplied:
        supplied[order[0]] = client
    use_env = (client is None and not clients) if allow_env is None else allow_env
    attempts: list[dict] = []

    for name in order:
        active = supplied.get(name)
        if active is None and not use_env:
            continue
        if active is None:
            try:
                active = _client_for_provider(name)
            except DraftCallError as exc:
                attempts.append(
                    {
                        "provider": name,
                        "model": exc.model,
                        "failure_type": exc.failure_type,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "cost": None,
                        "usage_known": False,
                        "usage_source": "unknown",
                        "duration_ms": 0,
                        "message": str(exc),
                    }
                )
                logger.info("Listing 草稿跳过 %s：%s", name, exc.failure_type)
                continue
        outcome = generate_llm_draft(
            query,
            sku,
            evidence,
            platform,
            client=active,
            allowed_fields=allowed_fields,
        )
        attempts.append(_attempt_record(name, outcome.telemetry))
        if outcome.draft is None:
            logger.info("Listing 草稿 %s 不可用：%s", name, outcome.telemetry.get("failure_type"))
            continue
        failed = [item["provider"] for item in attempts if item.get("failure_type")]
        outcome.telemetry["provider"] = name
        outcome.telemetry["attempts"] = attempts
        outcome.telemetry["fallback_from"] = failed
        if failed:
            note = _fallback_note(failed, name)
            outcome.telemetry["message"] = note
            outcome.draft["fallback_note"] = note
        outcome.draft["call"] = outcome.telemetry
        return outcome

    from app.listing_agent.drafting import build_draft

    failed = [item["provider"] for item in attempts]
    try:
        draft = build_draft(query, sku, evidence, "template-draft-conservative")
    except (LookupError, ValueError) as exc:
        telemetry = {
            "mode": "llm",
            "provider": None,
            "attempts": attempts,
            "fallback_from": failed,
            "failure_type": attempts[-1]["failure_type"] if attempts else FAILURE_NO_EVIDENCE,
            "message": str(exc),
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost": None,
            "usage_known": False,
            "usage_source": "unknown",
            "model": None,
            "model_name_source": "unknown",
            "parameters": None,
            "prompt_version": PROMPT_VERSION,
            "duration_ms": round(sum(item.get("duration_ms") or 0 for item in attempts), 3),
            "usage_note": "模型没有给出草稿，知识库也没能拼出原句。费用留空，不记成 0。",
        }
        return DraftOutcome(None, telemetry, str(exc))

    note = _fallback_note(failed, "knowledge") if failed else "已改用知识库里的原句。请确认后再写入。"
    spent = round(sum(item.get("duration_ms") or 0 for item in attempts) + draft["duration_ms"], 3)
    telemetry = offline_telemetry(spent)
    telemetry.update(
        {
            "mode": "knowledge",
            "provider": "knowledge",
            "attempts": attempts,
            "fallback_from": failed,
            "message": note,
            "usage_note": "两家模型都没有给出可用草稿，已改用知识库原句。这一步没有再调用模型，费用是 0。",
        }
    )
    draft["platform"] = platform
    draft["fallback_note"] = note
    draft["call"] = telemetry
    logger.info("Listing 草稿改用知识库原句，先前失败：%s", "、".join(failed))
    return DraftOutcome(draft, telemetry, note)
