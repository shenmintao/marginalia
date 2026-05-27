# Marginalia 设计

> 单一真相源。描述系统**是什么**、由**什么数据**组成、如何**运作**。
>
> 不解释立项动机、不列使用场景、不展望未来——那些 README 的事。

---

## 0. 30 秒理解

文件进来 → AI 读一遍写好 metadata → 用户提问 → AI 翻自己的笔记本 + 按 metadata 找文件 + 读原文 → 给带引用的答案。

**不切块、不嵌入、不做向量。** AI 通过结构化访问点(分类树 / tag / view)缩小范围,通过 metadata 判断相关性,通过原文确认事实。

整个系统围绕三个身份构建:**🏛️ 图书馆员**(离线整理藏书)/ **🔍 调查员**(在线查阅资料)/ **👤 用户**(投递、取阅、销毁)。这不是修辞——它是"这件事该谁做"的判别工具,直接决定 schema 的写权限分布(§7.1)。

---

## 1. 数据模型

> 数据结构正确,代码自然就对。先看表,再看流程。

**14 张业务表 + 1 张 alembic_version。** 所有主键 uuid7(时间有序);所有时间戳 timestamptz(UTC)。

按职责分四层,互不重叠:

```
┌─────────────────────────────────────────────┐
│ 用户层    folders / file_entries / files    │ 真实文件 + 位置引用
├─────────────────────────────────────────────┤
│ AI 内部层  catalogs / views / tags /          │ 图书馆员的工作知识
│           tag_aliases / entry_tags /         │ 用户完全看不到
│           entry_relations / journal          │
├─────────────────────────────────────────────┤
│ 审计层    audit_events / sessions /          │ 给人类看的事实流
│           conversations                      │
├─────────────────────────────────────────────┤
│ 基础设施   tasks / task_outcomes              │ 调度面
└─────────────────────────────────────────────┘
```

### 1.1 用户层(3 张)

#### `folders` — 用户的虚拟文件夹

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| parent_id | uuid? (自引用,null = 根) |
| name | str(255) |
| deleted_at | timestamptz? |
| created_at, updated_at | timestamptz |

约束:`UNIQUE(parent_id, name)`(同父不重名)。

写者:用户。读者:用户 + agent(读 name 作先验信号)。

#### `file_entries` — 一个文件在一个位置的引用

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| folder_id | uuid → folders |
| file_id | uuid → files |
| display_name | str(255) |
| lifecycle | enum(active / demoted / archived / manual_active / manual_archived) |
| catalog_id | uuid? → catalogs |
| extra | text? — per-entry AI 累积理解(mutable) |
| deleted_at, purge_after | timestamptz? |
| created_at, updated_at | timestamptz |

**关键设计**:同一份 sha256 在不同 folder 下产生不同 file_entry 行,各自独立的 AI 分类与解读。AI 字段是 **per-position** 的,不是 per-content 的。

dedup 时从源 entry 拷贝 catalog_id / extra / tags 作为种子,之后独立演化。**不**拷贝 entry_relations(关系是观察记录,不是属性)。

写者:用户(folder_id / display_name / 用户层 lifecycle 切换 / 软删)+ 离线任务(catalog_id / lifecycle 自动迁移)+ reflect(extra)。

#### `files` — 物理文件,内容寻址

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| storage_key | str(255) UNIQUE |
| sha256 | str(64) UNIQUE |
| size_bytes | bigint |
| mime_type, original_ext | str? |
| kind | enum(text / table / log / image / audio / video / code / container) |
| summary | text? — 内容总结 |
| description | json? — 结构化导航(章节 / 列 / 帧 / 代码符号) |
| extra | text? — 内容洞察 |
| ingest_status | enum(pending / processing / done / failed) |
| ingested_at | timestamptz? |
| deleted_at | timestamptz? |
| created_at, updated_at | timestamptz |

**Write-once 契约**:`summary / description / extra / kind` 在 `ingested_at IS NULL` 时由 `ingest_file` 一次性写入,设 `ingested_at` 后永久锁定。

集中 enforce 路径:`files_repository.update_content` 检查 `ingested_at`,非 NULL 抛 `WriteOnceViolation`。**这是不变量,不是约定**——不能依赖程序员自觉。

为什么 write-once:这些字段描述**不变的字节流**,sha256 相同则描述应当一致。位置感知的解读由 `file_entries.extra` 承担。

### 1.2 审计层(3 张)

#### `audit_events` — 数据变化事件流

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| occurred_at | timestamptz |
| kind | str(64) |
| session_id, conversation_id, task_id | uuid? |
| payload | json |

