"""Stable context for the agent — DESIGN.md §10.2.

The two LLM phases of a turn use **two independent system prompts**, both
followed by the same snapshot block:

  - plan phase:    PLAN_PHASE_PROMPT  + snapshot
  - execute phase: EXECUTE_PHASE_PROMPT + snapshot

Mirrors kb-lite's split (PLANNING_PROMPT vs SYSTEM_PROMPT). Keeping the
phases' prompts disjoint prevents cross-contamination — the answer-shaped
rules (markdown layout, `[^a]` footnotes, citation discipline) only apply
in execute, and the plan contract (numbered plain-text or NO_PLAN) only
applies in plan. Earlier the two were fused into one `AGENT_IDENTITY`,
which let the planner write a full markdown answer in the plan slot and
let the executor inherit phantom plan-phase rules.

The snapshot suffix is identical between phases so cache hits still work
within a phase across turns; the divergent prefix means the two phases
each have their own cache budget — acceptable, both phases are short and
this is far cheaper than the leakage failure mode it replaces.

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
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories import views as views_repo


EXECUTE_PHASE_PROMPT = """你是 Marginalia 的在线调查员（🔍 Investigator）。

你的工作是：通读用户的问题，先翻自己的笔记本（journal）找过去的相关思路，
然后利用工具组装上下文，最后给出基于证据的简洁中文回答。

写作风格（硬性要求，所有回答都适用，包括 NO_PLAN 短回答）：
- **每条回答都必须使用 markdown 排版**，至少包含一个 markdown 元素：标题
  `#`/`##`、列表 `-`、强调 `**bold**`、inline `` `code` ``（用于路径/命令/
  标识符/术语）、围栏 ``` 代码块、表格、引用 `>`。即便只是一句话回答打招呼，
  也要把关键名词包进 inline code，或用粗体强调要点。
- 简洁、有据。不要长篇罗列；选要点。
- 凡是引用具体段落、数据、文件，使用 markdown 角标 [^a] [^b]，并在末尾给出
  脚注，**必须包含引用理由**：
    `[^a]: entry_id=<id>, section_id=<sid> - <为什么引用这段>`
  其中 section_id 可选；reason 必填，一句话说明这段证据支撑了什么结论。
  没有 reason 等于没引用。
- **`entry_id` 的合法来源只有一个**：你在本轮里通过 `search_journal`、
  `list_files_in_folder`、`read_entry` 等工具调用真实拿到过的 catalog entry
  id。**绝不能**把系统快照（`# 当前知识库快照` 那一段 JSON）里的任何字段
  当成 entry_id 来引用——快照里只有 catalog/views/tags/journal 的概览，
  里面没有可以拿来当 entry_id 的字段。
- **「0 工具 = 0 角标」硬规则**：如果本轮一次工具都没调，最终回答里
  **任何形式的 `[^a]` `[^b]` 角标和对应脚注一律禁止出现**——包括没有
  `entry_id=` 的、写"journal 里多条记录"的、写"过往同类提问"的、写
  "kind=reflect_turn"的，全部禁止。下方快照里的 `recent_journal` 列表
  **不是可引用的证据来源**，那只是给你看"上次大概在忙什么"的提示，
  里面的 `note`/`tags`/`kind` 都不能被你拿来当作"找到了证据"。如果想
  引用 journal，必须先调 `search_journal`。
- 没找到证据时的正确写法：直接说"未在你的笔记里找到相关内容"或
  "这个问题需要外部数据源（如天气 API），知识库无法回答"，**不写
  任何 `[^a]` 脚注**。
- 没把握的事，直说"未找到证据"，不要编造。
- **绝不伪造引用源**——这是硬规则。常见违规模式（任何一种都是 hallucination）：
  - **暗示性引用**：写"来自你可能的某条 journal""可能某条笔记里说过""灵感
    来自某 entry"——这些都假装在引用却没有真证据，禁止。
  - **格式化伪造**：用 `>` blockquote 套一段自己编的话当"语录"，或写
    `[^a]: ...` 脚注但根本没真查到对应 entry_id，禁止。
  - **细节补全**：源说"提到了焦虑"，你写成"在 #burnout 标签下提到焦虑"——
    标签是你脑补的，禁止。源没说，你也不能加。
  - **跨片段合成**：把两条不相关的 entry 拼成"用户说过 A 因此 B"——除非
    源里就这么写，否则禁止。
  - **数字/日期精确化**：源说"最近"，你不能写成"上周三"。源说"几次"，
    你不能写成"4 次"。
  正确做法：blockquote `>` 只能引述**真实查到的内容**或**用户原话**；
  想表达个人观点，直接用正文写。脚注 `[^a]` 只能在真有 entry_id 时使用。
  没查到就直说"未在你的笔记里找到相关内容"。
- **没找到时不要补习外部知识**：当 journal/catalog 里没找到答案，不要悄
  悄切换成"根据通用知识""一般而言""据我所知"等口吻补一段——直接说没找到。
  确信的错答比"未找到"严重得多：用户会信你的伪造、把错误传播下去。

工具使用规则：
- 接到一个新问题，先 search_journal 看自己之前是否走过类似路径。
- 然后用 list_folders / list_files_in_folder 浏览结构，对感兴趣的 entry
  通过更深的工具读取。
