"""Compact human-readable rendering of agent tool calls for live display.

Adapts kb-lite/app/agent/tool_display.py to Marginalia's tool inventory.
Produces strings like:

    read_files paper.pdf pages 5-7
    read_files paper.pdf section s15
    search_metadata "raft", "consensus" + tags 'machine-learning'
    search_journal "leader election"
    list_folder Papers/2024
    list_folder Papers
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
    """True if `s` could plausibly be an entry/tag/folder/catalog id.

    Accepts both full uuids (36 chars with dashes, 32 without) and short
    hex prefixes (>= 8 chars, hex-only after stripping dashes). The
    short-prefix case is what the agent occasionally parrots from the
    activity bar back into a tool call — we want the resolver to label
    it instead of falling through to the raw string branch.
    """
    if not isinstance(s, str):
        return False
    if len(s) >= _UUID_LIKE:
        return True
    cleaned = s.replace("-", "").lower()
    if len(cleaned) < 8:
        return False
    return all(c in "0123456789abcdef" for c in cleaned)


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


def _folder_ids_from_args(name: str, args: Mapping[str, Any]) -> list[str]:
    """Pull folder ids out of folder-aware tools. list_folder uses
    parent_id; parent_id can also be null (root) — skip those."""
    out: list[str] = []
    if name == "list_folder":
        v = args.get("parent_id")
        if _looks_like_id(v):
            out.append(str(v))
    return out


def _catalog_ids_from_args(name: str, args: Mapping[str, Any]) -> list[str]:
    """Pull catalog ids out of catalog-aware tools. list_catalogs →
    parent_id; read_catalog → id."""
    out: list[str] = []
    if name == "list_catalogs":
        v = args.get("parent_id")
        if _looks_like_id(v):
            out.append(str(v))
    elif name == "read_catalog":
        v = args.get("id") or args.get("catalog_id") or args.get("catalog_path")
        if _looks_like_id(v):
            out.append(str(v))
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


def collect_folder_ids(name: str, args: Mapping[str, Any]) -> list[str]:
    """Return uuid-shaped folder ids referenced by this call so the
    runtime can batch a single folders lookup."""
    if not isinstance(args, dict):
        return []
    return _folder_ids_from_args(name, args)


def collect_catalog_ids(name: str, args: Mapping[str, Any]) -> list[str]:
    """Return uuid-shaped catalog ids referenced by this call so the
    runtime can batch a single catalogs lookup."""
    if not isinstance(args, dict):
        return []
    return _catalog_ids_from_args(name, args)


def _quoted_csv(values: Iterable[Any]) -> str:
    return ", ".join(f'"{v}"' for v in values if v not in (None, ""))


def _read_segment(seg: Mapping[str, Any]) -> str:
    """Format one entry of a `reads` list as a human anchor.

    `section_id` (s1/s2/…) is the LLM's internal handle; we don't
    surface it. If the agent passes only a section_id, fall through to
    showing the heading or another anchor it also includes; if there's
    nothing else we'd rather render an empty string than a meaningless
    `section s1`.
    """
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
    folder_resolver: NameResolver | None = None,
    catalog_resolver: NameResolver | None = None,
) -> str:
    """Render a compact one-line description of a tool call.

    Resolvers map ids to user-visible names so the live trace shows
    "list_folder Papers" instead of the raw uuid the agent passed:
      - `resolver` — entry_id → display_name
      - `tag_resolver` — uuid-shaped tag_id → tag name (no-op for
        bare-name tag inputs that the agent already typed by name)
      - `folder_resolver` — folder_id → folder name
      - `catalog_resolver` — catalog_id → catalog name
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
        q = args.get("text") or args.get("query") or args.get("q")
        if q:
            parts.append(f'"{q}"')
        kinds = args.get("kinds") or []
        if kinds and kinds != ["insight"]:
            parts.append(f"kinds={kinds}")
        if args.get("entry_id"):
            parts.append(_name(args["entry_id"], resolver))
        if args.get("limit"):
            parts.append(f"(limit {args['limit']})")
        return " ".join(parts)

    if name == "list_folder":
        pid = args.get("parent_id")
        ppath = args.get("path")
        if pid:
            label = _name(str(pid), folder_resolver) if _looks_like_id(pid) else str(pid)
            parts.append(label)
        elif ppath:
            parts.append(str(ppath))
        return " ".join(parts)

    if name == "list_catalogs":
        v = args.get("parent_id")
        if v:
            label = _name(str(v), catalog_resolver) if _looks_like_id(v) else str(v)
            parts.append(label)
        return " ".join(parts)

    if name == "read_catalog":
        v = args.get("id") or args.get("catalog_id") or args.get("catalog_path")
        if v:
            label = _name(str(v), catalog_resolver) if _looks_like_id(v) else str(v)
            parts.append(label)
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