索引:`(occurred_at)` / `(session_id, occurred_at)` / `(conversation_id, occurred_at)` / `(task_id, occurred_at)` / `(kind, occurred_at)`。

INSERT-only。`prune_audit_events` 删 90 天前的行(可配置)。

**只给人类审计看**。Agent 不读、离线任务不读——离线任务的调度判定走 `task_outcomes`(§1.4)。这条边界一旦破坏,audit 会被调度查询污染,人类反而读不懂。

kind 仅记录数据变化:`file_created / file_updated / entry_created / entry_updated / lifecycle_changed / journal_entry_written / tag_created / tag_merged / entry_relation_upserted / catalog_moved / view_updated / task_started / task_finished / task_failed / ingest_status_changed` 等。

**不**记录 in-memory 的 tool_call / llm_call——那些保存在 conversations 表的 JSON 字段里。

#### `sessions` — 一次使用窗口

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| started_at, ended_at | timestamptz |
| end_reason | enum(cleared / normal / unclean) |
| initiating_user_message | text |
| turn_count | int |
| total_input_tokens / output_tokens / cache_read / tool_calls / llm_calls / cost_estimate / duration_ms | 累计指标 |

`unclean` 由 `recover_stuck_tasks` 在最后 audit_event > 24h 时标记。无空闲超时。

#### `conversations` — session 内一轮活动

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| session_id | uuid → sessions |
| turn_index | int |
| started_at, ended_at | timestamptz |
| user_message | text |
| agent_response | text? |
| tool_calls | json |
| llm_calls | json |
| total_* | 累计指标 |

**关键约束**:
- 没有 `tags` / `extra` / `plan` 字段——那些都不属于审计层
- agent **不读**这张表——"过去经验"通过 `journal` 取
- plan 文本不持久化,作为 `llm_calls` 中 phase='plan' 的记录保留

### 1.3 AI 内部层(7 张)

用户**完全看不到**这层。所有访问通过 agent 工具(§4)。

#### `catalogs` — AI 分类树

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| parent_id | uuid? → catalogs |
| name | str(255) |
| summary | text? |
| description | json? |
| extra | text? — mutable 累积理解 |
| tags | json? |
| deleted_at | timestamptz? |
| created_at, updated_at | timestamptz |

完全由 AI 涌现。`restructure_catalogs` 离线调整。`extra` 由 reflect 在对话触及该节点时刷新。

#### `views` — 跨 catalog 的话题聚合

同 catalogs 字段,加:

| 字段 | 类型 |
|---|---|
| filter_spec | json — catalog_subtree / tags_all / tags_any / tags_none / facets / date_range |

`materialize_view` 工具实时跑过滤,**不**缓存命中结果。V1 无用户创建入口。

#### `tags` — 受控词表

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| name | str(255) |
| facet | enum(topic / form / time / source / language / extra) |
| alias_of | uuid? → tags(规范 tag,必须 alias_of IS NULL) |
| doc_count | int |
| last_used_at | timestamptz? |

约束:`UNIQUE(name, facet)`。`alias_of` **单层**指向规范 tag,不能链式——normalize_tags 保证。

facet 是预定义机制(不违反涌现原则——facet 是机制,不是内容)。`extra` facet 是兜底维度,新 facet 通过累积观察后由开发者手动加入代码。

#### `tag_aliases` — authority file

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| from_name | str(255) |
| to_tag_id | uuid → tags |
| note | text? |
| created_at | timestamptz |

**永不删除**——历史合并是事实。`resolve_tag` 工具优先查 `tags.name`,未命中再查 `from_name`。

#### `entry_tags` — entry ↔ tag

| 字段 | 类型 |
|---|---|
| entry_id, tag_id | (复合 PK) |
| source | enum(ingest / reflect / enrich_tags / dedup_seed) |
| created_at | timestamptz |

`source` 是 provenance——agent 在矛盾元数据间能加权判断(`source='dedup_seed'` 弱于 `source='reflect'`)。

#### `entry_relations` — entry 对的关联

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| entry_a_id, entry_b_id | uuid (a < b) |
| note | text |
| source_kind | enum(reflect) |
| last_observed_at | timestamptz |
| observation_count | int |
| created_at | timestamptz |

约束:`UNIQUE(entry_a_id, entry_b_id)`。Service 层强制 a < b。

**无 kind 受控词表**——note 自由文本,agent 自己读判断关系性质。一对 entry 一行,observation_count 累加。

访问模式:`read_entries_metadata` 后端 JOIN,默认附 top-10 by observation_count。无独立 traverse 工具——多跳遍历由 agent 自行组合。

