"""Stable context for the agent — design.md §10.2.

Each turn's LLM call gets the same identity-shaped system prompt prefix
followed by a snapshot of the catalog tree + view list + tag vocabulary +
recent journal headlines. Keeping this prefix stable across turns is the
prompt-cache optimisation — adapters mark / auto-detect cache breakpoints.

V1 implementation: rebuilt on every turn (cheap; the underlying queries
take a handful of milliseconds). A future optimisation can periodise this
through normalize_tags' completion hook.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import (
    Catalog,
    FileEntry,
    Journal,
    Tag,
    View,
)


AGENT_IDENTITY = """你是 Marginalia 的在线调查员（🔍 Investigator）。

你的工作是：通读用户的问题，先翻自己的笔记本（journal）找过去的相关思路，
然后利用工具组装上下文，最后给出基于证据的简洁中文回答。

写作风格：
- 简洁、有据。不要长篇罗列；选要点。
- 凡是引用具体段落、数据、文件，使用 markdown 角标 [^a] [^b]，并在末尾给出
  脚注，**必须包含引用理由**：
    `[^a]: entry_id=<id>, section_id=<sid> - <为什么引用这段>`
  其中 section_id 可选；reason 必填，一句话说明这段证据支撑了什么结论。
  没有 reason 等于没引用。
- 没把握的事，直说"未找到证据"，不要编造。

工具使用规则：
- 接到一个新问题，先 search_journal 看自己之前是否走过类似路径。
- 然后用 list_folders / list_files_in_folder 浏览结构，对感兴趣的 entry
  通过更深的工具读取。
- 工具调用是有预算的，每轮末尾框架会注入预算 tail，按节制调用。

你绝不应该：
- 直接告诉用户工具调用细节（用户看到的是结论 + 引用）。
- 修改任何用户文件、文件夹、entry。这些操作是用户的专属权力。
"""


# Caps to keep the snapshot bounded.
TOP_LEVEL_CATALOGS_LIMIT = 50
VIEWS_LIMIT = 30
TAG_TOP_PER_FACET = 30
JOURNAL_RECENT_LIMIT = 20
JOURNAL_RECENT_DAYS = 30


async def build_stable_snapshot(db: AsyncSession) -> dict[str, Any]:
    """Build the structured snapshot the agent's stable system prompt
    embeds. Keep small + deterministic so prompt cache works."""
    # ---- 1. catalog top level (with doc_count) -----------------------------
    top_cats = (
        await db.execute(
            select(Catalog)
            .where(Catalog.parent_id.is_(None), Catalog.deleted_at.is_(None))
            .order_by(Catalog.name)
            .limit(TOP_LEVEL_CATALOGS_LIMIT)
        )
    ).scalars().all()

    cat_counts_rows = (
        await db.execute(
            select(FileEntry.catalog_id, func.count())
            .where(
                FileEntry.catalog_id.isnot(None),
                FileEntry.deleted_at.is_(None),
            )
            .group_by(FileEntry.catalog_id)
        )
    ).all()
    cat_counts = {cid: c for cid, c in cat_counts_rows}

    catalog_view = [
        {
            "id": c.id,
            "name": c.name,
            "summary": c.summary,
            "doc_count": cat_counts.get(c.id, 0),
        }
        for c in top_cats
    ]

    # ---- 2. views ---------------------------------------------------------
    views = (
        await db.execute(
            select(View).order_by(View.name).limit(VIEWS_LIMIT)
        )
    ).scalars().all()
    view_view = [
        {"id": v.id, "name": v.name, "summary": v.summary}
        for v in views
    ]

    # ---- 3. tag vocabulary by facet --------------------------------------
    tags_by_facet: dict[str, list[dict[str, Any]]] = {}
    for facet in ("topic", "form", "time", "source", "language", "extra"):
        rows = (
            await db.execute(
                select(Tag.id, Tag.name, Tag.doc_count)
                .where(Tag.facet == facet, Tag.alias_of.is_(None))
                .order_by(Tag.doc_count.desc(), Tag.name)
                .limit(TAG_TOP_PER_FACET)
            )
        ).all()
        if rows:
            tags_by_facet[facet] = [
                {"id": tid, "name": n, "doc_count": dc or 0}
                for tid, n, dc in rows
            ]

    # ---- 4. recent journal headlines --------------------------------------
    cutoff = datetime.now(timezone.utc) - timedelta(days=JOURNAL_RECENT_DAYS)
    journals = (
        await db.execute(
            select(Journal)
            .where(Journal.created_at >= cutoff)
            .order_by(Journal.created_at.desc())
            .limit(JOURNAL_RECENT_LIMIT)
        )
    ).scalars().all()
    journal_view = [
        {
            "id": j.id,
            "note": (j.note or "")[:200],
            "entry_count": len(j.entry_ids or []),
            "tags": list(j.tags or []),
        }
        for j in journals
    ]

    return {
        "catalog_top_level": catalog_view,
        "views": view_view,
        "tags_by_facet": tags_by_facet,
        "recent_journal": journal_view,
    }


def render_system_prompt(snapshot: dict[str, Any]) -> str:
    """Combine identity + snapshot into one stable system prompt string.

    The snapshot is JSON-serialised once, so adapters can place a cache
    breakpoint right after this entire block.
    """
    return (
        AGENT_IDENTITY
        + "\n\n# 当前知识库快照\n\n"
        + "```json\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
