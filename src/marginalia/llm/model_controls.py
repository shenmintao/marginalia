from __future__ import annotations

from dataclasses import replace
from typing import Any

from marginalia.config import LlmProfile
from marginalia.llm.types import ChatRequest


DISABLE_THINKING_EXTRA_BODY: dict[str, Any] = {"thinking": {"type": "disabled"}}

_QWEN_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "max": 8192,
    "xhigh": 8192,
}

_ANTHROPIC_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "max": 8192,
    "xhigh": 8192,
}


def should_disable_thinking_by_default(profile: LlmProfile) -> bool:
    return (
        profile.name == "ingest"
        and profile.provider in ("openai-compatible", "anthropic")
    )


def with_disabled_thinking(request: ChatRequest) -> ChatRequest:
    body = dict(request.extra_body or {})
    body.setdefault("thinking", {"type": "disabled"})
    if body == (request.extra_body or {}):
        return request
    return replace(request, extra_body=body)


def detect_openai_compatible_dialect(profile: LlmProfile) -> str:
    if profile.provider == "openai":
        return "openai"
    base_url = (profile.base_url or "").lower()
    if "dashscope" in base_url or "aliyuncs" in base_url or "bailian" in base_url:
        return "bailian"
    if "deepseek" in base_url:
        return "deepseek"
    return "openai-compatible"


def apply_openai_reasoning_controls(
    kwargs: dict[str, Any],
    request: ChatRequest,
    *,
    dialect: str,
) -> None:
    extra_body = dict(request.extra_body or {})
    thinking = extra_body.pop("thinking", None)
    if dialect == "bailian":
        _apply_bailian_controls(
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
        )
    else:
        if thinking is not None:
            extra_body["thinking"] = thinking
        if request.reasoning_effort:
            kwargs["reasoning_effort"] = request.reasoning_effort
    if extra_body:
        kwargs["extra_body"] = extra_body


def anthropic_reasoning_controls(
    request: ChatRequest,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extra_body = dict(request.extra_body or {})
    thinking = extra_body.pop("thinking", None)
    generated = False
    if thinking is None and request.reasoning_effort:
        thinking = {"type": "enabled"}
        generated = True

    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
        if thinking_type == "enabled" and "budget_tokens" not in thinking:
            budget = _anthropic_budget(request.reasoning_effort, request.max_tokens)
            if budget is None and generated:
                thinking = None
            elif budget is not None:
                thinking = {**thinking, "budget_tokens": budget}

    return (
        thinking if isinstance(thinking, dict) else None,
        extra_body or None,
    )


def _apply_bailian_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
) -> None:
    thinking_type = _thinking_type(thinking)

    if thinking_type == "disabled":
        extra_body["enable_thinking"] = False
        extra_body.setdefault("preserve_thinking", False)
        return
    if thinking_type in ("enabled", "adaptive", "auto"):
        extra_body["enable_thinking"] = True

    model_l = model.lower()
    effort = _normalize_effort(request.reasoning_effort)
    thinking_enabled = extra_body.get("enable_thinking") is True
    if effort and _looks_deepseek_v4(model_l):
        extra_body.setdefault("reasoning_effort", _bailian_deepseek_effort(effort))
    if effort and thinking_enabled and _looks_qwen(model_l):
        extra_body.setdefault("thinking_budget", _qwen_thinking_budget(effort))


def _thinking_type(thinking: Any) -> str | None:
    if isinstance(thinking, dict):
        return str(thinking.get("type") or "").strip().lower() or None
    if isinstance(thinking, bool):
        return "enabled" if thinking else "disabled"
    return None


def _normalize_effort(value: str | None) -> str | None:
    if not value:
        return None
    effort = str(value).strip().lower()
    if "/" in effort:
        effort = effort.split("/")[-1].strip()
    return effort or None


def _bailian_deepseek_effort(effort: str) -> str:
    if effort in ("max", "xhigh"):
        return "max"
    return "high"


def _qwen_thinking_budget(effort: str) -> int:
    return _QWEN_THINKING_BUDGETS.get(effort, _QWEN_THINKING_BUDGETS["high"])


def _anthropic_budget(effort: str | None, max_tokens: int) -> int | None:
    cap = max(0, int(max_tokens) - 1)
    if cap < 1024:
        return None
    key = _normalize_effort(effort) or "medium"
    budget = _ANTHROPIC_THINKING_BUDGETS.get(
        key,
        _ANTHROPIC_THINKING_BUDGETS["medium"],
    )
    return max(1024, min(budget, cap))


def _looks_qwen(model_l: str) -> bool:
    return (
        model_l.startswith(("qwen", "qwq", "qvq"))
        or "/qwen" in model_l
        or "qwen" in model_l
    )


def _looks_deepseek_v4(model_l: str) -> bool:
    return "deepseek-v4" in model_l