#### `journal` — 调查员的笔记本

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| conversation_id | uuid → conversations |
| note | text |
| entry_ids | json |
| tags | json |
| source_kind | enum(reflect_turn) |
| created_at | timestamptz |

Append-only。永远 per-conversation,**不是** per-file / per-pair / per-tag。

agent 起步翻笔记的方式:`search_journal(text?, entry_id?, tags?, since?)`。

### 1.4 基础设施(2 张)

#### `tasks` — 异步任务队列

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| kind | str(64) |
| payload | json |
| dedup_key | str? |
| status | enum(pending / running / done / failed / dead) |
| priority | int (越小越优先) |
| attempts, max_attempts | int |
| last_error | text? |
| scheduled_at, lease_expires_at | timestamptz |
| locked_by | str(64)? |
| created_at, started_at, finished_at | timestamptz |

索引:`(status, scheduled_at, priority)` / `(dedup_key)` / `(kind, status)`。

`dedup_key UNIQUE WHERE status IN ('pending','running')` 保证同一逻辑任务不重复入队。

claim 机制:Postgres `FOR UPDATE SKIP LOCKED`;SQLite 单 worker 假设。

零外部 broker(Redis / Celery / RabbitMQ 一律不要)。

#### `task_outcomes` — 任务对对象的处理记录

| 字段 | 类型 |
|---|---|
| id | uuid7 PK |
| task_kind | str(64) |
| object_kind | enum(file_entry / conversation / entry_pair / global) |
| object_id | str(255) |
| task_run_id | str? → tasks.id |
| outcome | enum(applied / noop / rejected / deferred) |
| detail | json? |
| completed_at | timestamptz |

索引:`(task_kind, object_kind, object_id)` / `(task_kind, completed_at)` / `(completed_at)`。

INSERT-only。`prune_task_outcomes` 删 30 天前的行。

**为什么和 audit_events 分开**:
- audit_events 是事件流,人类读,按时间窗口扫
- task_outcomes 是判定表,调度器读,按 (task, object_id) 查

读者不同 → 索引不同 → 增长来源不同。合并的话每个挖掘任务都要往 audit 加新 kind,audit 立刻被污染成 schedules.log。

同 (task_kind, object_id) 可以多行(每次处理一行),查询时取 `MAX(completed_at)`。

---

## 2. extra 与 journal 的协作语义

很多人会被这两个东西的关系绊倒。一句话讲清:

- **`journal`** = 历史快照,append-only。"那次对话学到了什么。"
- **`<entity>.extra`** = entity **当前**累积理解,mutable。综合若干 journal 后由 reflect 决定是否覆盖。

reflect_turn **可以同时**写 journal(历史)和 UPDATE entity.extra(当前)。LLM 自己判断哪些洞察值得"晋升"到 extra,哪些只值得记在 journal。

**唯一例外**:`files.extra` 是 write-once(描述内容本身,不随对话演化)。`file_entries.extra` / `catalogs.extra` / `views.extra` 都是 mutable。

为什么 file_entries.extra 必须 mutable:它是位置感知的累积理解。如果只有 ingest 时写一次,这个字段语义就退化成"ingest 当时怎么看",违反"当前理解"承诺。

---

## 3. 任务系统

10 个业务 kind + 1 个 dispatcher。所有异步工作走统一 `tasks` 队列。

### 3.1 优先级 + 周期

数字越小越优先。层级反映价值排序:用户在场最优先 → 系统不能坏 → 用户意愿要兑现 → 质量基础先打牢 → 结构演化慢慢来 → 生命周期判断最后做。

| pri | kind | 周期 | 身份 |
|---|---|---|---|
| 30 | `reflect_turn` | 每轮对话结束 | 🔍→🏛️ |
| 50 | `ingest_file` | 新 sha256 上传 | 🏛️ |
| 100 | `recover_stuck_tasks` | 10 min | 🏛️ 自愈 |
| 150 | `purge_deleted_files` | 1 d | 🏛️ |
| 200 | `normalize_tags` | 6 h | 🏛️ |
| 215 | `enrich_tags` | 5 d | 🏛️ |
| 220 | `restructure_catalogs` | 7 d | 🏛️ |
| 240 | `suggest_demotion` | 7 d | 🏛️ |
| 250 | `suggest_archival` | 14 d | 🏛️ |
| 260 | `prune_audit_events` | 1 d | 🏛️ |
| 265 | `prune_task_outcomes` | 7 d | 基础设施 |
| 300 | `periodic_tick` | 10 min | dispatcher |

### 3.2 因果依赖

