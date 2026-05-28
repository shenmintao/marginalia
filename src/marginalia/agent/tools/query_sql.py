"""query_sql — agent tool for read-only SELECT over user data files.

Loads CSV / XLSX / JSON / Parquet entries into an in-memory DuckDB
session as t1, t2, t3 ... in the order entry_ids are passed, then runs
one SELECT statement and returns the rows. The connection is fresh per
call and discarded afterwards (memory.md: DuckDB is agent-time only,
never persistence).

Improvements over the legacy single-table tool:

  * Multiple tables in one query (joins / unions across user files).
  * XLSX with multiple sheets is auto-loaded into one table with a
    `__sheet_name` column so the agent can filter:
        WHERE __sheet_name = 'Q4-orders'
    All sheets are read via openpyxl + pandas (no DuckDB extension
    install at runtime, which is brittle on locked-down environments).
  * Column-name fuzzy matching: `"Order Total"` written by the LLM as
    `"order_total"` is auto-rewritten to the canonical name with a
    note in the result, so a typo doesn't waste a turn.
  * On "column not found", the result includes a "did you mean" hint
    based on case/whitespace-normalized similarity.

Safety: only SELECT (parser-level reject of INSERT / UPDATE / DELETE /
DROP / ATTACH / COPY / INSTALL / LOAD etc.). DuckDB is in-memory anyway
so even a bypass cannot mutate Marginalia state, but we keep the guard
so the model gets a clear error instead of a misleading success.
"""
from __future__ import annotations

import csv
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.config import get_settings
from marginalia.db.models import File, FileEntry
from marginalia.repositories import entries as entries_repo
from marginalia.storage import get_storage
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

ALLOWED_EXTS = {"csv", "tsv", "xlsx", "xls", "json", "parquet", "pq"}
MAX_RESULT_ROWS = 500
MAX_RESULT_CHARS = 40_000
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_EXPORT_ROWS = 100_000
SHEET_NAME_COLUMN = "__sheet_name"

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|COPY|PRAGMA|EXPORT|"
    r"INSTALL|LOAD|TRUNCATE|GRANT|REVOKE|MERGE|REPLACE)\b",
    re.IGNORECASE,
)
_DANGEROUS_FUNCS = re.compile(
    r"\b(read_csv_auto|read_csv|read_xlsx|read_json_auto|read_parquet|"
    r"read_text|read_blob|copy|export|write_csv|st_read|httpfs|"
    r"load|install|attach)\s*\(",
    re.IGNORECASE,
)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_ids", "sql"],
    "properties": {
        "entry_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": (
                "Entry UUIDs (or short hex prefixes, ≥ 8 chars) of tabular "
                "files to load. NOT file names — resolve via "
                "search_metadata / list_folder first. They become tables "
                "t1, t2, t3 ... in the same order as listed."
            ),
        },
        "sql": {
            "type": "string",
            "description": (
                "One SELECT statement against tables t1, t2, ... Joins, "
                "aggregates, window functions allowed. XLSX entries gain a "
                "`__sheet_name` column for sheet filtering."
            ),
        },
        "offset": {
            "type": "integer", "minimum": 0,
            "description": (
                "Skip first N rows of the result. Default 0. Combine with "
                "the row cap (500) to page through large result sets."
            ),
        },
        "export_csv": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also write the selected result set to MARGINALIA_HOME/exports "
                "as CSV and emit a user-only artifact. Export is capped at "
                f"{MAX_EXPORT_ROWS} rows."
            ),
        },
    },
}