# ---------------------------------------------------------------------------
# Tool-result preview rendering
# ---------------------------------------------------------------------------
#
# `_dispatch_tool_calls` used to send the model's raw JSON result string as
# the user-facing preview, which made the GUI's "expand result" panel a wall
# of `{"folders":[{"id":"019e..."}]}`. Each tool emits a known shape, so we
# can summarise it as one short line ("4 folders", "raft-paper.pdf · 3 reads").
# The model still gets the full JSON; only the user-facing preview changes.


def _ru(items: list[Any], unit: str, plural: str | None = None) -> str:
    n = len(items)
    if n == 1:
        return f"1 {unit}"
    return f"{n} {plural or unit + 's'}"


def _truncate(s: str, n: int = 200) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def format_tool_result_preview(name: str, result: Any) -> str:
    """Return a short human-readable summary of a tool result.

    Falls back to a generic key:count line for unknown shapes so the
    preview is never raw JSON. Errors come through with their `error`
    field so the user can see why something failed without expanding.
    """
    if not isinstance(result, dict):
        if isinstance(result, list):
            return _ru(result, "item")
        s = str(result) if result is not None else ""
        return _truncate(s, 200)

    if result.get("error"):
        return f"error: {_truncate(str(result['error']), 160)}"

    if name == "list_folder":
        rows = result.get("folders") or []
        entries = result.get("entries") or []
        bits: list[str] = []
        if rows:
            names = [r.get("name") or r.get("id", "") for r in rows[:5] if isinstance(r, dict)]
            head = ", ".join(n for n in names if n)
            more = "" if len(rows) <= 5 else f" +{len(rows) - 5} more"
            bits.append(f"{_ru(rows, 'folder')}: {head}{more}")
        else:
            bits.append("no subfolders")
        if entries:
            names = [r.get("display_name") or "" for r in entries[:5] if isinstance(r, dict)]
            head = ", ".join(n for n in names if n)
            more = "" if len(entries) <= 5 else f" +{len(entries) - 5} more"
            bits.append(f"{_ru(entries, 'file')}: {head}{more}")
        return " · ".join(bits) if bits else "empty"

    if name == "list_catalogs":
        rows = result.get("catalogs") or []
        if not rows:
            return "no catalogs"
        names = [r.get("name") or "" for r in rows[:5] if isinstance(r, dict)]
        head = ", ".join(n for n in names if n)
        more = "" if len(rows) <= 5 else f" +{len(rows) - 5} more"
        return f"{_ru(rows, 'catalog')}: {head}{more}"

    if name == "read_catalog":
        cname = result.get("name") or "(unnamed)"
        kids = result.get("children") or []
        ents = result.get("entries") or []
        bits = [cname]
        if kids:
            bits.append(_ru(kids, "subcatalog"))
        if ents:
            bits.append(_ru(ents, "entry", "entries"))
        if not kids and not ents:
            bits.append("empty")
        return " · ".join(bits)

    if name == "read_files":
        results = result.get("results") or []
        if not results:
            return "no results"
        chunks: list[str] = []
        for r in results[:3]:
            if not isinstance(r, dict):
                continue
            disp = r.get("display_name") or (r.get("entry_id", "")[:8])
            if r.get("ok") is False:
                # Surface either the entry-level error (e.g. "entry not
                # found", "ingest_status=pending") or the first read's
                # error if the entry resolved but a slice failed. Without
                # this the agent's repeated retries against a bad id
                # looked like a silent loop in the activity bar.
                err = r.get("error")
                if not err:
                    for rd in (r.get("reads") or []):
                        if isinstance(rd, dict) and rd.get("error"):
                            err = rd["error"]
                            break
                if err:
                    chunks.append(f"{disp} failed: {_truncate(str(err), 80)}")
                else:
                    chunks.append(f"{disp} failed")
                continue
            reads = r.get("reads") or []
            chunks.append(f"{disp} · {_ru(reads, 'read')}" if reads else disp)
        more = "" if len(results) <= 3 else f" +{len(results) - 3} more"
        return "; ".join(chunks) + more

    if name == "read_entries_metadata":
        rows = result.get("entries") or []
        errors = result.get("errors") or []
        if not rows and errors:
            err = errors[0].get("error") if isinstance(errors[0], dict) else None
            return f"error: {_truncate(str(err or 'invalid entry_id'), 160)}"
        if not rows:
            return "no entries"
        names = [r.get("display_name") or "" for r in rows[:5] if isinstance(r, dict)]
        head = ", ".join(n for n in names if n)
        more = "" if len(rows) <= 5 else f" +{len(rows) - 5} more"
        tail = f" · {len(errors)} invalid id{'s' if len(errors) != 1 else ''}" if errors else ""
        return f"{_ru(rows, 'entry', 'entries')}: {head}{more}{tail}"

    if name == "search_metadata":
        rows = result.get("entries") or []
        if not rows:
            return "no matches"
        names = [r.get("display_name") or "" for r in rows[:5] if isinstance(r, dict)]
        head = ", ".join(n for n in names if n)
        more = "" if len(rows) <= 5 else f" +{len(rows) - 5} more"
        return f"{_ru(rows, 'match', 'matches')}: {head}{more}"

    if name == "search_journal":
        notes = result.get("notes") or []
        if not notes:
            return "no notes"
        first = notes[0].get("note") if isinstance(notes[0], dict) else None
        head = _truncate(first, 100) if first else ""
        more = "" if len(notes) <= 1 else f" +{len(notes) - 1} more"
        return f"{_ru(notes, 'note')}: {head}{more}" if head else _ru(notes, "note")

    if name == "resolve_tag":
        if result.get("found"):
            n = result.get("name") or ""
            f = result.get("facet") or ""
            return f"found '{n}' ({f})" if f else f"found '{n}'"
        suggestions = result.get("suggestions") or []
        if suggestions:
            sn = [s.get("name") or "" for s in suggestions[:3] if isinstance(s, dict)]
            return "no exact match · suggestions: " + ", ".join(n for n in sn if n)
        return "no match"

    if name in ("query_sql", "query_log"):
        rows = result.get("rows") or result.get("results") or []
        if isinstance(rows, list):
            return f"{_ru(rows, 'row')}"
        if "count" in result:
            return f"{result['count']} rows"
        return "ok"

    if name == "generate_chart":
        cid = result.get("chart_id") or result.get("id")
        ct = result.get("chart_type") or ""
        if cid:
            return f"{ct} chart" if ct else "chart"
        return "ok"

    if name == "analyze_container":
        members = result.get("members") or result.get("entries") or []
        if isinstance(members, list) and members:
            return _ru(members, "member")
        return "ok"

    if name == "materialize_view":
        ents = result.get("entries") or result.get("entry_ids") or []
        if isinstance(ents, list):
            return _ru(ents, "entry", "entries")
        return "ok"

    # Unknown / future tools — skim top-level shape.
    if "count" in result and isinstance(result["count"], int):
        return f"{result['count']} items"
    keys = [k for k in result.keys() if not k.startswith("_")]
    if not keys:
        return "ok"
    return _truncate("ok · " + ", ".join(keys[:6]), 160)
