"""Compact human-readable rendering of agent tool calls for live display.

Adapts kb-lite/app/agent/tool_display.py to Marginalia's tool inventory.
Produces strings like:

    read_files paper.pdf pages 5-7
    read_files paper.pdf section s15
    search_metadata "raft", "consensus" + tags 'machine-learning'
    search_journal "leader election"
    list_files_in_folder Papers/2024
    list_folders Papers
    read_entries_metadata paper.pdf, slides.pdf
    query_sql 'select count(*) from entry where ...'

Falls back to short uuid prefixes when the resolver hasn't cached the lookup.

Two resolvers are accepted: `entry_resolver` for entry_id → display_name and
`tag_resolver` for tag_id → tag name. The runtime batches both lookups before
emitting each tool_call event so the live trace doesn't fan out to N round trips.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

NameResolver = Callable[[str], str | None]


_UUID_LIKE = 32  # any string of this length or longer is treated as an id


def _looks_like_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) >= _UUID_LIKE


def _name(eid: str | None, resolver: NameResolver | None) -> str:
    if not eid:
        return ""
    if resolver:
        n = resolver(eid)
        if n:
            return n
    return eid[:8] if len(eid) >= 8 else eid


def _tag_label(t: Any, resolver: NameResolver | None) -> str:
    """Render one tag value. If it's a uuid-shaped id, try the resolver;
    otherwise the value is already a tag name (resolve_tag input form)."""
    if not t:
        return ""
    s = str(t)
    if _looks_like_id(s) and resolver:
        n = resolver(s)
        if n:
            return n
        return s[:8]
    return s


def _entry_ids_from_args(args: Mapping[str, Any]) -> list[str]:
    """Pull entry_ids out of the common shapes Marginalia tools use."""
    out: list[str] = []
    if isinstance(args.get("entry_ids"), list):
        out.extend(str(x) for x in args["entry_ids"] if x)
    if args.get("entry_id"):
        out.append(str(args["entry_id"]))
    # read_files takes `requests: [{entry_id, reads: [...]}]`
    reqs = args.get("requests")
    if isinstance(reqs, list):
        for r in reqs:
            if isinstance(r, dict) and r.get("entry_id"):
                out.append(str(r["entry_id"]))
    return out


def _tag_ids_from_args(args: Mapping[str, Any]) -> list[str]:
    """Pull tag_ids out of search_metadata-style args. We only collect
    uuid-shaped strings; bare names go through _tag_label unchanged."""
    out: list[str] = []
    for key in ("tags_all", "tags_any", "tags_none"):
        v = args.get(key)
        if isinstance(v, list):
            for t in v:
                if _looks_like_id(t):
                    out.append(str(t))
    return out


def collect_entry_ids(name: str, args: Mapping[str, Any]) -> list[str]:
    """Return the entry_ids referenced by this call so the runtime can
    pre-resolve them in one DB round trip."""
    if not isinstance(args, dict):
        return []
    return _entry_ids_from_args(args)


def collect_tag_ids(name: str, args: Mapping[str, Any]) -> list[str]:
    """Return uuid-shaped tag_ids referenced by this call so the runtime
    can batch a single tags lookup."""
    if not isinstance(args, dict):
        return []
    return _tag_ids_from_args(args)


def _quoted_csv(values: Iterable[Any]) -> str:
    return ", ".join(f'"{v}"' for v in values if v not in (None, ""))


def _read_segment(seg: Mapping[str, Any]) -> str:
    """Format one entry of a `reads` list as `pages 5-7` / `section s3`."""
    if seg.get("section_id"):
        return f"section {seg['section_id']}"
    if seg.get("heading"):
        return f"heading {seg['heading']!r}"
    if seg.get("page_start") is not None:
        ps = seg["page_start"]
        pe = seg.get("page_end") or ps
        return f"pages {ps}-{pe}" if pe != ps else f"page {ps}"
    if seg.get("line_start") is not None:
        ls = seg["line_start"]
        le = seg.get("line_end") or ls
        return f"lines {ls}-{le}" if le != ls else f"line {ls}"
    if seg.get("paragraph_start") is not None:
        ps = seg["paragraph_start"]
        pe = seg.get("paragraph_end") or ps
        return f"paras {ps}-{pe}" if pe != ps else f"para {ps}"
    if seg.get("pattern"):
        return f'"{seg["pattern"]}"'
    if seg.get("offset") is not None or seg.get("max_chars") is not None:
        start = int(seg.get("offset") or 0)
        length = int(seg.get("max_chars") or 8000)
        return f"chars {start}-{start + length}"
    return ""


def format_tool_call(
    name: str,
    args: Mapping[str, Any] | None,
    resolver: NameResolver | None = None,
    *,
    tag_resolver: NameResolver | None = None,
) -> str:
    """Render a compact one-line description of a tool call.

    `resolver` resolves entry_id → display_name; `tag_resolver` resolves
    uuid-shaped tag_id → tag name (no-op for bare-name tag inputs).
    """
    if not isinstance(args, Mapping):
        args = {}

    parts: list[str] = [name]

    if name == "read_files":
        reqs = args.get("requests") or []
        if isinstance(reqs, list):
            chunks: list[str] = []
            for r in reqs:
                if not isinstance(r, dict):
                    continue
                fname = _name(r.get("entry_id"), resolver)
                reads = r.get("reads") or []
                segs = [
                    s for s in (_read_segment(rd) for rd in reads if isinstance(rd, dict)) if s
                ]
                if segs:
                    chunks.append(f"{fname} {', '.join(segs)}")
                elif fname:
                    chunks.append(fname)
            if chunks:
                parts.append("; ".join(chunks))
        return " ".join(parts)

    if name == "read_entries_metadata":
        eids = args.get("entry_ids") or ([args["entry_id"]] if args.get("entry_id") else [])
        names = [_name(e, resolver) for e in eids if e]
        if names:
            parts.append(", ".join(names))
        return " ".join(parts)

    if name == "search_metadata":
        text = args.get("text")
        if text:
            parts.append(f'"{text}"')
        for key, prefix in (("tags_all", "tags"), ("tags_any", "any-tags"), ("tags_none", "no-tags")):
            v = args.get(key)
            if isinstance(v, list) and v:
                labels = [_tag_label(t, tag_resolver) for t in v]
                parts.append(f"+ {prefix} " + ", ".join(f"'{t}'" for t in labels if t))
        if args.get("kind"):
            parts.append(f"+ kind {args['kind']}")
        if args.get("limit"):
            parts.append(f"(limit {args['limit']})")
        return " ".join(parts)

    if name == "search_journal":
        q = args.get("query") or args.get("q")
        if q:
            parts.append(f'"{q}"')
        if args.get("limit"):
            parts.append(f"(limit {args['limit']})")
        return " ".join(parts)

    if name == "list_files_in_folder":
        path = args.get("folder_path") or args.get("path") or args.get("folder_id")
        if path:
            parts.append(str(path))
        return " ".join(parts)

    if name == "list_folders":
        parent = args.get("parent_path") or args.get("parent_id")
        if parent:
            parts.append(str(parent))
        return " ".join(parts)

    if name == "list_catalogs":
        pid = args.get("parent_id")
        if pid:
            parts.append(str(pid))
        return " ".join(parts)

    if name == "read_catalog":
        cid = args.get("catalog_id") or args.get("catalog_path")
        if cid:
            parts.append(str(cid))
        return " ".join(parts)

    if name == "resolve_tag":
        v = args.get("name") or args.get("tag")
        if v:
            parts.append(f"'{v}'")
        return " ".join(parts)

    if name in ("query_sql", "query_log"):
        sql = (args.get("sql") or args.get("query") or "").strip().replace("\n", " ")
        if sql:
            if len(sql) > 80:
                sql = sql[:80] + "..."
            parts.append(sql)
        return " ".join(parts)

    if name == "generate_chart":
        if args.get("chart_type"):
            parts.append(f"({args['chart_type']})")
        sql = (args.get("sql") or "").strip().replace("\n", " ")
        if sql:
            if len(sql) > 60:
                sql = sql[:60] + "..."
            parts.append(sql)
        return " ".join(parts)

    if name == "analyze_container":
        eid = args.get("entry_id")
        if eid:
            parts.append(_name(eid, resolver))
        return " ".join(parts)

    if name == "materialize_view":
        vid = args.get("view_id") or args.get("view_path")
        if vid:
            parts.append(str(vid))
        return " ".join(parts)

    # Unknown tool — show top-level args, truncating long string values
    extras: list[str] = []
    for k, v in args.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, str):
            sv = v if len(v) <= 24 else v[:21] + "..."
            extras.append(f'{k}="{sv}"')
        else:
            extras.append(f"{k}={v}")
    if extras:
        line = " ".join(extras)
        if len(line) > 80:
            line = line[:77] + "..."
        parts.append(line)
    return " ".join(parts)