@tool(
    name="query_sql",
    description=(
        "Run a read-only SELECT against tabular file entries (CSV, TSV, "
        "XLSX, JSON, Parquet) using DuckDB. Multiple entry_ids load as "
        "t1, t2, ... in order so you can JOIN across them. XLSX sheets "
        "are merged with a `__sheet_name` column. Use read_files first "
        "to inspect column names; the result also auto-corrects "
        "case/whitespace mismatches. Results cap at 500 rows; pass "
        "`offset` (with the same SQL) to page through more. Set "
        "`export_csv` when the user needs the full selected result as a file."
    ),
    schema=SCHEMA,
)
async def query_sql(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    entry_ids: list[str] = list(args.get("entry_ids") or [])
    sql: str = (args.get("sql") or "").strip()
    offset = max(0, int(args.get("offset") or 0))
    export_csv = bool(args.get("export_csv") or False)

    if not entry_ids:
        return {"ok": False, "error": "entry_ids must be a non-empty list"}
    if not sql:
        return {"ok": False, "error": "sql is required"}

    err = _validate_sql(sql)
    if err:
        return {"ok": False, "error": err}

    # Resolve entries → files
    records: list[tuple[FileEntry, File, str]] = []
    storage = get_storage()
    for raw_eid in entry_ids:
        eid, resolve_err = await entries_repo.resolve_entry_id_prefix(db, str(raw_eid))
        if resolve_err:
            return {"ok": False, "error": resolve_err}
        pair = await entries_repo.get_live_with_file(db, eid)
        if pair is None:
            return {"ok": False, "error": f"entry not found: {eid}"}
        entry, f = pair
        if f.size_bytes and f.size_bytes > MAX_FILE_BYTES:
            return {
                "ok": False,
                "error": (
                    f"file '{entry.display_name}' is "
                    f"{f.size_bytes // (1024*1024)}MB, exceeds "
                    f"{MAX_FILE_BYTES // (1024*1024)}MB limit"
                ),
            }
        ext = _ext_for(entry.display_name, f.original_ext, f.mime_type)
        if ext not in ALLOWED_EXTS:
            return {
                "ok": False,
                "error": (
                    f"entry '{entry.display_name}' is not tabular "
                    f"(detected ext={ext}); supported: "
                    f"{sorted(ALLOWED_EXTS)}"
                ),
            }
        records.append((entry, f, ext))

    export_dir = (
        Path(get_settings().marginalia_home).expanduser() / "exports"
        if export_csv
        else None
    )
    return await _run_in_tempdir(records, sql, storage, offset, export_dir)


# -- helpers ---------------------------------------------------------------

def _validate_sql(sql: str) -> str | None:
    if _FORBIDDEN_SQL.search(sql):
        return "only read-only SELECT statements are allowed"
    if _DANGEROUS_FUNCS.search(sql):
        return "dangerous function call rejected (no read_csv/attach/load/...)"
    if ";" in sql.rstrip(";"):
        return "exactly one statement allowed (no semicolons except at end)"
    if not re.match(r"\s*(WITH|SELECT)\b", sql, re.IGNORECASE):
        return "query must begin with SELECT or WITH"
    return None


def _ext_for(display_name: str | None, original_ext: str | None, mime: str | None) -> str:
    if original_ext:
        e = original_ext.lstrip(".").lower()
        if e:
            return e
    if display_name:
        ext = Path(display_name).suffix.lstrip(".").lower()
        if ext:
            return ext
    if mime:
        m = mime.lower()
        if "csv" in m:
            return "csv"
        if "tab-separated" in m or m.endswith("/tsv"):
            return "tsv"
        if "spreadsheetml" in m or "excel" in m:
            return "xlsx"
        if "json" in m:
            return "json"
        if "parquet" in m:
            return "parquet"
    return ""


async def _run_in_tempdir(
    records: list[tuple[FileEntry, File, str]],
    sql: str,
    storage,
    offset: int,
    export_dir: Path | None,
) -> dict[str, Any]:
    """Stream files to a tempdir, run DuckDB sync in a thread."""
    import asyncio

    tmpdir = Path(tempfile.mkdtemp(prefix="marg_query_sql_"))
    try:
        on_disk: list[tuple[str, FileEntry, File]] = []
        for i, (entry, f, ext) in enumerate(records):
            local = tmpdir / f"t{i + 1}.{ext}"
            await _stream_to_disk(storage, f, local)
            on_disk.append((str(local), entry, f))

        return await asyncio.to_thread(
            _run_duckdb, on_disk, sql, records, offset, export_dir
        )
    finally:
        for p in tmpdir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass


async def _stream_to_disk(storage, file_row: File, target: Path) -> None:
    with target.open("wb") as fh:
        async for chunk in storage.get(file_row.storage_key):
            fh.write(chunk)


def _run_duckdb(
    on_disk: list[tuple[str, FileEntry, File]],
    sql: str,
    records: list[tuple[FileEntry, File, str]],
    offset: int,
    export_dir: Path | None,
) -> dict[str, Any]:
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        all_cols: list[dict[str, str]] = []
        tables: list[dict[str, Any]] = []
        for i, ((path, entry, f), (_, _, ext)) in enumerate(
            zip(on_disk, records)
        ):
            table = f"t{i + 1}"
            safe_path = path.replace("\\", "/")
            try:
                _load_table(conn, table, safe_path, ext)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": (
                        f"failed to load entry {entry.id} "
                        f"({entry.display_name}) as {table}: {exc!r}"
                    ),
                }
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ?",
                [table],
            ).fetchall()
            row_count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            col_list = [{"name": c[0], "type": c[1]} for c in cols]
            all_cols.extend(col_list)
            tables.append({
                "alias": table,
                "entry_id": entry.id,
                "display_name": entry.display_name,
                "columns": col_list,
                "row_count": int(row_count),
            })

        rewritten, fixes = _reconcile_columns(sql, all_cols)
        try:
            cur = conn.execute(rewritten)
        except Exception as exc:
            hint = _suggest_column(repr(exc), all_cols)
            return {
                "ok": False,
                "error": str(exc),
                "hint": hint,
                "tables": tables,
                "rewritten_sql": rewritten if rewritten != sql else None,
            }
        col_names = [d[0] for d in cur.description] if cur.description else []
        if offset:
            # Discard the first `offset` rows. Fetch in modest chunks to keep
            # memory bounded for very wide result sets.
            remaining = offset
            while remaining > 0:
                chunk = cur.fetchmany(min(remaining, 1000))
                if not chunk:
                    break
                remaining -= len(chunk)
        fetched = cur.fetchmany(MAX_RESULT_ROWS + 1)
        export_rows: list[tuple[Any, ...]] | None = None
        export_truncated = False
        if export_dir is not None:
            export_rows = list(fetched)
            while len(export_rows) <= MAX_EXPORT_ROWS:
                need = MAX_EXPORT_ROWS + 1 - len(export_rows)
                if need <= 0:
                    break
                chunk = cur.fetchmany(min(need, 5000))
                if not chunk:
                    break
                export_rows.extend(chunk)
            export_truncated = len(export_rows) > MAX_EXPORT_ROWS
            if export_truncated:
                export_rows = export_rows[:MAX_EXPORT_ROWS]

        truncated = len(fetched) > MAX_RESULT_ROWS
        rows = fetched[:MAX_RESULT_ROWS]
        # Stringify cells (DuckDB returns native types — keep result JSON-able)
        flat_rows = [[_to_json_safe(v) for v in r] for r in rows]

        result = {
            "ok": True,
            "tables": tables,
            "columns": col_names,
            "rows": flat_rows,
            "row_count": len(flat_rows),
            "truncated": truncated,
            "has_more": truncated,
            "column_fixes": fixes,
            "rewritten_sql": rewritten if rewritten != sql else None,
        }
        if truncated:
            result["next_offset"] = offset + len(flat_rows)
        if export_dir is not None and export_rows is not None:
            export = _write_csv_export(export_dir, col_names, export_rows, export_truncated)
            result["export"] = export
            result["__user_only__"] = {
                "kind": "data_export",
                "format": "csv",
                "filename": export["filename"],
                "path": export["path"],
                "row_count": export["row_count"],
                "truncated": export["truncated"],
                "columns": col_names,
            }
        # Cap output size — model context cost
        approx = sum(len(str(c)) for r in flat_rows for c in r)
        if approx > MAX_RESULT_CHARS:
            keep = max(1, len(flat_rows) * MAX_RESULT_CHARS // approx)
            result["rows"] = flat_rows[:keep]
            result["row_count"] = keep
            result["truncated"] = True
            result["has_more"] = True
            result["next_offset"] = offset + keep
            result["truncation_reason"] = (
                f"result body exceeded {MAX_RESULT_CHARS} chars; "
                f"kept first {keep} rows"
            )
        return result
    finally:
        conn.close()


def _write_csv_export(
    export_dir: Path,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    truncated: bool,
) -> dict[str, Any]:
    export_dir.mkdir(parents=True, exist_ok=True)
    export_id = "qs_" + new_id().split("-")[0]
    path = export_dir / f"{export_id}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_to_json_safe(value) for value in row])
    return {
        "kind": "csv",
        "filename": path.name,
        "path": str(path),
        "row_count": len(rows),
        "truncated": truncated,
        "max_rows": MAX_EXPORT_ROWS,
    }


def _load_table(conn, table: str, path: str, ext: str) -> None:
    if ext == "csv":
        # sample_size=-1 → auto-infer types from the whole file. Lets the
        # agent write natural `age > 30` style filters without needing
        # explicit CAST. Mixed-type columns may end up VARCHAR; that's
        # acceptable since the model gets the column type back in
        # `tables[].columns` and can adapt.
        conn.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_csv_auto(?, '
            f"header=true, sample_size=-1)",
            [path],
        )
    elif ext == "tsv":
        conn.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_csv_auto(?, '
            f"header=true, sample_size=-1, sep='\\t')",
            [path],
        )
    elif ext in ("xlsx", "xls"):
        _load_excel_via_pandas(conn, table, path)
    elif ext == "json":
        conn.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_json_auto(?)',
            [path],
        )
    elif ext in ("parquet", "pq"):
        conn.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_parquet(?)',
            [path],
        )
    else:
        raise ValueError(f"unsupported extension: {ext}")