```
recover_stuck_tasks ─→ 所有其他离线任务
normalize_tags ─→ enrich_tags
              ─→ restructure_catalogs
normalize_tags + restructure_catalogs ─→ suggest_demotion
                                       ─→ suggest_archival
purge_deleted_files 自包含
```

依赖通过 `dedup_key` 串行化保证——每个 kind 同时只有一行 pending/running,下一轮间隔到了才入队。priority + interval + dedup_key 自然形成正确顺序,**无需显式 DAG**。

为什么生命周期判断最后做:AI 不该在自己还没看清楚一个 entry 的时候就把它打入冷宫。等 normalize / enrich / restructure 跑过,再判断"什么不重要"。

### 3.3 关键任务

#### `ingest_file`(入册新书)
- 输入:`{file_id}`
- 路由:按 `mime_type` + `original_ext` 查 pipeline 注册表(§5)
- 一次 LLM 调用产出:`files.{summary, description, kind, extra}` + entry 的 `{catalog_id, extra, entry_tags}`
- 成功后设 `ingested_at` 锁定 file 内容字段
- 失败:`ingest_status='failed'`,等 `recover_stuck_tasks` 给一次重试

#### `reflect_turn`(写笔记本 + 刷新 extra)
- 触发:每轮 conversation 终止(agent 最后一轮无 tool_call)
- 输入:conversation 完整事实 + 涉及 entry 当前 metadata
- 强模型 + 长上下文一次调用,产出(每项独立判断要不要写):
  - 0-N 条 journal
  - 0-N 条 entry_relations(新对 INSERT 或已有对 INCREMENT)
  - 0-N 条 entry_tags 增补(`source='reflect'`)
  - 0-N 条 file_entries.extra 覆盖更新
  - 0-N 条 catalogs.extra / views.extra 覆盖更新(仅当对话触及)
  - 可能给后续离线任务留 hint(写 journal 标 `tags=['hint:restructure_catalogs']`)
- **严格不动** files.summary / description / extra(write-once)
- LLM 自己判断"无可记录" → 直接 done,不强制写 journal

#### `recover_stuck_tasks`(自愈)
四类职责:
1. `tasks WHERE status='running' AND lease_expires_at < now` → 重置 pending
2. `tasks WHERE status='dead' AND created_at > 7d ago` → 给一次重试机会
3. `files WHERE ingest_status IN ('processing','failed') AND ingest_file 任务不存在/dead` → 重新入队
4. `sessions WHERE ended_at IS NULL AND last_audit_event_age > 24h` → 标 `unclean`

#### `normalize_tags`(整理卡片目录)
- LLM 任务,**不是**规则匹配
- 取 facet 内 tag(含 doc_count)分批喂 LLM 判断同义合并
- 应用合并:`tags.alias_of` 指向规范 / 写 tag_aliases 历史 / entry_tags 重写指向规范
- 处理 `extra` facet 跨 facet 迁移
- 提议引入新 facet(极罕见)→ 写一条 journal 提醒开发者

#### `enrich_tags`(用新词表回填老书)
- LLM 任务
- 输入:lifecycle ∈ ('active', 'manual_active') 的 entry,最近 N 天没被 enrich
- 拿当前规范 tag 词表(按 facet 分组)+ entry description + 现有 tags → LLM 严格从词表选新增
- 产出:INSERT entry_tags(`source='enrich_tags'`)。**不 DELETE**

#### `restructure_catalogs`(重整书架)
- 输入:当前 catalog 树 + 最近 hint:restructure_catalogs 的 journal + 高 doc_count 的 active entry
- LLM 提议节点合并/拆分/重命名 + entry 移动
- 产出:UPDATE catalogs.* / file_entries.catalog_id
- 软删旧节点(`deleted_at`),不硬删

#### `suggest_demotion` / `suggest_archival`
- 基于活跃度信号(最近 N 天未被 conversation 引用 / 对应 file 未被 read_files / entry_tags 稀疏)
- demotion: active → demoted;archival: demoted → archived
- 跳过 manual_* 状态(用户已锁定)
- **永远 NO 自动恢复 active**——升级只能由"使用"触发

#### `purge_deleted_files`(兑现用户销毁意愿)
- 扫 `file_entries WHERE deleted_at IS NOT NULL AND purge_after < now`
- 单事务:DELETE entry → 检查同 file_id 还有无活跃 entry → 没有则 DELETE file 行 + 删 storage 对象

#### `prune_audit_events` / `prune_task_outcomes`
- 各自按 retention 配置删旧行,分批避免长事务

---

## 4. Agent 工具集

13 个工具。全部由 🔍 调查员使用。每个工具对应一个外部动作——访问 DB / 存储 / 临时计算引擎。**Agent 自己组合判断,不替它做决策**。

