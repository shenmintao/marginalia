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

_KIMI_THINKING_MODELS = frozenset({
    "kimi-k2.5",
    "kimi-k2.6",
    "k2.6-code-preview",
})

_MIMO_THINKING_MODELS = frozenset({
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2-pro",
    "mimo-v2-omni",
})


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
    if "siliconflow" in base_url:
        return "siliconflow"
    if "openrouter" in base_url:
        return "openrouter"
    if "together" in base_url:
        return "together"
    if "nvidia" in base_url:
        return "nvidia"
    if "minimax" in base_url:
        return "minimax"
    if (
        "volces" in base_url
        or "volcengine" in base_url
        or "bytepluses" in base_url
        or "moonshot" in base_url
        or "xiaomimimo" in base_url
        or "ark.cn-" in base_url
        or "ark.ap-" in base_url
    ):
        return "thinking-type"
    if "generativelanguage.googleapis.com" in base_url or "googleapis.com" in base_url:
        return "gemini"
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
        _apply_enable_thinking_controls(
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
            preserve_thinking=True,
            bailian_deepseek_effort=True,
        )
    elif dialect == "siliconflow":
        _apply_enable_thinking_controls(
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
            preserve_thinking=False,
            bailian_deepseek_effort=False,
        )
    elif dialect == "openrouter":
        _apply_openrouter_controls(
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
        )
    elif dialect == "together":
        _apply_together_controls(extra_body, request, thinking=thinking)
    elif dialect == "nvidia":
        _apply_nvidia_controls(
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
        )
    elif dialect == "minimax":
        _apply_reasoning_split_controls(
            extra_body,
            request,
            thinking=thinking,
        )
    elif dialect == "gemini":
        _apply_gemini_controls(extra_body, request, thinking=thinking)
    elif dialect in ("deepseek", "thinking-type"):
        _apply_thinking_type_controls(
            kwargs,
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
        )
    else:
        _apply_generic_controls(
            kwargs,
            extra_body,
            request,
            thinking=thinking,
            model=str(kwargs.get("model") or ""),
        )
    if extra_body:
        kwargs["extra_body"] = extra_body


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _merge_extra_body(extra_body: dict[str, Any], patch: dict[str, Any]) -> None:
    merged = _deep_merge(dict(extra_body), patch)
    extra_body.clear()
    extra_body.update(merged)


def _apply_openrouter_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    effort = _normalize_effort(request.reasoning_effort)

    if thinking_type == "disabled":
        patch: dict[str, Any] = {"reasoning": {"enabled": False, "exclude": True}}
        if _is_kimi_thinking_model(model) or _is_mimo_thinking_model(model):
            patch["thinking"] = {"type": "disabled"}
        _merge_extra_body(extra_body, patch)
        return
    if thinking_type in ("enabled", "adaptive", "auto"):
        patch = {}
        if _is_kimi_thinking_model(model) or _is_mimo_thinking_model(model):
            patch["thinking"] = {"type": "enabled"}
        if effort:
            patch["reasoning"] = {"effort": _openrouter_effort(effort)}
        else:
            patch["reasoning"] = {"enabled": True}
        _merge_extra_body(extra_body, patch)


def _apply_together_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    effort = _normalize_effort(request.reasoning_effort)

    if thinking_type == "disabled":
        _merge_extra_body(extra_body, {"reasoning": {"enabled": False}})
        return
    if thinking_type in ("enabled", "adaptive", "auto"):
        patch: dict[str, Any] = {"reasoning": {"enabled": True}}
        if effort:
            patch["reasoning"]["effort"] = _openrouter_effort(effort)
        _merge_extra_body(extra_body, patch)


def _apply_nvidia_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    if thinking_type not in ("disabled", "enabled", "adaptive", "auto"):
        return

    enabled = thinking_type != "disabled"
    model_l = model.lower()
    key = "thinking" if _looks_deepseek(model_l) or _looks_kimi(model_l) else "enable_thinking"
    patch: dict[str, Any] = {"chat_template_kwargs": {key: enabled}}
    effort = _normalize_effort(request.reasoning_effort)
    if enabled and effort and _looks_qwen(model_l):
        patch["chat_template_kwargs"]["thinking_budget"] = _qwen_thinking_budget(effort)
    _merge_extra_body(extra_body, patch)


def _apply_reasoning_split_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    if thinking_type == "disabled":
        extra_body["reasoning_split"] = False
    elif thinking_type in ("enabled", "adaptive", "auto"):
        extra_body["reasoning_split"] = True


def _apply_gemini_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    if thinking_type == "disabled":
        _merge_extra_body(
            extra_body,
            {"google": {"thinking_config": {"thinking_budget": 0}}},
        )
        return
    if thinking_type in ("enabled", "adaptive", "auto"):
        effort = _normalize_effort(request.reasoning_effort)
        budget = _gemini_thinking_budget(effort) if effort else -1
        _merge_extra_body(
            extra_body,
            {
                "google": {
                    "thinking_config": {
                        "thinking_budget": budget,
                        "include_thoughts": True,
                    }
                }
            },
        )


