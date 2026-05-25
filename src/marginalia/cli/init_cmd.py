"""`marginalia init` — bootstrap a Marginalia project directory.

Mirrors claw-code's `claw init`: creates the bare minimum so the user can
run the server + worker without manual setup.

Created/updated artifacts (in `cwd`):
  - .env             starter config (only if missing)
  - data/            for SQLite + storage objects
  - .marginalia/     local cache (sessions, tmp)
  - .gitignore       appended with Marginalia-local artifacts

Schema creation happens automatically on first server / worker startup
(see `marginalia.db.bootstrap`), so this command does not touch the DB.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable


_STARTER_ENV = """\
# Marginalia configuration. See README.md for the full set.

# Project-local home — db, library, and objects all live under ./data/.
# Comment this out (or change to ~/Marginalia) to use the user-global home.
MARGINALIA_HOME=./data

DB_BACKEND=sqlite

# mirror = folder-tree on disk matching the user's intent (default)
# local  = UUID-flat object pool; faster, dedup-on, less human-friendly
# s3     = remote object storage; configure S3_* below
STORAGE_BACKEND=mirror

# Set WORKER_ENABLED=true for development mode (TaskRunner runs in the
# uvicorn process). Production: keep this false and run `marginalia-worker`
# as a separate process.
WORKER_ENABLED=false

# LLM defaults — every profile (chat / reflect / ingest / vision / audio)
# inherits these unless overridden by LLM_<PROFILE>_* keys.
LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_API_KEY=
LLM_DEFAULT_MODEL=gpt-4o-mini

# Per-profile overrides (uncomment to use):
# LLM_REFLECT_MODEL=gpt-4o
# LLM_VISION_MODEL=gpt-4o
"""


_GITIGNORE_COMMENT = "# Marginalia local artifacts"
_GITIGNORE_ENTRIES = (
    ".env",
    "data/",
    ".marginalia/",
    "*.db",
    "*.db-shm",
    "*.db-wal",
)


class _Status(Enum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped (already exists)"


@dataclass(slots=True)
class _Artifact:
    name: str
    status: _Status


def init_project(cwd: Path) -> list[_Artifact]:
    """Create / update the bootstrap files. Caller pretty-prints the report."""
    out: list[_Artifact] = []

    out.append(_Artifact(".env", _write_if_missing(cwd / ".env", _STARTER_ENV)))
    out.append(_Artifact("data/", _ensure_dir(cwd / "data")))
    out.append(_Artifact("data/library/", _ensure_dir(cwd / "data" / "library")))
    out.append(_Artifact(".marginalia/", _ensure_dir(cwd / ".marginalia")))
    out.append(_Artifact(".gitignore", _ensure_gitignore(cwd / ".gitignore")))
    return out


def render_report(cwd: Path, artifacts: list[_Artifact]) -> str:
    lines = [
        "marginalia init",
        f"  Project          {cwd}",
    ]
    for a in artifacts:
        lines.append(f"  {a.name:<18} {a.status.value}")
    lines.append("")
    lines.append("Next steps:")
    lines.append("  1. Edit .env and set LLM_DEFAULT_API_KEY (or per-profile keys).")
    lines.append("  2. Start the API:            uvicorn marginalia.main:app")
    lines.append("  3. (Production) start the worker:  marginalia-worker")
    lines.append("  4. Open the CLI:             marginalia")
    lines.append("")
    return "\n".join(lines)


def _ensure_dir(p: Path) -> _Status:
    if p.is_dir():
        return _Status.SKIPPED
    p.mkdir(parents=True, exist_ok=True)
    return _Status.CREATED


def _write_if_missing(p: Path, content: str) -> _Status:
    if p.exists():
        return _Status.SKIPPED
    p.write_text(content, encoding="utf-8")
    return _Status.CREATED


def _ensure_gitignore(p: Path) -> _Status:
    if not p.exists():
        body = _GITIGNORE_COMMENT + "\n" + "\n".join(_GITIGNORE_ENTRIES) + "\n"
        p.write_text(body, encoding="utf-8")
        return _Status.CREATED

    existing = p.read_text(encoding="utf-8")
    lines = existing.splitlines()
    changed = False
    if not any(line == _GITIGNORE_COMMENT for line in lines):
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(_GITIGNORE_COMMENT)
        changed = True
    for entry in _GITIGNORE_ENTRIES:
        if not any(line.strip() == entry for line in lines):
            lines.append(entry)
            changed = True
    if not changed:
        return _Status.SKIPPED
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _Status.UPDATED


# ---- CLI entry point ------------------------------------------------------

def cmd_init_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="marginalia init",
        description="Bootstrap a Marginalia project in the current directory.",
    )
    parser.add_argument(
        "directory", nargs="?", default=".",
        help="Project directory (default: current).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    target = Path(args.directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    artifacts = init_project(target)
    print(render_report(target, artifacts))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_init_main())
