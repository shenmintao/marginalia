"""Stable context for the agent — DESIGN.md §10.2.

Each turn's LLM call gets the same identity-shaped system prompt prefix
followed by a snapshot of the catalog tree + view list + tag vocabulary +
recent journal. Keeping this prefix stable across turns is the
prompt-cache optimisation — adapters mark / auto-detect cache breakpoints.

Journal recall is logically frozen for the duration of one session by
filtering `created_at < session.started_at`. This both:
  * excludes the session's own reflect_turn rows (which would otherwise
    fold the agent's just-written notes back into its next plan-phase
    prompt — a noisy self-loop, design [[journal-tiers]]), and
  * keeps the journal slice stable across turns, so the prefix doesn't
    drift mid-session.

V1: rebuilt on every turn (cheap; the underlying queries take a handful
of milliseconds). The catalog/views/tags slices are NOT logically frozen
— per DESIGN.md §4.2 the offline writers don't run during live sessions,
so in practice they don't drift.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories import views as views_repo


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

# 计划阶段（plan phase）的特殊指令

你的本轮第一次调用是 plan 阶段，没有工具可用。请用一两句话规划接下来要查
什么、用哪些工具。**但是**：如果用户的问题不需要任何工具就能回答（打招呼、
道谢、纯闲聊、能直接从上述快照给出答案的概念性问题），不要假装规划，请直
接以下面这一行开头并给出最终答案：

    NO_PLAN: <你的最终回答>

例如用户说"谢谢"，回 `NO_PLAN: 不客气。`。运行时看到 `NO_PLAN:` 会跳过
execute 阶段直接把这段当回答返回。普通问题照常规划即可，不要滥用 NO_PLAN。
"""


# Caps to keep the snapshot bounded.
TOP_LEVEL_CATALOGS_LIMIT = 50
VIEWS_LIMIT = 30
TAG_TOP_PER_FACET = 30
RECENT_JOURNAL_LIMIT = 10


async def build_stable_snapshot(
    db: AsyncSession, *, session_started_at: datetime,
) -> dict[str, Any]:
    """Build the structured snapshot the agent's stable system prompt
    embeds. Keep small + deterministic so prompt cache works.

    `session_started_at` freezes the journal slice to rows written before
    the current session began — see module docstring for rationale.
    """
    top_cats = await catalogs_repo.list_live_top_level(
        db, limit=TOP_LEVEL_CATALOGS_LIMIT,
    )
    cat_counts = await catalogs_repo.direct_entry_counts(db)
    catalog_view = [
        {
            "id": c.id,
            "name": c.name,
            "summary": c.summary,
            "doc_count": cat_counts.get(c.id, 0),
        }
        for c in top_cats
    ]

    views = await views_repo.list_for_snapshot(db, limit=VIEWS_LIMIT)
    view_view = [
        {"id": v.id, "name": v.name, "summary": v.summary}
        for v in views
    ]

    tags_by_facet: dict[str, list[dict[str, Any]]] = {}
    for facet in ("topic", "form", "time", "source", "language", "extra"):
        rows = await tags_repo.top_per_facet(
            db, facet, limit=TAG_TOP_PER_FACET,
        )
        if rows:
            tags_by_facet[facet] = [
                {"id": tid, "name": n, "doc_count": dc or 0}
                for tid, n, dc in rows
            ]

    # Logically frozen at session start — see module docstring.
    rows = await journal_repo.recent_journal_for_snapshot(
        db, before=session_started_at, limit=RECENT_JOURNAL_LIMIT,
    )
    journal_view = [
        {
            "id": j.id,
            "kind": j.source_kind,
            "note": j.note or "",
            "entry_count": len(j.entry_ids or []),
            "tags": list(j.tags or []),
        }
        for j in rows
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
