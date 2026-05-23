"""Seed Marginalia with the bundled sample files.

Run after the API server (and ideally the worker) are up:

    .venv/Scripts/python samples/seed.py

It uploads each `samples/*` file under `/samples/`, polls until ingest
completes, then prints a one-line summary per file. Idempotent — re-runs
hit the dedup path (no duplicate ingests).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx


SAMPLES_DIR = Path(__file__).resolve().parent
REMOTE_FOLDER = "/samples/"

# (filename, content_type)
FILES = [
    ("raft-paxos.md", "text/markdown"),
    ("quickstart.md", "text/markdown"),
    ("architecture.md", "text/markdown"),
    ("release-notes.md", "text/markdown"),
    ("employees.csv", "text/csv"),
    ("server.log", "text/plain"),
]


async def _upload(client: httpx.AsyncClient, name: str, content_type: str) -> dict:
    body = (SAMPLES_DIR / name).read_bytes()
    r = await client.post(
        "/upload",
        params={"remote_path": REMOTE_FOLDER, "on_conflict": "skip"},
        files={"file": (name, body, content_type)},
    )
    r.raise_for_status()
    return r.json()


async def _poll(client: httpx.AsyncClient, entry_id: str,
                *, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/file-entries/{entry_id}/metadata")
        if r.status_code == 200:
            meta = r.json()
            if meta.get("ingest_status") == "done":
                return meta
            if meta.get("ingest_status") == "failed":
                return meta
        await asyncio.sleep(0.5)
    return {"ingest_status": "timeout", "entry_id": entry_id}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Marginalia samples.")
    parser.add_argument("--server", default="http://127.0.0.1:8000",
                        help="Marginalia server base URL")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Per-file ingest poll timeout in seconds")
    args = parser.parse_args()

    print(f"connecting to {args.server} ...")
    async with httpx.AsyncClient(base_url=args.server, timeout=30.0) as c:
        try:
            r = await c.get("/health")
            r.raise_for_status()
        except Exception as e:
            print(f"  could not reach server: {e}")
            print(f"  is uvicorn running? try: uvicorn marginalia.main:app")
            return 2

        results: list[tuple[str, dict]] = []
        for name, ct in FILES:
            try:
                up = await _upload(c, name, ct)
            except httpx.HTTPStatusError as exc:
                print(f"  ✗ {name}: upload failed: HTTP {exc.response.status_code}")
                continue
            print(f"  uploaded {name:<22} entry={up['entry_id'][:8]}…"
                  + ("  (skipped, already exists)" if up.get("skipped") else "")
                  + ("  (deduped)" if up.get("deduped") else ""))
            meta = await _poll(c, up["entry_id"], timeout=args.timeout)
            results.append((name, meta))

        print()
        print("=" * 78)
        print(f"{'file':<22} {'state':<10}  summary")
        print("-" * 78)
        for name, meta in results:
            status = meta.get("ingest_status") or "?"
            summary = (meta.get("summary") or "(none)").replace("\n", " ")
            if len(summary) > 50:
                summary = summary[:47] + "…"
            print(f"{name:<22} {status:<10}  {summary}")

        # offer suggestions for next steps
        print()
        if any(r[1].get("ingest_status") != "done" for r in results):
            print("note: some files are still ingesting. Make sure the worker")
            print("      process is running (or set WORKER_ENABLED=true on the")
            print("      API server) and re-run this script to refresh.")
        else:
            print("all samples ingested. try the CLI:")
            print(f"  marginalia --server {args.server}")
            print(f"  marginalia> /tree")
            print(f"  marginalia> /search consensus")
            print(f"  marginalia> 帮我对比 Raft 和 Paxos")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("interrupted")
        sys.exit(130)