> 工具是给 AI 的,不是给人类的。简单粗糙的原语优于智能复杂的工具。`search_metadata` 用 ILIKE 而不是相关性排序,让 agent 自己组合判断。

### 4.1 工具清单

**起步层**
- `search_journal(text?, entry_id?, tags?, since?, limit, order)` — 翻笔记本

**结构层**
- `list_catalogs(parent_id?)` — 下钻 catalog 树
- `read_catalog(id)` — catalog 完整 metadata
- `list_folder(parent_id?, path?)` — 用户文件夹树(作先验提示)。path='Papers/2024' 一次解析到位。
- `materialize_view(id, limit)` — 跑 view filter
- `resolve_tag(name)` — 任意写法 → 规范 tag id

**搜索层**
- `search_metadata(text?, tags_all?, tags_any?, tags_none?, catalog_id?, catalog_subtree?, view_id?, kind?, lifecycle?, include_container_paths?, limit)`
  - text 走 ILIKE on summary+extra
  - catalog_id 精确单节点 vs catalog_subtree 递归(互斥)

**内容层**
- `read_entries_metadata(entry_ids, related_limit=10)` — 批量 metadata + 自动附 `related_entries`
- `read_files(requests)` — 批量读原文。每 request: `{entry_id, locations[], search?}`。同一文件多次出现 → 单次 storage 打开。Locations 支持 unit=section / pages / lines / bytes / heading

**结构化数据层**
- `query_table(entry_id, sql, chart_hint?, chart_spec?)` — 打开 in-memory DuckDB,read_csv_auto / read_xlsx 原文件,跑 SQL 白名单(SELECT/SHOW/DESCRIBE/EXPLAIN/PRAGMA),关闭。Schema 在工具描述里 bind-time 注入
- `query_log(entry_id, sql, ...)` — 同 query_table,格式由 ingest 时识别记录的 description.format 决定
- `generate_chart(...)` — 产出 Vega-Lite spec,**单向给用户**

**容器层**
- `analyze_container(container_entry_id, list_files?, read_files?, search?)` — 临时解压 + 列文件 / 读内部文件 / 内部 grep。一次调用共享一次解压

### 4.2 框架自动行为(不是工具)

#### 稳定层注入(prompt cache 核心)

每次 agent LLM 调用的系统提示前缀:

```
[系统提示]
[工具定义]
[catalog 一级节点:id + name + summary + entry 数] × N
[全部 view 列表:id + name + summary] × M
[tag 词表快照:按 facet 分组,频次截断 top-K]
[最近 20 条 journal 简介]
```

由离线任务(normalize_tags 完成后)生成快照,整体替换。**在线对话期间永不修改**——agent 多轮对话的前缀缓存命中率最大化。

#### 每轮末尾自动追加 budget

```
[turn N tail]
本轮已用:12 次工具调用 / 估算 18000 token / 剩余预算 60%
```

agent **没有** `report_budget` 工具——budget 由框架注入。

#### 引用自动收集

agent 在最终 markdown 用角标:

```
扩散模型的训练目标是预测噪声 [^a]。
[^a]: entry_id=E123, section_id=s3
```

框架在 conversation 终止后扫角标写入 `conversations.tool_calls.citations`。agent 没有 `mark_finding` 工具——引用是写作动作的副产品。

#### 终止检测

agent 最后一轮无 `tool_calls` = 自然终止。框架:
1. 写 agent_response
2. 提取角标 citations
3. 入队 `reflect_turn`(priority 30)

agent 没有 `commit_answer` 工具——终止是状态而非动作。

#### plan-execute 分阶段

每个新 user_message:

**Plan 阶段**(一次 LLM 调用,**`tools=[]`**)
- 输入:系统提示 + 稳定层 + 用户问题
- 输出:plan 文本(步骤、预期产出、退出条件、预算估算)
- **不能调任何工具**——纯文本规划

**Execute 阶段**(多轮 LLM 调用,完整 tool 绑定)
- 输入:稳定层 + 问题 + plan + 历次 tool 调用历史
- plan 作为约束嵌入提示词:偏离 plan 在 reasoning 中说明,**不重新生成 plan**(保持缓存)

为什么 plan 阶段零工具:调查员动身查资料前先想清楚要查什么,不是边走边想。零工具阶段产出的 plan 也保证 prompt 前缀稳定。

---

## 5. Ingest Pipeline

### 5.1 路由

`ingest_file` handler 按 `files.mime_type` + `files.original_ext` 查注册表。新文件类型 = 注册新 pipeline,**不改 dispatcher**。

### 5.2 通用契约

