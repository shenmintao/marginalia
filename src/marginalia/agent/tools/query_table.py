"""query_table — design.md §10.1.

Open a tabular file (csv / parquet / xlsx) into an in-memory DuckDB
session and execute a SELECT query. The connection is fresh per call
and discarded afterwards (memory.md: DuckDB is agent-time only, never
persistence).

Safety:
  - Caller can only run a single SELECT statement. Anything that would
    mutate state is rejected at parse time. (DuckDB is in-memory anyway,
    so even if mutation slipped through it could not affect Marginalia
    state — but rejecting up-front gives clearer error messages.)
  - Result row count is capped at MAX_ROWS to keep prompt budgets sane.
  - The table loaded from the file is always exposed as `t` so the LLM
    has a stable name regardless of the underlying filename.
"""
from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import File, FileEntry
from marginalia.storage import get_storage


MAX_ROWS = 1_000
MAX_BYTES = 32 * 1024 * 1024  # 32 MB cap to load


_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|COPY|PRAGMA|EXPORT|"
    r"INSTALL|LOAD|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_id", "sql"],
    "properties": {
        "entry_id": {"type": "string"},
        "sql": {
            "type": "string",
            "description": (
                "A single SELECT against the loaded table aliased as `t`. "
                "Example: `SELECT COUNT(*) FROM t WHERE age > 30`."
            ),
        },
    },
}


@tool(
    name="query_table",
    description=(
        "Run a SELECT against a CSV / Parquet / XLSX entry. The file is "
        "loaded into an in-memory DuckDB session and the loaded relation "
        "is exposed as `t`. Reject anything other than SELECT."
    ),
    schema=SCHEMA,
)
async def query_table(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    entry_id = args["entry_id"]
    sql = (args.get("sql") or "").strip()
    if not sql:
        return {"error": "sql is required"}
    if _FORBIDDEN_SQL.search(sql):
        return {"error": "only SELECT statements are allowed"}
    if ";" in sql.rstrip(";"):
        return {"error": "only one statement allowed (no semicolons)"}

    pair = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id == entry_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        return {"error": "entry not found", "entry_id": entry_id}
    entry, file_row = pair

    ext = (file_row.original_ext or "").lower().lstrip(".")
    mime = (file_row.mime_type or "").lower()
    storage = get_storage()
    body = bytearray()
    async for chunk in storage.get(file_row.storage_key):
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            return {"error": f"file exceeds {MAX_BYTES} bytes load cap"}

    return _run_duckdb(
        body=bytes(body), ext=ext, mime=mime, sql=sql,
        display_name=entry.display_name,
    )


def _run_duckdb(
    *, body: bytes, ext: str, mime: str, sql: str, display_name: str,
) -> dict[str, Any]:
    import duckdb

    # DuckDB needs a real filesystem path to use its built-in readers.
    suffix = "." + (ext or _ext_from_mime(mime) or "csv")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(body)
        path = fh.name

    try:
        con = duckdb.connect(database=":memory:", read_only=False)
        try:
            loader = _loader_for(suffix)
            if loader is None:
                return {"error": f"unsupported file extension: {suffix!r}"}
            create = f"CREATE VIEW t AS {loader.format(path=_quote(path))}"
            con.execute(create)
            res = con.execute(sql)
            cols = [d[0] for d in res.description] if res.description else []
            rows: list[list[Any]] = []
            for row in res.fetchmany(MAX_ROWS):
                rows.append(list(row))
            truncated = len(rows) >= MAX_ROWS
            return {
                "display_name": display_name,
                "columns": cols,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            }
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"duckdb error: {exc!r}"}
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def _loader_for(suffix: str) -> str | None:
    s = suffix.lower()
    if s in (".csv", ".tsv"):
        return "SELECT * FROM read_csv_auto({path}, header=True, sample_size=-1)"
    if s in (".parquet", ".pq"):
        return "SELECT * FROM read_parquet({path})"
    if s in (".xlsx", ".xls"):
        # excel reader requires the spatial extension; we attempt the
        # built-in `read_excel` (DuckDB 0.10+) and fall back to error.
        return "SELECT * FROM read_excel({path})"
    if s in (".json", ".jsonl", ".ndjson"):
        return "SELECT * FROM read_json_auto({path})"
    return None


def _ext_from_mime(mime: str) -> str | None:
    if "csv" in mime:
        return "csv"
    if "json" in mime:
        return "json"
    if "parquet" in mime:
        return "parquet"
    if "excel" in mime or "spreadsheet" in mime:
        return "xlsx"
    return None


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
