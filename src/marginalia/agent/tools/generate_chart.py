"""generate_chart — agent tool that emits a Vega-Lite v5 spec for the user.

DESIGN.md §10.4 / B.6: charts are *single-directional*. The agent decides
"a picture would help here" and calls this tool with controlled inputs;
we assemble the spec server-side and ship it to the user via the SSE
side-channel. The model gets back only `{chart_id, caption}` — the full
spec is intentionally hidden so it doesn't pollute the LLM's context.

We do NOT accept a raw user-supplied Vega-Lite spec — that path is the
classic SSR-style risk surface (remote `data.url`, `transform`,
`expression`). All structure is built here from a tight schema.

Side-channel: the result dict carries `__user_only__` which the runtime
strips before serialising for the model and re-emits as a `user_artifact`
SSE frame. The conversation.tool_calls row keeps the full payload so /info
and replays still show the chart.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.utils.ids import new_id


MAX_ROWS = 1000
MAX_FIELDS_PER_ROW = 10
MAX_TITLE_LEN = 200
MAX_CAPTION_LEN = 500

ALLOWED_MARKS = frozenset({"bar", "line", "point", "area", "tick", "rule"})
ALLOWED_TYPES = frozenset({"quantitative", "ordinal", "nominal", "temporal"})
ALLOWED_AGGREGATES = frozenset({
    "count", "sum", "mean", "median", "min", "max", "stdev",
})

_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["field", "type"],
    "properties": {
        "field": {"type": "string", "minLength": 1, "maxLength": 64},
        "type": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
        "aggregate": {"type": "string", "enum": sorted(ALLOWED_AGGREGATES)},
        "title": {"type": "string", "maxLength": 80},
    },
}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mark", "encoding", "data", "caption"],
    "properties": {
        "mark": {
            "type": "string",
            "enum": sorted(ALLOWED_MARKS),
            "description": (
                "Vega-Lite mark type. Pick `bar` for categorical comparison, "
                "`line` for trends over a temporal/ordinal x, `point` for "
                "scatter, `area` for stacked-over-time."
            ),
        },
        "encoding": {
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y"],
            "properties": {
                "x": _FIELD_SCHEMA,
                "y": _FIELD_SCHEMA,
                "color": _FIELD_SCHEMA,
            },
            "description": (
                "Channel mappings. Each side names a field present in `data` "
                "and its type (quantitative / ordinal / nominal / temporal)."
            ),
        },
        "data": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ROWS,
            "items": {"type": "object"},
            "description": (
                "Row objects. Cap is 1000 rows / 10 fields. For larger "
                "tables, pre-aggregate via query_sql before charting."
            ),
        },
        "title": {
            "type": "string",
            "maxLength": MAX_TITLE_LEN,
            "description": "Chart title shown above the figure.",
        },
        "caption": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CAPTION_LEN,
            "description": (
                "One-sentence caption explaining what the chart shows and why "
                "it supports the answer. Required — the model is expected to "
                "reference this caption in its final answer via [^x] footnote."
            ),
        },
    },
}


@tool(
    name="generate_chart",
    description=(
        "Render a Vega-Lite v5 chart for the user (single-directional: the "
        "spec goes to the UI, NOT back to you). Use after query_sql when a "
        "picture clarifies the answer. You receive only `chart_id` + your "
        "own caption — reference the chart in the final answer with a "
        "footnote like [^chart-1] and the chart_id."
    ),
    schema=SCHEMA,
)
async def generate_chart(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    mark: str = args["mark"]
    encoding: Mapping[str, Any] = args["encoding"]
    rows: list[Mapping[str, Any]] = list(args["data"])
    caption: str = args["caption"].strip()
    title: str | None = args.get("title")

    # --- sanitise data ------------------------------------------------------
    err = _check_rows(rows)
    if err:
        return {"ok": False, "error": err}

    # Each named encoding field must exist in at least one row.
    fields_referenced = {
        v["field"] for v in encoding.values() if isinstance(v, Mapping)
    }
    sample_keys = set()
    for r in rows[:50]:
        sample_keys.update(r.keys())
    missing = fields_referenced - sample_keys
    if missing:
        return {
            "ok": False,
            "error": (
                f"encoding refers to field(s) not present in data: "
                f"{sorted(missing)}; available: {sorted(sample_keys)}"
            ),
        }

    chart_id = "ch_" + new_id().split("-")[0]
    spec = _build_spec(
        mark=mark,
        encoding=encoding,
        rows=rows,
        title=title,
        chart_id=chart_id,
    )

    # __user_only__ is the side-channel payload. The runtime pops it
    # before handing the result to the model; everything outside that key
    # IS visible to the LLM.
    return {
        "ok": True,
        "chart_id": chart_id,
        "caption": caption,
        "summary": (
            f"chart {chart_id} ready for the user "
            f"(mark={mark}, x={encoding['x']['field']}, "
            f"y={encoding['y']['field']}, rows={len(rows)})"
        ),
        "__user_only__": {
            "kind": "vega_lite",
            "chart_id": chart_id,
            "title": title,
            "caption": caption,
            "spec": spec,
        },
    }


# -- helpers ---------------------------------------------------------------

def _check_rows(rows: list[Mapping[str, Any]]) -> str | None:
    if not rows:
        return "data must contain at least one row"
    if len(rows) > MAX_ROWS:
        return f"data exceeds {MAX_ROWS} rows; pre-aggregate via query_sql"
    for i, r in enumerate(rows[:50]):
        if len(r) > MAX_FIELDS_PER_ROW:
            return (
                f"row {i} has {len(r)} fields; limit is "
                f"{MAX_FIELDS_PER_ROW}. Project to the columns you need."
            )
    return None


def _field_def(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build one channel def from the schema-validated input. Whitelist
    only — anything not in our schema is dropped on the floor."""
    out: dict[str, Any] = {
        "field": spec["field"],
        "type": spec["type"],
    }
    if "aggregate" in spec:
        out["aggregate"] = spec["aggregate"]
    if "title" in spec:
        out["title"] = spec["title"]
    return out


def _build_spec(
    *,
    mark: str,
    encoding: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    title: str | None,
    chart_id: str,
) -> dict[str, Any]:
    enc: dict[str, Any] = {
        "x": _field_def(encoding["x"]),
        "y": _field_def(encoding["y"]),
    }
    if "color" in encoding:
        enc["color"] = _field_def(encoding["color"])
    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": [dict(r) for r in rows]},
        "mark": mark,
        "encoding": enc,
        "width": "container",
        "height": 320,
        "name": chart_id,
    }
    if title:
        spec["title"] = title
    return spec
