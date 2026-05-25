"""End-to-end DuckDB tools (Cycle 18).

Run:
    .venv/Scripts/python tests/test_duckdb_tools_e2e.py

Verifies:
  query_sql:
    - SELECT COUNT(*), filtered counts, projection — return correct rows
    - INSERT/UPDATE/DROP/etc. rejected
    - Multiple statements rejected
    - Unknown entry → error
    - Column-name fuzzy match: "Name" auto-rewritten to "name"
  query_log:
    - substring filter, regex filter
    - level filter (INFO/ERROR + WARN ↔ WARNING alias)
    - since/until time bounds
    - limit + truncated flag
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_duckdb_tools_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.agent.tools import ToolContext, get_tool
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, FileEntry, Folder
from marginalia.storage import get_storage
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


CSV = (
    b"name,age,role\n"
    b"alice,30,engineer\n"
    b"bob,25,designer\n"
    b"carol,40,engineer\n"
    b"dave,35,manager\n"
    b"eve,28,engineer\n"
)


LOG = (
    "2024-03-12T10:00:01 INFO server starting\n"
    "2024-03-12T10:00:05 INFO accepting connections\n"
    "2024-03-12T10:01:30 WARN slow query detected on /api/users\n"
    "2024-03-12T10:02:00 ERROR connection refused from 10.0.0.5\n"
    "2024-03-12T10:03:15 ERROR connection refused from 10.0.0.6\n"
    "2024-03-12T10:05:00 INFO recovered after retry\n"
    "2024-03-12T10:10:00 DEBUG cache hit ratio 0.83\n"
    "2024-03-12T11:00:00 ERROR fatal disk full\n"
).encode("utf-8")


async def _seed():
    factory = get_session_factory()
    storage = get_storage()
    now = _now()

    async def _stream(b: bytes):
        async def _it():
            yield b
        return _it()

    await storage.put("00/aa/csv", await _stream(CSV), content_type="text/csv")
    await storage.put("00/aa/log", await _stream(LOG), content_type="text/plain")

    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        f_csv = File(id=new_id(), storage_key="00/aa/csv", sha256="c"*64,
                     size_bytes=len(CSV),
                     mime_type="text/csv", original_ext=".csv", kind="text",
                     summary="employees.csv", description={"sections": []},
                     extra=None, ingest_status="done", ingested_at=now,
                     created_at=now, updated_at=now)
        f_log = File(id=new_id(), storage_key="00/aa/log", sha256="l"*64,
                     size_bytes=len(LOG),
                     mime_type="text/plain", original_ext=".log", kind="text",
                     summary="server.log", description={"sections": []},
                     extra=None, ingest_status="done", ingested_at=now,
                     created_at=now, updated_at=now)
        s.add_all([f_csv, f_log]); await s.flush()

        e_csv = FileEntry(id=new_id(), folder_id=folder.id, file_id=f_csv.id,
                          display_name="employees.csv", lifecycle="active",
                          catalog_id=None, extra=None,
                          created_at=now, updated_at=now)
        e_log = FileEntry(id=new_id(), folder_id=folder.id, file_id=f_log.id,
                          display_name="server.log", lifecycle="active",
                          catalog_id=None, extra=None,
                          created_at=now, updated_at=now)
        s.add_all([e_csv, e_log]); await s.commit()
        return {"e_csv": e_csv.id, "e_log": e_log.id}


async def _call(name: str, args: dict, ctx_id: str = "c") -> dict:
    factory = get_session_factory()
    reg = get_tool(name)
    assert reg is not None
    async with factory() as s:
        ctx = ToolContext(session_id="s", conversation_id=ctx_id)
        result = await reg.handler(s, ctx, args)
        await s.commit()
    return result


async def main():
    await _create_schema()
    seeded = await _seed()

    # ---- query_sql ------------------------------------------------------
    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]],
        "sql": "SELECT COUNT(*) AS n FROM t1",
    })
    print("[1] count(*):", r)
    assert r["ok"] is True, r
    assert r["columns"] == ["n"]
    assert r["rows"] == [[5]]

    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]],
        "sql": "SELECT COUNT(*) FROM t1 WHERE role = 'engineer'",
    })
    assert r["ok"] is True
    assert r["rows"][0][0] == 3
    print("[2] engineer count:", r["rows"][0][0])

    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]],
        "sql": "SELECT name, age FROM t1 WHERE age > 30 ORDER BY age DESC",
    })
    assert r["ok"] is True
    assert len(r["rows"]) == 2
    # Auto-infer treats `age` as integer, so comparisons + projection are typed.
    assert r["rows"][0] == ["carol", 40]
    print("[3] over-30:", r["rows"])

    # rejection paths
    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]], "sql": "DROP TABLE t1",
    })
    assert r["ok"] is False
    assert "SELECT" in r["error"]
    print("[4] DROP rejected")

    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]],
        "sql": "SELECT * FROM t1; DELETE FROM t1",
    })
    assert r["ok"] is False
    assert "SELECT" in r["error"] or "one statement" in r["error"]
    print("[5] multi-statement rejected")

    r = await _call("query_sql", {
        "entry_ids": ["no-such-entry"], "sql": "SELECT 1",
    })
    assert r["ok"] is False
    assert r["error"].startswith("entry not found")
    print("[6] unknown entry handled")

    # ---- query_sql column fuzzy match (kb-lite-style) -------------------
    r = await _call("query_sql", {
        "entry_ids": [seeded["e_csv"]],
        "sql": 'SELECT "Name" FROM t1 LIMIT 1',
    })
    # The seed CSV uses lowercase "name"; auto-rewrite kicks in.
    assert r["ok"] is True, r
    assert r["column_fixes"], "expected column_fixes to record the rewrite"
    assert any("name" in f.lower() for f in r["column_fixes"])
    print("[6b] column fuzzy match:", r["column_fixes"])

    # ---- query_log -----------------------------------------------------
    r = await _call("query_log", {
        "entry_id": seeded["e_log"], "pattern": "connection refused",
    })
    print("[7] pattern matches:", r["match_count"])
    assert r["match_count"] == 2
    assert all("connection refused" in m["text"] for m in r["matches"])

    r = await _call("query_log", {
        "entry_id": seeded["e_log"], "level": "ERROR",
    })
    print("[8] ERROR lines:", r["match_count"])
    assert r["match_count"] == 3

    # WARN should also match WARNING — none here, but using WARN literal
    r = await _call("query_log", {
        "entry_id": seeded["e_log"], "level": "WARN",
    })
    assert r["match_count"] == 1
    print("[9] WARN level:", r["matches"][0]["text"])

    r = await _call("query_log", {
        "entry_id": seeded["e_log"],
        "pattern": "10\\.0\\.0\\.\\d+",
        "regex": True,
    })
    assert r["match_count"] == 2
    print("[10] regex match:", r["match_count"])

    r = await _call("query_log", {
        "entry_id": seeded["e_log"],
        "since": "2024-03-12T10:02:00",
        "until": "2024-03-12T10:09:59",
    })
    print("[11] in time window:", r["match_count"])
    assert r["match_count"] == 3   # 10:02:00, 10:03:15, 10:05:00 (10:10 > until)

    r = await _call("query_log", {
        "entry_id": seeded["e_log"], "limit": 2,
    })
    assert r["match_count"] == 2
    assert r["truncated"] is True
    print("[12] limit/truncated:", r["match_count"], r["truncated"])

    print("\nALL DUCKDB TOOLS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