def _apply_thinking_type_controls(
    kwargs: dict[str, Any],
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    if thinking_type in ("disabled", "enabled", "adaptive", "auto"):
        extra_body["thinking"] = {
            "type": "disabled" if thinking_type == "disabled" else "enabled",
        }

    effort = _normalize_effort(request.reasoning_effort)
    explicit_disabled = _thinking_type(thinking) == "disabled"
    if (
        effort
        and effort != "none"
        and not explicit_disabled
        and not _is_kimi_thinking_model(model)
    ):
        kwargs["reasoning_effort"] = request.reasoning_effort
    if _is_kimi_thinking_model(model):
        kwargs.pop("reasoning_effort", None)


def _apply_generic_controls(
    kwargs: dict[str, Any],
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)
    model_has_native_thinking = _is_kimi_thinking_model(model) or _is_mimo_thinking_model(model)
    model_l = model.lower()

    if _looks_qwen(model_l) and thinking_type in ("disabled", "enabled", "adaptive", "auto"):
        extra_body["enable_thinking"] = thinking_type != "disabled"
        effort = _normalize_effort(request.reasoning_effort)
        if extra_body["enable_thinking"] and effort:
            extra_body.setdefault("thinking_budget", _qwen_thinking_budget(effort))
    elif thinking is not None:
        extra_body["thinking"] = thinking
    elif model_has_native_thinking and thinking_type in ("disabled", "enabled", "adaptive", "auto"):
        extra_body["thinking"] = {
            "type": "disabled" if thinking_type == "disabled" else "enabled",
        }

    effort = _normalize_effort(request.reasoning_effort)
    explicit_disabled = _thinking_type(thinking) == "disabled"
    if (
        effort
        and effort != "none"
        and not explicit_disabled
        and not _is_kimi_thinking_model(model)
        and not _looks_qwen(model_l)
    ):
        kwargs["reasoning_effort"] = request.reasoning_effort


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


def _apply_enable_thinking_controls(
    extra_body: dict[str, Any],
    request: ChatRequest,
    *,
    thinking: Any,
    model: str,
    preserve_thinking: bool,
    bailian_deepseek_effort: bool,
) -> None:
    thinking_type = _resolved_thinking_type(thinking, request.reasoning_effort)

    if thinking_type == "disabled":
        extra_body["enable_thinking"] = False
        if preserve_thinking:
            extra_body.setdefault("preserve_thinking", False)
        return
    if thinking_type in ("enabled", "adaptive", "auto"):
        extra_body["enable_thinking"] = True

    model_l = model.lower()
    effort = _normalize_effort(request.reasoning_effort)
    thinking_enabled = extra_body.get("enable_thinking") is True
    if effort and bailian_deepseek_effort and _looks_deepseek_v4(model_l):
        extra_body.setdefault("reasoning_effort", _bailian_deepseek_effort(effort))
    if effort and thinking_enabled and _looks_qwen(model_l):
        extra_body.setdefault("thinking_budget", _qwen_thinking_budget(effort))


def _thinking_type(thinking: Any) -> str | None:
    if isinstance(thinking, dict):
        return str(thinking.get("type") or "").strip().lower() or None
    if isinstance(thinking, bool):
        return "enabled" if thinking else "disabled"
    return None


def _resolved_thinking_type(thinking: Any, effort: str | None) -> str | None:
    thinking_type = _thinking_type(thinking)
    if thinking_type:
        return thinking_type
    normalized = _normalize_effort(effort)
    if normalized is None:
        return None
    if normalized in ("none", "minimal", "minimum"):
        return "disabled"
    return "enabled"


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


def _openrouter_effort(effort: str) -> str:
    if effort in ("xhigh", "max"):
        return "high"
    if effort == "minimal":
        return "low"
    return effort


def _qwen_thinking_budget(effort: str) -> int:
    return _QWEN_THINKING_BUDGETS.get(effort, _QWEN_THINKING_BUDGETS["high"])


def _gemini_thinking_budget(effort: str) -> int:
    if effort in ("minimal", "low"):
        return 1024
    if effort == "medium":
        return 4096
    return 8192


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


def _looks_deepseek(model_l: str) -> bool:
    return "deepseek" in model_l


def _looks_kimi(model_l: str) -> bool:
    return "kimi" in model_l or "moonshot" in model_l


def _model_slug(model: str) -> str:
    return model.lower().rsplit("/", 1)[-1]


def _is_kimi_thinking_model(model: str) -> bool:
    return _model_slug(model) in _KIMI_THINKING_MODELS


def _is_mimo_thinking_model(model: str) -> bool:
    return _model_slug(model) in _MIMO_THINKING_MODELS