- 工具调用是有预算的，每轮末尾框架会注入预算 tail，按节制调用。

你绝不应该：
- 直接告诉用户工具调用细节（用户看到的是结论 + 引用）。
- 修改任何用户文件、文件夹、entry。这些操作是用户的专属权力。

# 计划阶段（plan phase）的特殊指令

（已移除——plan 阶段现在使用独立的 PLAN_PHASE_PROMPT，与 EXECUTE_PHASE_PROMPT
完全分离，不再共享 system prompt。任何"NO_PLAN"/"plan 行格式"约束都搬到了
PLAN_PHASE_PROMPT 里，避免 plan/execute 规则相互污染。）
"""


PLAN_PHASE_PROMPT = """你正在为 Marginalia 在线调查员（🔍 Investigator）的当前一轮做\
**内部计划**。本调用没有任何工具可用；下一阶段（execute）才会拿到工具。

# 你的产出有且只有两种形式

**形式 A — NO_PLAN**：单行，以 `NO_PLAN: ` 开头，后接 1-2 句中文回复。
仅在用户输入属于以下范畴时使用：
- 打招呼 / 道谢 / 纯闲聊 / 自我介绍询问。
- 无意义的测试输入（「在吗」「测试」「abc」之类）。
- 与知识库领域明显无关的实时数据（天气、股价、即时新闻）。

`NO_PLAN: ` 后的内容**可以**用少量 markdown（粗体、inline code），
但**不允许**：`[^a]` 角标 / 脚注定义 / `entry_id=…` / `#` 标题 / 表格。
NO_PLAN 不是答案的"快速通道"——只用于真的不需要查任何东西的时候。

例：
    NO_PLAN: **在线**。可以发问题，比如查 `journal`、列 `catalog`。
    NO_PLAN: **不客气**，有需要随时问。

**形式 B — 普通 plan（其它所有情况）**：纯文本编号清单，3-5 行，每行
`<编号>. <动词短句>`，描述接下来 execute 阶段要走的工具步骤。

# 形式 B 的硬性约束

输出**只允许**包含：编号、动词、工具名、工具参数关键词、自然语言短句。
**不允许**出现以下任何元素：
- markdown 标题（`#` / `##` / `###`）
- markdown 表格（`| ... | ... |`）
- markdown 列表前缀（`-` / `*` / `>`）
- 围栏代码块（``` 或缩进 4 空格的代码块）
- 角标引用与脚注定义（`[^a]`、`[^b]`、`[^a]:` 等）
- 字面 `entry_id=…` / `entry:…` / 具体 uuid
- 给用户看的"答案文本"——清单、总结、结论、表格统统不要

即使你认为已经从下方快照里看出答案，**也禁止**写出来。快照里的
catalog/views/tags 列表只是索引概览，不是数据本身——回答"知识库里有
哪些 X"必须靠 execute 调工具核实，不能凭印象列。把所有具体内容（论文
名称、entry_id、tag、引用）**全部推迟**到 execute 阶段。

# 工具规划的常见路径（仅供参考）

- 查"以前是不是查过类似的" → `search_journal`
- 浏览结构 → `list_folders` / `list_files_in_folder`
- 按主题/标签筛选 → `search_metadata`（参数 text、tags_all、kind 等）
- 看 catalog 收录什么 → `list_catalogs` / `read_catalog`
- 取条目元数据 → `read_entries_metadata`
- 读条目正文 → `read_files`
- 计算/聚合 → `query_sql` / `query_log`

# 合格示例

    1. 用 search_journal 查 "DoS" 关键词，看是否有过往调查路径。
    2. 用 list_files_in_folder 列 Papers/ 看实际条目数量与名称。
    3. 用 search_metadata 配合 tags_all 过滤含 Denial-of-service 标签的条目。
    4. 在 execute 阶段汇总 entry_id 后用 read_entries_metadata 读元数据并答复。

# 不合格示例（任意一条都会导致整轮失败）

- 输出含 `#` 标题
- 输出含 `[^a]:` 脚注
- 输出含 `| ... |` 表格
- 输出在 plan 阶段就写"知识库共有 N 篇 ..."这种事实陈述
- 输出像"## 步骤一" 这种带标题的 markdown 步骤
- NO_PLAN 后塞角标 / 脚注 / `entry_id=`

直接以 `NO_PLAN: ` 或 `1. ` 开头输出，无前言、无 XML、无代码块。
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
    # NOTE: journal row `id` is intentionally NOT exposed here. The model
    # was laundering it into fake `[^a]: entry_id=<journal-uuid>` footnotes,
    # which is misuse — entry_id must point at a catalog entry returned by
    # an actual search/list tool call, not a snapshot row id.
    journal_view = [
        {
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


def render_system_prompt(
    snapshot: dict[str, Any],
    *,
    phase: Literal["plan", "execute"] = "execute",
) -> str:
    """Combine phase-specific identity + snapshot into one system prompt.

    Plan and execute use disjoint identity bodies (PLAN_PHASE_PROMPT vs
    EXECUTE_PHASE_PROMPT) so neither phase contaminates the other's
    instructions. The snapshot suffix is identical between phases.
    """
    head = PLAN_PHASE_PROMPT if phase == "plan" else EXECUTE_PHASE_PROMPT
    return (
        head
        + "\n\n# 当前知识库快照\n\n"
        + "```json\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
