"""Parse a `.git/` directory layout from the extracted container tempdir.

Pure stdlib — we don't import GitPython because:
  1. Marginalia would have to ship/install it
  2. We need only a thin slice of git's metadata: branch, recent commits,
     authors — all readable from the on-disk format
  3. .git/logs/HEAD is a plain text reflog the agent can render simply

Read paths (in order):
  .git/HEAD                       → ref to current branch
  .git/refs/heads/<branch>        → tip commit hash
  .git/logs/HEAD                  → reflog (most recent commits)
  .git/config                     → optional remote URL extraction

If a file is missing or unparseable, we silently skip and emit what we
have — no exception bubbles up to the caller. The caller treats
git_metadata as best-effort enrichment.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_HEAD_REF_RE = re.compile(r"^ref:\s*(\S+)\s*$")
# A reflog line has 2 hashes, an author block, a tab, then the message:
#   <old_hash> <new_hash> <name> <email> <unix_ts> <tz>\t<message>
_REFLOG_LINE_RE = re.compile(
    r"^(?P<old>[0-9a-f]{40})\s+(?P<new>[0-9a-f]{40})\s+"
    r"(?P<name>.+?)\s+<(?P<email>[^>]+)>\s+"
    r"(?P<ts>\d+)\s+(?P<tz>[+-]\d{4})\t(?P<msg>.*)$"
)
_REMOTE_URL_RE = re.compile(r'\[remote\s+"([^"]+)"\][^[]*?url\s*=\s*(\S+)',
                            re.DOTALL)


@dataclass(slots=True)
class GitCommitSummary:
    hash: str
    author_name: str
    author_email: str
    timestamp: int
    message_first_line: str


@dataclass(slots=True)
class GitMetadata:
    branch: str | None = None
    head_hash: str | None = None
    recent_commits: list[GitCommitSummary] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    remotes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "head_hash": self.head_hash,
            "remotes": dict(self.remotes),
            "authors": list(self.authors),
            "recent_commits": [
                {
                    "hash": c.hash,
                    "author_name": c.author_name,
                    "author_email": c.author_email,
                    "timestamp": c.timestamp,
                    "message": c.message_first_line,
                }
                for c in self.recent_commits
            ],
        }


def parse(extract_root: Path, *, max_commits: int = 20) -> GitMetadata | None:
    """Read `.git/` under `extract_root` and return a GitMetadata.

    Returns None if `.git/` doesn't exist (caller already classified the
    container as git_repo on the basis of `.git/HEAD`, but parse may still
    be called defensively).
    """
    git_dir = extract_root / ".git"
    if not git_dir.exists():
        return None

    meta = GitMetadata()
    _parse_head(git_dir, meta)
    _parse_branch_tip(git_dir, meta)
    _parse_reflog(git_dir, meta, max_commits=max_commits)
    _parse_config(git_dir, meta)
    return meta


def _parse_head(git_dir: Path, meta: GitMetadata) -> None:
    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        return
    try:
        content = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return
    m = _HEAD_REF_RE.match(content)
    if m:
        ref = m.group(1)
        # ref is like "refs/heads/main"
        if ref.startswith("refs/heads/"):
            meta.branch = ref[len("refs/heads/"):]
    else:
        # Detached head: HEAD itself is a hash
        if re.match(r"^[0-9a-f]{40}$", content):
            meta.head_hash = content
            meta.branch = None  # detached


def _parse_branch_tip(git_dir: Path, meta: GitMetadata) -> None:
    if not meta.branch:
        return
    ref_path = git_dir / "refs" / "heads" / meta.branch
    if not ref_path.is_file():
        # Possibly packed-refs — best-effort scan
        packed = git_dir / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == f"refs/heads/{meta.branch}":
                        meta.head_hash = parts[0]
                        return
            except Exception:
                return
        return
    try:
        meta.head_hash = ref_path.read_text(
            encoding="utf-8", errors="replace"
        ).strip() or None
    except Exception:
        return


def _parse_reflog(
    git_dir: Path, meta: GitMetadata, *, max_commits: int,
) -> None:
    """Parse .git/logs/HEAD into a list of recent commits (newest first).

    Reflog lines describe HEAD movements; each line's `new_hash` was at
    one point HEAD. We dedupe by hash so a reset/fast-forward doesn't
    show the same commit twice."""
    reflog = git_dir / "logs" / "HEAD"
    if not reflog.is_file():
        return
    try:
        text = reflog.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    seen_hashes: set[str] = set()
    seen_authors: set[str] = set()
    out: list[GitCommitSummary] = []
    for line in reversed(text.splitlines()):
        m = _REFLOG_LINE_RE.match(line)
        if not m:
            continue
        new_hash = m.group("new")
        if new_hash in seen_hashes:
            continue
        seen_hashes.add(new_hash)
        author_name = m.group("name")
        author_email = m.group("email")
        # Use "name <email>" as author identity to dedup
        ident = f"{author_name} <{author_email}>"
        if ident not in seen_authors:
            seen_authors.add(ident)
        try:
            ts = int(m.group("ts"))
        except ValueError:
            ts = 0
        out.append(GitCommitSummary(
            hash=new_hash,
            author_name=author_name,
            author_email=author_email,
            timestamp=ts,
            message_first_line=m.group("msg"),
        ))
        if len(out) >= max_commits:
            break
    meta.recent_commits = out
    meta.authors = sorted(seen_authors)


def _parse_config(git_dir: Path, meta: GitMetadata) -> None:
    cfg_path = git_dir / "config"
    if not cfg_path.is_file():
        return
    try:
        content = cfg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    for m in _REMOTE_URL_RE.finditer(content):
        remote_name, url = m.group(1), m.group(2)
        meta.remotes[remote_name] = url