每个 pipeline 接受:
- `file_id`
- 文件原文(自己从 storage 取)
- folder 路径 + 同 folder 兄弟 display_names + 当前 catalog 一级 + 当前 tag 词表(作提示)

一次 LLM 调用产出:
- `files.{summary, description, kind, extra}`(write-once)
- 该 entry 的 `{catalog_id, extra, entry_tags}`

### 5.3 V1 Pipeline

**第一批(必须)**

| Pipeline | 触发 | description 形态 |
|---|---|---|
| text | `text/markdown`, `text/plain`, `.txt .md .rst` | sections(heading-path / line-range) |
| code | `.py .ts .go .rs` 等 | symbols(class/function + 行号) |
| pdf | `application/pdf` | sections(per-page);扫描件回退 OCR |
| docx | `.docx` | sections(heading-path) |
| tabular | csv / xlsx / parquet / sqlite | columns + samples + row_count |
| image | `image/*` | caption + ocr_text + elements(VLM) |

**第二批**

pptx / ebook / git_repo / archive / log(tabular 子情况)

**第三批**

audio(Whisper)/ video(关键帧 VLM + audio_transcript)/ mailbox(mbox 拆每封)

### 5.4 容器 Pipeline

**关键决策**:git 仓库 / 压缩包作为单个 file 行处理,**不拆成多个 leaf file**。Schema 零改动。

容器的 `description`:

```jsonc
{
  "container_kind": "git_repo" | "zip_archive" | "tar_archive" | "mbox" | ...,
  "file_count": 234,
  "total_uncompressed_bytes": 12345678,
  "primary_language": "python",                  // 仅 git_repo
  "frameworks_detected": ["FastAPI"],            // 仅 git_repo
  "tree": { "src/": {"file_count": 80, "kinds": ["code"]}, ... },
  "indexed_files": [                             // 完整内部清单(不做 LLM)
    {"path": "src/auth/login.py", "size": 2048, "mime": "text/x-python"}
  ],
  "key_files": [                                 // 选几个做轻量摘要
    {"path": "README.md", "summary": "FastAPI note system with SQLite"}
  ],
  "ingest_filters_applied": [".gitignore", "node_modules/", "*.lock"]
}
```

ingest 流程:
1. 流式下载到临时目录
2. 应用 `.gitignore` + 内置 ignore + 安全限制(路径穿越 / 单文件大小 / 总解压大小 / 压缩比)
3. 枚举 → indexed_files
4. 选 key_files 做 1-2 句摘要
5. ONE LLM 调用产出 summary / description / tags / extra
6. **不为内部每个文件做 LLM**——内部探索由 agent 时刻 `analyze_container` 临时跑

---

## 6. 关键流程

### 6.1 用户上传

```
POST /folders/{folder_id}/upload
   │
   ├─ 流式 sha256 + 写 storage(如果新内容)
   ├─ 查 files.sha256
   │
   ├─ sha256 命中:
   │   ├─ 复用 files 行
   │   ├─ INSERT file_entries(folder_id, file_id, display_name)
   │   ├─ 拷贝种子:catalog_id / extra / entry_tags(source='dedup_seed')
   │   ├─ 不拷贝 entry_relations
   │   └─ 不入队 ingest 任务
   │
   └─ sha256 未命中:
       ├─ INSERT files(ingest_status='pending')
       ├─ INSERT file_entries
       └─ 入队 ingest_file(priority 50)
```

### 6.2 Agent 在线推理

```
POST /sessions/{session_id}/turn
   │
   └─ 创建 conversation 行
       │
       ├─ Plan 阶段(tools=[],一次 LLM)
       │   输出:plan 文本
       │
       └─ Execute 阶段(多轮 LLM)
           ├─ plan 作为约束嵌入提示词
           ├─ 每轮:LLM 决定调工具或给最终答案
           ├─ 终止检测:最后一轮无 tool_call → 写 agent_response
           ├─ 扫角标提取 citations
           └─ 入队 reflect_turn(priority 30)
```

### 6.3 离线周期

```
periodic_tick 每 10 分钟一次
   │
   ├─ 对每个 kind 查"最近一次 done 是什么时候"
   ├─ 超过间隔的 kind → enqueue(dedup_key 防重入)
   └─ 入队顺序受 priority 控制
```

无外部 cron / systemd / Celery beat。

---

## 7. 不变量(代码必须 enforce)

### 7.1 写权限边界

