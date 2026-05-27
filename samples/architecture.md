# Marginalia 架构概览

> 这份文档是给开发者看的，不是用户文档。用户文档见 `quickstart.md`。

## 五层结构

```
┌─────────────────────────────────────────────┐
│  audit_events    数据变化事件流（仅人类审计）│
├─────────────────────────────────────────────┤
│  sessions/conversations    容器 + 累计指标   │
├─────────────────────────────────────────────┤
│  AI-internal    catalogs / tags / journal    │
│                 entry_relations / entry_tags │
├─────────────────────────────────────────────┤
│  user-visible   folders / file_entries / files│
└─────────────────────────────────────────────┘
┌─────────────────────────────────────────────┐
│  基础设施    tasks / task_outcomes           │
└─────────────────────────────────────────────┘
```

## 三个 LLM 角色

- 🔍 **investigator** — online agent，处理用户问题。读 journal 找过去
  思路，用工具组装上下文，回答带引用
- 🏛️ **librarian** — 离线后台。ingest_file / normalize_tags /
  enrich_tags / restructure_catalogs / lifecycle suggestions
- 📋 **reflector** — 每个 conversation 跑完后，把"这一轮学到了什么"
  写进 journal 供下次 investigator 翻阅

## 任务系统

12 个 task kind 通过 `tasks` 表统一调度，无外部 broker。
`periodic_tick` (10 分钟一次) 是分发器：根据 `PERIODIC_INTERVALS` 决
定哪些 kind 该重新入队。

```
priority   kind                       interval
─────────────────────────────────────────────
30         reflect_turn               (event-driven, after each turn)
50         ingest_file                (event-driven, after upload)
100        recover_stuck_tasks        10 minutes
150        purge_deleted_files        1 day
200        normalize_tags             6 hours
215        enrich_tags                5 days
220        restructure_catalogs       7 days
240        suggest_demotion           7 days
250        suggest_archival           14 days
260        prune_audit_events         1 day
265        prune_task_outcomes        7 days
300        periodic_tick              10 minutes (self-rearm)
```

## 11 个 agent 工具

```
search_journal       翻自己的笔记本（最常用，新对话第一动作）
list_folder          列单个文件夹（同时返回子文件夹和 entries；支持 path='Papers/2024'）
list_catalogs        列 AI 内部分类树
read_catalog         看分类节点详情
resolve_tag          tag 名 → canonical id
materialize_view     view filter_spec 实例化为 entry 列表
search_metadata      复合过滤：text/tags/catalog/lifecycle
read_entries_metadata 批量读 entry 详情 + 自动附 related
read_files           按 section/lines/heading/bytes 读原文 + 内文搜索
query_log            日志 filter（pattern/level/since/until）
query_table          DuckDB SELECT against CSV/Parquet/XLSX/JSON
```

## 3 条 ingest pipeline

- **text** — text/markdown / .txt .md .rst
- **image** — image/* (用 vision profile)
- **pdf** — application/pdf (pypdf 直读 + 内嵌图喂 vision)

## LLM Profile

5 个 profile 通过 `.env` 配置，缺失字段回退到 `LLM_DEFAULT_*`：

```
chat     online agent (用户对话)
reflect  reflect_turn (强模型 + 长上下文)
ingest   离线 ingest + 所有 batch 任务
vision   image_pipeline / pdf 抽图描述
audio    Whisper 类（V1 框架就位，pipeline 待实现）
```

支持 OpenAI 和 Anthropic（含 OpenAI-compatible endpoints）。

## 不可违反的约定

- AI 永不删数据，只软删
- audit_events INSERT-only
- files 内容字段（summary/description/extra/kind）write-once
- agent 不读 audit / sessions / conversations（用 journal 回忆过去）
- 用户不读 AI-internal 表（catalog/tags/journal 等）
- 离线任务不读 audit/conversations 做业务决策（用 task_outcomes）
- task_outcomes 只 INSERT，prune_task_outcomes 是唯一删除路径