def _load_excel_via_pandas(conn, table: str, path: str) -> None:
    """All sheets → one table with __sheet_name column.

    XLSX support depends on pandas + openpyxl being installed. If the
    user hasn't pulled them in yet, raise a clear error rather than
    crashing in the DuckDB layer.
    """
    try:
        import pandas as pd  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "xlsx support needs pandas + openpyxl installed; "
            "convert to CSV/Parquet or `pip install pandas openpyxl` first"
        ) from exc

    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    if not sheets:
        conn.execute(f'CREATE TABLE "{table}" (dummy VARCHAR)')
        return
    frames = []
    for sheet_name, df in sheets.items():
        df = df.copy()
        df[SHEET_NAME_COLUMN] = sheet_name
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True, sort=False)
    conn.register(f"_pd_{table}", combined)
    conn.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _pd_{table}')
    conn.unregister(f"_pd_{table}")


def _to_json_safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s.lower().strip())


def _reconcile_columns(sql: str, columns: list[dict]) -> tuple[str, list[str]]:
    """Auto-correct quoted column names that differ only by case/whitespace."""
    if not columns:
        return sql, []
    canonical: dict[str, str] = {}
    for c in columns:
        key = _normalize(c["name"])
        if key and key not in canonical:
            canonical[key] = c["name"]
    fixes: list[str] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        canon = canonical.get(_normalize(name))
        if canon and canon != name:
            fixes.append(f'"{name}" -> "{canon}"')
            return f'"{canon}"'
        return m.group(0)

    return re.sub(r'"([^"]+)"', repl, sql), fixes


def _suggest_column(err: str, columns: list[dict]) -> str:
    m = re.search(r'"([^"]+)" not found|column "([^"]+)"', err, re.IGNORECASE)
    if not m:
        return ""
    missing = m.group(1) or m.group(2) or ""
    if not missing:
        return ""
    key = _normalize(missing)
    for c in columns:
        if _normalize(c["name"]) == key:
            return f'did you mean "{c["name"]}"?'
    # near-match by prefix
    for c in columns:
        if _normalize(c["name"]).startswith(key) or key.startswith(_normalize(c["name"])):
            return f'closest column: "{c["name"]}"'
    return ""