| 主体 | 可写 |
|---|---|
| 👤 用户 | folders 全字段;file_entries(folder_id / display_name / lifecycle 用户层切换 / 软删);上传新 file 物理字段 |
| 🏛️ 图书馆员 | files 内容字段(write-once);file_entries.catalog_id;catalogs / views / tags / tag_aliases / entry_tags |
| 🔍 调查员 | journal / entry_relations / entry_tags(reflect 增补)/ file_entries.extra / catalogs.extra / views.extra |
| 基础设施 | audit_events / sessions / conversations / tasks / task_outcomes |

### 7.2 必须 enforce 的写规则

1. **`files.{summary, description, extra, kind}` 是 write-once**——repository 层 `update_content` 检查 `ingested_at`,非 NULL 抛 `WriteOnceViolation`。**不能依赖 service 自觉**。
2. **AI 永不删除任何文件 / entry / journal / entry_relations 行**——只能调 lifecycle / observation_count 增量。删除是用户专属(软删 → purge_deleted_files 兑现)。
3. **`audit_events` / `task_outcomes` 只 INSERT 不 UPDATE**——prune_* 是唯一删除路径。
4. **`entry_relations` 永远对称对(a < b)**——构造时 service 层 enforce。
5. **每个数据库变化必须同事务写入对应 audit_events**——由统一的 `audit.write()` 函数封装。
6. **`tags.alias_of` 必须指向 `alias_of IS NULL` 的规范 tag**——不能链式。normalize_tags 保证。

### 7.3 必须 enforce 的读规则

1. **Agent 不读 audit_events / sessions / conversations**——"过去经验"通过 `search_journal`。
2. **离线任务不读 audit_events**——调度判定走 `task_outcomes`。这条边界一旦破坏,audit 立刻失去"事件流"语义。
3. **用户不读 AI-internal 表**——catalogs / views / tags / journal / entry_relations / entry_tags / tag_aliases 不暴露给用户层 API。
4. **AI 字段不能在用户视角"隔层暴露"**——比如 `GET /file-entries/{id}` 不能返回 catalog / tags 字段。

### 7.4 lifecycle 状态机

- `active → demoted → archived` 单向自动迁移
- `manual_active` / `manual_archived` 用户锁定,系统状态机**不动**
- 任何 lifecycle ↔ manual_*:用户操作触发
- **永远 NO 自动恢复 active**——升级只能由"使用"触发(实际查询 / 编辑)

### 7.5 容器边界

- 容器作为单个 file 处理,**不创建内部 leaf file 行**
- 内部内容仅在 `analyze_container` 时临时解压
- 引用容器内部用 `[^a]: entry_id=<container>, container_path=src/auth/login.py, lines=42-58`
- 容器内部的 file_relations / 内部 tag 由 reflect 写到 journal,**不进** entry_relations 表

---

## 8. 设计原则(非妥协)

13 条按重要性排序。每条都是硬约束,违反它意味着回到早期审视的死胡同。

1. **涌现优先于预定义**——组织结构(词表 / 分类 / 关系 / 视图)从积累的内容和使用中长出来。预设机制可以,预设内容不行。
2. **工具仅服务于外部动作**——LLM 自己做不到的事才做工具。LLM 通过自然语言能表达的事(思考、改主意、引用)不做工具。
3. **工具是给 AI 的**——简单粗糙的原语优于智能复杂的工具。
4. **plan 阶段零工具**——`tools=[]`。仅靠系统提示 + 稳定层 + 用户问题产出 plan。
5. **不切块、不嵌入**——agent 直接读原文。检索靠结构化访问点。
6. **不 FTS / 不 tsvector**——元数据上的关键字搜索用 ILIKE 兜底,绝不引入分词器和倒排索引。
7. **DuckDB 仅用作 agent 时刻的临时计算引擎**——零持久化、零预 ingest。
8. **每文件类型一条可插拔 pipeline**——pipelines 是注册表。
9. **用户写 ↔ AI 写严格隔离,AI 读两层**——AI 读用户层(folder 路径、display_name)作为先验提示,但不写。
10. **`files.{summary, description, extra}` write-once**——一次写入,`ingested_at` 锁定。
11. **lifecycle 是计算成本契约;AI 永不删除**——三档决定 AI 在哪些 entry 上花算力。删除是用户专属。
12. **受控词表事后涌现,只用于 tags**——词表只约束 tags,不约束 summary/description/extra。
13. **不为企业做架构投资**——保持单用户哲学。如果要做企业版,基于成熟的个人版起新子项目。

### 拒绝清单

- ❌ 向量数据库 / embedding——哲学拒绝
- ❌ 全文搜索引擎(Elasticsearch / Typesense / FTS5 / tsvector)
- ❌ 外部任务队列(Celery / RQ / Redis)
- ❌ 分布式追踪 / Prometheus / Grafana
- ❌ 多租户 / SaaS 化 / owner_id / 鉴权 / 加密 / 计费
- ❌ AI 主动推送
- ❌ 物理删除文件(AI 不能)
- ❌ 上下位词层级(与 catalog 树重叠)
- ❌ 主动馆藏淘汰(违反"AI 永不删除")

