"""Agent runtime types + tool registry.

Each tool is a Python coroutine that takes:
  - `db`: an open AsyncSession (caller controls the transaction)
  - `ctx`: ToolContext (conversation_id / session_id; future: cancel hooks)
  - `args`: dict from the LLM (already JSON-parsed and schema-validated)

…and returns a JSON-serializable dict that becomes the tool_result content
fed back to the model AND appended to conversations.tool_calls.

A tool registers itself via `@tool(name=..., description=..., schema=...)`
at import time. agent/tools/__init__.py imports the concrete tool modules
to trigger registration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, MutableMapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.llm import ToolDef


@dataclass(slots=True)
class ToolContext:
    session_id: str
    conversation_id: str


ToolHandler = Callable[
    [AsyncSession, ToolContext, Mapping[str, Any]],
    Awaitable[dict[str, Any]],
]


@dataclass(slots=True)
class ToolRegistration:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


_REGISTRY: MutableMapping[str, ToolRegistration] = {}


def tool(
    *,
    name: str,
    description: str,
    schema: dict[str, Any],
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool. The handler signature must match ToolHandler."""
    def decorator(fn: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise RuntimeError(f"tool {name!r} already registered")
        _REGISTRY[name] = ToolRegistration(
            name=name,
            description=description,
            input_schema=schema,
            handler=fn,
        )
        return fn
    return decorator


def get_tool(name: str) -> ToolRegistration | None:
    return _REGISTRY.get(name)


def all_tool_defs() -> list[ToolDef]:
    return [
        ToolDef(name=r.name, description=r.description, input_schema=r.input_schema)
        for r in _REGISTRY.values()
    ]


def registered_tools() -> list[str]:
    return sorted(_REGISTRY)


# Import concrete tool modules to trigger their @tool registrations.
# Order doesn't matter; the LLM picks among them by name.
def _bootstrap() -> None:
    from marginalia.agent.tools import analyze_container  # noqa: F401
    from marginalia.agent.tools import generate_chart  # noqa: F401
    from marginalia.agent.tools import list_catalogs  # noqa: F401
    from marginalia.agent.tools import list_folder  # noqa: F401
    from marginalia.agent.tools import materialize_view  # noqa: F401
    from marginalia.agent.tools import query_log  # noqa: F401
    from marginalia.agent.tools import query_sql  # noqa: F401
    from marginalia.agent.tools import read_catalog  # noqa: F401
    from marginalia.agent.tools import read_entries_metadata  # noqa: F401
    from marginalia.agent.tools import read_files  # noqa: F401
    from marginalia.agent.tools import resolve_tag  # noqa: F401
    from marginalia.agent.tools import search_journal  # noqa: F401
    from marginalia.agent.tools import search_metadata  # noqa: F401


_bootstrap()