---

## 9. 技术栈

**后端**:FastAPI + SQLAlchemy 2.0 async + Alembic + 自实现 uuid7

**数据库**:SQLite(默认,WAL 并发读)/ PostgreSQL(可选,FOR UPDATE SKIP LOCKED 真队列)。切换:`DB_BACKEND=sqlite|postgres`

**存储**:本地文件夹(默认,sha256 前缀分片)/ S3 / MinIO(可选)。切换:`STORAGE_BACKEND=local|s3`

**临时分析**:DuckDB(in-memory,agent 工具内部)

**LLM**:任何 OpenAI 兼容 API。每个 pipeline 可独立选择模型。配置:`OPENAI_API_BASE` / `OPENAI_API_KEY` / `DEFAULT_MODEL` / `STRONG_MODEL` / `VLM_MODEL`

**前端**:CLI 应用(opencode 形态)。OpenAPI 调本地 FastAPI 后端。CLI 可远程连接(自部署场景)。

---

## 10. 速查

| 项目 | 数量 |
|---|---|
| 设计原则 | 13 |
| 业务表 | 14(+ alembic_version) |
| 任务 kind | 10 + periodic_tick |
| Agent 工具 | 13 |
| 数据架构层 | 4 |
| AI 内部回忆机制 | 3(entry_relations / journal / views) |
| Tag facet | 6(topic / form / time / source / language / extra) |
| Lifecycle 取值 | 5(active / demoted / archived / manual_active / manual_archived) |

### 字段维护责任(孤儿字段排查)

| 表 | 字段 | 写者 | 读者 |
|---|---|---|---|
| folders | 全字段 | 用户 API | agent + 上传 + ingest |
| file_entries | folder_id / display_name / 软删字段 | 用户 API | agent + purge |
| file_entries | lifecycle | 用户 + suggest_demotion / suggest_archival | search + 离线 batch |
| file_entries | catalog_id | ingest + restructure + 上传 dedup | search + read_entries_metadata |
| file_entries | extra | ingest + reflect + 上传 dedup | read_entries_metadata + search ILIKE |
| files | 物理字段 | 上传 | dedup / storage / pipeline |
| files | summary / description / extra / kind | ingest_file (write-once) | read_entries_metadata + search |
| files | ingest_status / ingested_at | ingest + recover_stuck | search 默认过滤 |
| files | deleted_at | purge_deleted_files | 物理删除标志 |
| audit_events | 全字段 | 所有写动作通过 audit.write() | 人类管理 + prune |
| sessions | started_at / initiating_user_message | session 开始 | 人类管理 |
| sessions | ended_at / end_reason | session 结束 + recover_stuck | 人类管理 |
| sessions | total_* | 增量(audit + conversation 触发) | 人类管理 |
| conversations | session_id / turn_index / user_message | turn 开始 | 人类管理 |
| conversations | agent_response / ended_at | 终止检测 | reflect |
| conversations | tool_calls / llm_calls | 实时 append | reflect + 人类管理 |
| catalogs | name / parent_id / summary / description / tags | ingest + restructure | agent (list / read) |
| catalogs | extra | reflect + restructure | agent (read) |
| catalogs | deleted_at | restructure(合并节点) | 列出过滤 |
| views | 全字段(除 extra) | reflect + restructure | agent (materialize) |
| views | extra | reflect + restructure | agent |
| tags | name / facet | ingest 创建 | agent (resolve) + search |
| tags | alias_of | normalize | resolve_tag |
| tags | doc_count | normalize 重算 | 词表快照截断 |
| tags | last_used_at | entry_tags 写入时 | 词表快照排序 |
| tag_aliases | 全字段 | normalize + reflect | resolve_tag fallback |
| entry_tags | 全字段 | ingest / reflect / enrich / dedup / normalize 合并 | search + read_entries |
| entry_relations | 全字段 | reflect | read_entries 自动附 |
| journal | 全字段 | reflect (append-only) | search_journal |
| tasks | 全字段 | 上传 / reflect 终止 / periodic_tick + worker 状态机 + recover_stuck | TaskRunner.claim_batch |
| task_outcomes | 全字段 | 离线 handler 处理完每对象 INSERT | 调度面读取 |

**结论:14 张表所有字段都有明确写者读者,无孤儿字段。**

---

**文档版本**:v2(重写,去除冗余阐释)
