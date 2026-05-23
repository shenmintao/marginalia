# Marginalia 设计文档

> 本文档是 Marginalia 项目的**单一真相源**——描述系统是什么、各部分如何协作、字段如何定义、用户如何使用、未来如何演进。
>
> 文档自包含，不依赖外部参考文件。

---

## 目录

1. [项目愿景](#1-项目愿景)
2. [核心比喻：图书馆员 + 调查员](#2-核心比喻图书馆员--调查员)
3. [典型使用场景](#3-典型使用场景)
4. [技术栈与选型理由](#4-技术栈与选型理由)
5. [设计原则](#5-设计原则)
6. [图书馆学借鉴对照](#6-图书馆学借鉴对照)
7. [四层数据架构](#7-四层数据架构)
8. [数据模型](#8-数据模型)
9. [任务系统](#9-任务系统)
10. [Agent 工具集](#10-agent-工具集)
11. [文件 Ingest Pipeline](#11-文件-ingest-pipeline)
12. [关键流程](#12-关键流程)
13. [Provenance：来源记录是一等公民](#13-provenance来源记录是一等公民)
14. [不变量与边界规则](#14-不变量与边界规则)
15. [快速入门](#15-快速入门)
16. [V1 范围与延后项](#16-v1-范围与延后项)
17. [未来路径](#17-未来路径)
18. [附录](#18-附录)

---

## 1. 项目愿景

**Marginalia** 是一个图书馆学风格的个人知识管理系统——把图书馆几百年实践的编目、分类、检索智慧，与 LLM agent 的语义理解能力结合，做成一个能伴随用户长期使用、越用越懂用户的私人知识库。

### 它解决什么问题

随着用户积累的资料越来越多（论文、邮件、笔记、代码、会议记录、合同……），传统文件系统在两个维度上失效：

1. **找不到**：文件夹结构是用户当下的整理逻辑，几个月后想找具体内容时已经记不清当时怎么放的
2. **看不全**：跨文档的关联（一份合同对应一封邮件、一篇论文呼应另一篇）只存在用户脑子里，文件之间没有连接

通用搜索引擎（grep / Spotlight / 各种 RAG 产品）能解决一部分"找不到"，但解决方式是**关键字模糊匹配**——用户得记住关键字、AI 不真正理解内容、用得越多噪音越大、跨文档关联完全无能为力。

Marginalia 走另一条路：**让 AI 像专业图书馆员那样真正理解每一份资料、为它编目、归类、加标签、标注关联，并能像专业调查员那样在用户提问时穿越所有资料、给出有据可查的回答。**

### 它适合谁

- **个人研究者**：积累论文、笔记、实验数据，需要长期跨主题查找和综合
- **知识工作者**：处理大量邮件、合同、会议纪要，需要回看历史决策的来龙去脉
- **写作者**：需要管理素材、引用、思考片段
- **任何积累了够多资料、感到失去掌控的人**

### 它能为用户做什么

**上传任何文件**——PDF、Word、Excel、代码、图片、音频、邮箱归档、Git 仓库压缩包……AI 自动识别文件类型并选用合适的方式理解。

**自由整理**——用户像用百度网盘一样建文件夹、命名、移动文件。这一层完全由用户掌控，AI 不干涉。

**自然语言提问**——比如：
- "我之前那个关于扩散模型的论文里，关于评分匹配的部分讲了什么？"
- "Q3 财务复盘的那次讨论涉及哪些文件？最后的决议是什么？"
- "我收集的这些 React 项目里，有没有用 Tailwind + tRPC 组合的？"
- "上周写的会议纪要和上个月那份预算文档有没有矛盾的地方？"

AI（调查员身份）会规划查阅路径、读相关文档、交叉比对，**给出带引用的答案**——用户可以点击引用直接跳到原文段落核对。

**长期演化**——AI 在后台（图书馆员身份）持续整理：归并同义标签、重整分类、把归档不再活跃的资料移到二线、为新涌现的概念预留位置。用户什么都不用管，库越用越精确、越用越懂用户的关注重心。

**导出研究报告**——任何对话的答案都能导出为带引用角标的 markdown，或打包成含引用原文片段的 zip。这是 Marginalia 真正的产出物——不只是"我的资料库"，而是"这些资料帮我得出的回答"。导出后可分享给不能访问 Marginalia 实例的同事、粘贴到笔记软件、或长期归档。

### 设计哲学

Marginalia 不预先决定任何组织结构——分类树、标签词表、文件关联、视图聚合——全部从用户上传的内容和使用模式中**涌现出来**。系统的初始状态是几乎空白的，几个月使用后会自然长成符合用户领域的形态。这是图书馆学经过几百年实践得出的核心智慧：**编目质量来自实际使用，不来自先验设计**。

---

## 2. 核心比喻：图书馆员 + 调查员

整个系统的设计围绕一个清晰的人物原型——**AI 在 Marginalia 中既是图书馆员，又是调查员**。这两个身份职责完全分离，对应不同的运行节奏：

| 身份 | 何时活跃 | 在做什么 |
|---|---|---|
| 🏛️ **图书馆员** | 离线（后台批处理） | 整理藏书、维护卡片目录、归类、加标签、归档 |
| 🔍 **调查员** | 在线（用户提问时） | 查阅资料、跨册比对、验证假设、写笔记 |
| 👤 **用户** | 任意 | 投递新书、取阅、归档、销毁 |

### 两个 AI 身份的关键差异

- **图书馆员** 离线工作——后台任务整理藏书时不被任何对话打扰
- **调查员** 在线工作——只在用户提问时活跃，**不重整书架**，只读和写笔记
- 两者通过**藏书状态**间接互动——调查员把对话中的发现写在自己的笔记本（`journal`）里，图书馆员之后看到这些笔记决定是否要重整 catalog 或合并 tag

### 为什么用这个比喻

这个比喻不只是修辞——它**结构性地决定**了系统的方方面面：

| 设计选择 | 比喻解释 |
|---|---|
| `files.summary` write-once | 入册时给书写的卡片描述是关于书本身的，不会因为它被借出就改写 |
| AI 字段 per-entry | 同一本书在"新书展架" vs "Q3 财务专架"——同一本书在不同陈列位置可能贴不同标签 |
| `journal` 不给用户看 | 调查员的笔记本是私人工作工具，给读者的产物是调查报告（agent 答案），不是笔记本 |
| AI 永不删除 | 图书馆员永远不烧书，只能把书移到储藏室（archived） |
| plan 阶段零工具 | 调查员动身查资料前先想清楚要查什么，不是边走边想 |
| 不切块/不嵌入 | 调查员翻原文，不依赖"事先把书撕成片再加索引" |
| `audit_events` 90 天滚动 | 图书馆的安全监控录像按周期覆盖 |
| 不维护 cooccurrence 统计表 | 真有"两本书常被一起借"的统计需求，调查员当下数一下就行，不需要预算的统计表 |

### 身份判别原则

遇到新的设计问题时问自己：**这件事是图书馆员、调查员、还是用户该做的？** 三者之外的都要警惕——很可能是过度设计。

举例：
- "Agent 应不应该能合并 tag？" → 这是图书馆员的工作（离线 `normalize_tags` 任务），调查员不动手
- "用户应该能看到 catalog 树吗？" → catalog 是图书馆员的卡片目录，是工作工具，不给读者看
- "Agent 应不应该能给文件改 summary？" → 入册卡片是图书馆员的产物，调查员不改

如果某个新功能让人难以判断"是哪个身份在做"——很可能这个功能本身概念不清，需要先想清楚再实施。

---

## 3. 典型使用场景

下面用几个具体故事展示用户如何与 Marginalia 协作。每个故事以**用户视角**展开——技术细节（哪个工具、哪张表）留到后面章节。

### 场景 A：写综述前的资料梳理

**张博士**做扩散模型相关研究。三年下来她在 `~/papers/` 里塞了 600 多篇论文，分散在十几个子文件夹。她要写一篇关于"score-based 方法"的综述章节。

她在 CLI 里问：

> "我收藏的扩散模型论文里，哪些是关于 score-based 方法的？把它们的核心贡献按时间顺序总结一下，注明每个观点出自哪篇。"

几十秒后，agent 的答案出现在终端——大约 800 字的综述，每个论断后面带 `[^a]` `[^b]` 这样的角标，列出引用的论文标题、作者、具体章节。

她让 agent 把答案打包成研究报告：

```bash
marg export $LAST --bundle -o score-based-review.zip
```

`score-based-review.zip` 里有：
- `answer.md` ——综述全文 + 引用列表
- `citations/` ——每篇被引用的论文相关章节的原文片段（PDF 切页或文本摘录）
- `metadata.json` ——对话时间、模型、token 用量

她把这个 zip 发给合作者——对方不需要安装 Marginalia，直接读 markdown 就能看到完整论证 + 证据原文。综述初稿基本成型。

**Marginalia 替她做了什么：**
- 几个月前 ingest 时已经为每篇论文写好 summary、提取了 tags、归类到 AI 内部的"扩散模型"分类树
- 这次提问时 agent 自动识别"score-based"的同义词（库里可能曾写作"评分匹配"），跨文档比对后综合
- 答案的每个引用都可追溯到原文段落，写论文时直接用，不用回头核对

### 场景 B：回顾决策的来龙去脉

**张经理**把工作邮箱每月导出为 `.mbox` 上传到 Marginalia，常用的合同、会议纪要也定期上传。某天领导问起："Q3 预算从削减 20% 改成 15% 的过程是怎么走完的？"——他凭印象记得有过几轮讨论但具体谁在哪一步同意了什么记不清了。

他问 agent：

> "Q3 预算调整的决议过程是怎样的？时间线 + 涉及哪些邮件和文档 + 最终决议人。"

agent 给出按时间排序的事件链：

> 2024-08-05 财务部首次邮件提出削减 20% [^a]
> 2024-08-08 业务部回复反对 [^b]
> 2024-08-12 高管会议纪要决议改为 15% 并加入备用方案 [^c]
> 最终决议人：CFO，会议纪要第 3 页签字 [^d]

他用 `marg export` 导出 markdown 直接粘贴到给领导的回信里。从被问到回复完毕，10 分钟。

**Marginalia 替他做了什么：**
- 邮箱 mbox 当作容器入册，内部几千封邮件无需逐封人读 LLM 处理——agent 调用时按需打开搜索
- 跨多个数据源（邮件 + 会议纪要 PDF + 合同）交叉比对，时间线自动对齐
- 引用直接落在邮件 ID 和文档页码上，可追溯

### 场景 C：写作的素材综合

**王编辑**手头有几百份素材：调研访谈录音（已经被自动转写）、政策文件 PDF、参考书籍片段、自己的笔记。她在写一篇关于职业教育改革的长文。

写作过程中她随时问 agent：

> "我之前调研里对'产教融合'这个话题最有洞察的几段是什么？"
> "这些观点和教育部 2023 年那份政策文件有矛盾的地方吗？"
> "把所有访谈里对'职业本科'看法划分成支持/反对/中立三类。"

每次提问都得到带引用的回答。最后她让 agent 把这一系列对话整体导出：

```bash
marg export $SESSION_ID --all --bundle -o vocational-edu-research.zip
```

得到的是**完整的研究过程档案**——每次提问、每次回答、所有引用片段——可以作为这篇文章的"研究笔记"附录，也是下次再写相关主题时的种子。

**Marginalia 替她做了什么：**
- 不同来源（音频、PDF、文本笔记）统一在一个对话界面里查询
- agent 在多轮 follow-up 中保持上下文，逐步深化
- 整个 session 可以打包成一个研究档案——比手工整理素材高效一个量级

### 场景 D：半年后的库

**任何用户**使用半年后会发现：

- **找东西更准了**：早期问问题 agent 经常找不到角度，半年后 agent 起步先翻笔记本（journal），命中率明显提高——它记得过去类似问题怎么走通的
- **库自己变整齐了**：当初上传时随手起的 tag 名、分散的子主题，现在已经被后台默默归并、重整。用户从未做过手动分类，但库内已经长出符合自己领域的分类树
- **不重要的东西自动让位**：半年没碰过的资料被自动降级、一年的归档；它们不会消失（永远能搜到、永远能恢复），但默认不参与新查询的检索范围。算力花在真正活跃的内容上
- **历史可追溯**：每次 agent 给出的研究报告都已导出存档；半年前那次"Q3 预算调研"现在还能在本地 zip 里翻出来——不依赖任何在线服务、不会因为产品变化而丢失

整个过程**用户没有主动做任何整理工作**——所有结构都是 AI 在后台从使用中涌现出来的。这是 Marginalia 与传统笔记/文件系统最根本的差别：**整理本身不需要用户付出心智成本**。

---

## 4. 技术栈与选型理由

### 后端

- **FastAPI**：Python 异步 Web 框架。选型理由：与 LLM 调用（`anthropic`、`openai` SDK）天然异步契合；OpenAPI 自动文档对前端友好
- **SQLAlchemy 2.0 (async)**：ORM。同时支持 SQLite 和 PostgreSQL，schema 用同一份模型定义
- **Alembic**：数据库迁移
- **uuid7（自实现）**：所有主键。时间有序，索引友好

### 数据库（双后端，按需切换）

- **SQLite**：默认。零运维，单文件，适合个人本地使用。WAL 模式启用并发读
- **PostgreSQL**：可选。多 worker 并发、`FOR UPDATE SKIP LOCKED` 真正的队列声明，适合更大规模

切换通过 `.env` 一行配置：`DB_BACKEND=sqlite|postgres`。应用层零差异。

### 存储（双后端）

- **本地文件夹**：默认。物理 UUID 寻址，按 sha256 前缀分片
- **S3 / MinIO**：可选。支持 Range 读、按 sha256 前缀分片、生命周期策略

切换：`STORAGE_BACKEND=local|s3`。

### 临时分析引擎

- **DuckDB**：在 agent 调用 `query_table` / `query_log` 时打开 in-memory 连接，直接 `read_csv_auto` / `read_xlsx` / `read_parquet` 原文件，跑 SQL，关闭。**永不持久化**——纯粹是 agent 工具内部的临时计算

### LLM 集成

- **接口标准：OpenAI 兼容 API**（任何兼容 `/v1/chat/completions` 的 provider 都可即插即用）
- **推荐模型：DeepSeek V4 Flash**——便宜、缓存命中率高，特别适合 Marginalia 这种"长上下文 + 多次重用稳定层"的工作负载。Marginalia 的提示词架构（稳定层 + plan-execute 缓存友好布局）正是为高缓存命中率设计的，DeepSeek V4 Flash 的定价模型让这个设计的成本优势最大化
- 每个 pipeline 可独立选择模型（强模型用于 ingest / reflect / restructure 等高价值任务；轻量任务可用更便宜的）
- 图像走 VLM、音频走 ASR——这些可以是不同的 OpenAI 兼容 endpoint
- 配置项：`OPENAI_API_BASE` / `OPENAI_API_KEY` / `DEFAULT_MODEL` / `STRONG_MODEL` / `VLM_MODEL` 等

### 前端

V1 的最小可用前端是一个 **CLI 应用**（类似 [opencode](https://github.com/opencode-ai/opencode) 的形态），不是浏览器 UI。这选择契合 Marginalia 的工程师/研究者目标用户群——他们已经习惯在终端里工作，CLI 的开发成本远低于完整 web UI，且天然适配本地单用户部署。

CLI 提供：
- **文件管理子命令**：`marg upload <path>` / `marg ls [folder]` / `marg mv` / `marg rm` / `marg archive` / `marg restore`
- **对话子命令**：`marg ask "问题"`（一次性问答）/ `marg chat`（进入交互式 session）
- **管理子命令**：`marg tasks`（查任务队列）/ `marg audit`（查审计事件）/ `marg gc`（手动触发清理）
- **图表渲染**：CLI 收到 Vega-Lite spec 时，可调用 `vl-convert` 转 PNG 并用终端图像协议（kitty/iTerm2）显示，或回退到 ASCII 图

CLI 通过 OpenAPI 客户端调用本地 FastAPI 后端（默认 `http://localhost:8000`）。后端可单独运行；CLI 可远程连接（自部署场景）。

未来可补浏览器 UI 作为可选第二客户端——后端 API 与 CLI 完全解耦。

### 不用什么

- ❌ **向量数据库 / embedding**——哲学拒绝
- ❌ **全文搜索引擎（Elasticsearch / Typesense / FTS5 / tsvector）**——哲学拒绝
- ❌ **外部任务队列（Celery / RQ / Redis）**——内置 `tasks` 表 + asyncio worker 足够
- ❌ **分布式追踪 / Prometheus / Grafana**——V1 单用户，简单日志即可

---

## 5. 设计原则

13 条根本原则，按重要性排序。每条都是**不可妥协的硬约束**，违反它意味着回到早期设计审视的死胡同。

1. **涌现优先于预定义**：每个组织结构（词表、分类、关系、视图）都从积累的内容和使用中长出来，不预先声明。预设机制可以，预设内容不行。

2. **工具仅服务于外部动作**：Agent 工具只用于 LLM 自己做不到的事（读外部文件、查数据库、跑 SQL）。任何 LLM 通过自然语言能表达的事（思考、改主意、引用）不做成工具。

3. **工具是给 AI agent 的，不是给人类的**：工具消费者是 agent，不是人类。简单粗糙的原语优于智能复杂的工具。`search_metadata` 用 ILIKE 而不是相关性排序——让 agent 自己组合判断。

4. **plan 阶段零工具**：plan 阶段的 LLM 调用 `tools=[]`。仅靠系统提示 + 稳定层快照 + 用户问题产出 plan。不在 plan 阶段访问数据库。

5. **不切块、不嵌入**：Agent 直接读原文。不存切块、不存向量。检索靠结构化访问点（catalog 下钻、tag 过滤、view 聚合）。

6. **不 FTS / 不 tsvector**：不引入全文检索基础设施。元数据上的关键字搜索用 ILIKE 兜底，绝不引入分词器和倒排索引。中文分词陷阱深、语义理解仍然要靠 LLM——FTS 是过度工程。

7. **DuckDB 仅用作 agent 时刻的临时计算引擎**：`query_table` / `query_log` 工具内打开 in-memory 连接、读原文件、跑 SQL、关闭。零持久化、零预 ingest。

8. **每文件类型一条可插拔 pipeline**：ingest 按 mime/ext 路由到对应 pipeline。images→VLM、PDF→分页、code→syntax-aware。pipelines 是注册表。

9. **用户写 ↔ AI 写严格隔离，AI 读两层**：用户只能写 folders / file_entries 部分字段 / 上传/删除/归档；AI 只能写 AI-internal 字段；AI 读用户层（folder 路径、display_name）作为先验提示，但不写。

10. **`files.summary` / `files.description` write-once**：ingest 时一次写入，`ingested_at` 锁定。后续永不更新——它们描述的是文件本身（不变的字节流），不是文件所处的世界。

11. **lifecycle 是计算成本契约；AI 永不删除**：`active`/`demoted`/`archived` 三档决定 AI 在哪些 entry 上花算力。AI 永不删除文件——只能调 lifecycle。删除是用户专属权力。

12. **受控词表事后涌现，只用于 tags**：tag 词表通过离线 `normalize_tags` 任务从 LLM 自由打的 tag 里收敛出来。词表只约束 tags，不约束 summary/description/extra。

13. **不为企业做架构投资**：保持单用户哲学。不加 owner_id / 鉴权 / FTS / embedding 等企业级口子。如果将来要做企业版，应基于成熟的个人版起新子项目。

---

## 6. 图书馆学借鉴对照

Marginalia 的核心价值之一是借鉴图书馆几百年实践智慧。下表列出系统中各机制对应的图书馆学概念：

| 图书馆学概念 | Marginalia 中的体现 |
|---|---|
| **分类树**（Dewey Decimal / Library of Congress Classification） | `catalogs` 表的单父树，由 AI 涌现 |
| **受控词表**（Controlled Vocabulary，如 LCSH） | `tags` + `tag_aliases` |
| **多面分类**（Faceted Classification，Ranganathan PMEST） | `tags.facet` 6 个轴：topic / form / time / source / language / extra |
| **规范档**（Authority File） | `tag_aliases` 把任意写法映射到规范 tag |
| **参见**（See Also） | `entry_relations` 自由 note 关联 |
| **卡片目录**（Catalog Card） | `files.summary` + `files.description` + `file_entries.extra` |
| **页边批注**（Marginalia 项目名本身） | agent 在 `journal` 写下的笔记 |
| **闭架储藏**（Inactive Storage） | `lifecycle = archived` |
| **借阅记录**（Loan Records） | `conversations` 表 + journal 中的 entry_ids 累积 |
| **咨询面谈**（Reference Interview） | agent 的 plan-execute 流程 |
| **来源记录**（Provenance） | `source` / `source_kind` 字段 + `audit_events` 完整事件流 |
| **编目员注记**（Cataloger's Notes） | `catalogs.extra` / `views.extra` |

### 借鉴但 Marginalia 选择不实施的

| 图书馆学概念 | 不实施的原因 |
|---|---|
| 上下位词层级（Broader/Narrower Term） | 与 catalog 树重叠，保持 tag 扁平 + facet 多维 |
| 馆藏淘汰（Weeding） | 违反"AI 永不删除"原则，只做软淘汰（archived） |
| 主动推送（SDI） | 与"agent 仅在用户提问时活跃"哲学冲突 |
| 跨馆互联标准（MARC/Dublin Core） | 个人系统不需要标准化互通，但 description JSON 形态借鉴了"按类型标准化" |

---

## 7. 四层数据架构

数据按职责分成四层，外加调度面（基础设施层），互不重叠：

```
┌──────────────────────────────────────────────────────────────┐
│  第一层 audit_events  (数据库变化事件流)                       │
│  谁写：所有数据库变化动作 / 谁读：人类（管理工具） / 寿命：90天   │
├──────────────────────────────────────────────────────────────┤
│  第二层 sessions / conversations  (容器 + 累计指标)            │
│  谁写：系统在生命周期事件 / 谁读：人类 + 框架 / 寿命：永久      │
├──────────────────────────────────────────────────────────────┤
│  第三层 AI-internal  (catalogs/views/tags/tag_aliases/         │
│                      entry_tags/entry_relations/journal)       │
│  谁写：ingest/reflect/离线 / 谁读：Agent only / 寿命：永久      │
├──────────────────────────────────────────────────────────────┤
│  第四层 用户可见  (folders/file_entries/files)                 │
│  谁写：upload + 用户操作 / 谁读：Agent + 用户 / 寿命：永久      │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│  基础设施层  (tasks / task_outcomes)                           │
│  谁写：调度系统 / 谁读：调度系统 / 寿命：tasks 永久, outcomes 30天 │
└──────────────────────────────────────────────────────────────┘
```

**关键属性：**

- **audit_events 是事实流**：每个数据库变化一行；写一次永不更新；agent **和**离线任务都不读这层（业务逻辑读真实数据或 task_outcomes）
- **sessions/conversations 是容器**：纯日志容器，没有 tags/extra/plan/AI 字段；可变字段只有 `total_*` 累计指标
- **AI-internal 数据是 agent 的工作知识**：用户完全看不到；agent 通过工具访问
- **用户可见数据**：用户层的真实文件、文件夹、entries，加可控的 lifecycle 切换
- **基础设施层**：tasks 是任务队列，task_outcomes 是任务对对象的处理记录——离线任务通过 task_outcomes 做幂等 / 最近性判定，**不读 audit_events**

**身份对应关系：**
- 🏛️ **图书馆员** 主要写入 AI-internal 层（除 journal） + files 内容描述字段
- 🔍 **调查员** 主要写入 journal + entry_relations + audit_events（共享基础设施）
- 👤 **用户** 主要写入 用户可见层 + 触发 audit_events
- 系统/框架 写入 audit_events / sessions / conversations 容器 + tasks / task_outcomes

---

## 8. 数据模型

共 **14 张业务表** + 1 张 alembic 元表。所有主键 `uuid7`（时间有序）。所有时间戳 `timestamptz`（UTC）。

每张表的字段表后会列出"字段维护责任"——确认每个字段都有明确的写者与读者，避免孤儿字段。

### 8.1 用户可见层（3 张）

#### `folders` — 用户的虚拟文件夹（树状）

**身份**：👤 用户写 / 🏛️ 图书馆员只读（作为 ingest 提示信号） / 🔍 调查员只读

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `parent_id` | uuid \| null | 父文件夹（null = 根），自引用 |
| `name` | str(255) | 文件夹名 |
| `deleted_at` | timestamptz \| null | 软删时间戳 |
| `created_at`, `updated_at` | timestamptz | |

约束：`UNIQUE(parent_id, name)`（同父下不重名）

**字段维护责任：**
- 全部字段：用户通过 API 写入（创建/重命名/移动/软删）
- `name` 在 ingest 阶段被 LLM 读作位置先验信号
- `parent_id` 通过 `list_folders(parent_id?)` 工具被 agent 读取

#### `file_entries` — 用户的文件引用（含 per-position AI 字段）

**身份**：👤 用户写 lifecycle 相关字段 / 🏛️ 图书馆员写 AI 字段（catalog_id, extra）/ 🔍 调查员可写 extra（reflect_turn）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `folder_id` | uuid FK→folders | 所属文件夹 |
| `file_id` | uuid FK→files | 指向物理文件 |
| `display_name` | str(255) | 用户看到的文件名 |
| `lifecycle` | str(16) | `active` \| `demoted` \| `archived` \| `manual_active` \| `manual_archived` |
| `catalog_id` | uuid \| null FK→catalogs | AI 分类归属（per-entry，dedup 时拷贝种子） |
| `extra` | text \| null | AI 在这个位置上下文的累积理解（per-entry） |
| `deleted_at` | timestamptz \| null | 用户软删时间 |
| `purge_after` | timestamptz \| null | 物理清理时间 |
| `created_at`, `updated_at` | timestamptz | |

**字段维护责任：**
- `folder_id` / `display_name`：用户写（创建/移动/重命名）
- `lifecycle`：系统自动 `suggest_demotion` / `suggest_archival` 转换；用户主动切换 `manual_active` / `manual_archived`
- `catalog_id`：`ingest_file` 初值；`restructure_catalogs` 离线调整；dedup 时从源 entry 拷贝种子
- `extra`：`ingest_file` 初值（位置感知洞察）；`reflect_turn` 覆盖式刷新；dedup 时从源 entry 拷贝种子
- `deleted_at` / `purge_after`：用户软删时设置；`purge_deleted_files` 任务读
- `lifecycle` 五取值合并了原来的 lifecycle + pin 概念。`manual_*` 表示用户锁定，系统状态机不动；其他三档由系统自动转换 `active → demoted → archived`
- `catalog_id` / `extra` 是 per-entry 的 AI 解读——同一份 sha256 在不同 folder 下可有不同分类与洞察
- dedup 时这些 AI 字段从源 entry 拷贝作为种子，之后独立演化

#### `files` — 物理文件（内容寻址，write-once 内容描述）

**身份**：👤 用户上传写物理字段 / 🏛️ 图书馆员写内容描述（write-once）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `storage_key` | str(255) UNIQUE | S3/本地路径 |
| `sha256` | str(64) UNIQUE | 内容哈希（dedup 键） |
| `size_bytes` | bigint | |
| `mime_type` | str(255) \| null | HTTP Content-Type |
| `original_ext` | str(32) \| null | 上传时扩展名 |
| `kind` | str(16) \| null | `text` \| `table` \| `log` \| `image` \| `audio` \| `video` \| `code` \| `container` |
| `summary` | text \| null | LLM 写的内容总结（write-once） |
| `description` | json \| null | LLM 写的导航结构（write-once） |
| `extra` | text \| null | LLM 写的内容洞察（write-once） |
| `ingest_status` | str(16) | `pending` \| `processing` \| `done` \| `failed` |
| `ingested_at` | timestamptz \| null | summary/description 锁定时间戳 |
| `deleted_at` | timestamptz \| null | |
| `created_at`, `updated_at` | timestamptz | |

**字段维护责任：**
- `storage_key` / `sha256` / `size_bytes` / `mime_type` / `original_ext`：上传服务一次性写入
- `summary` / `description` / `extra` / `kind`：`ingest_file` 任务一次性写入并通过 `ingested_at` 锁定（write-once）
- `ingest_status`：上传时初值 `pending`；ingest 任务流转 `processing → done|failed`；`recover_stuck_tasks` 可重置
- `ingested_at`：`ingest_file` 成功完成时设置；为不可逆锁
- `deleted_at`：仅 `purge_deleted_files` 任务在所有引用 entry 都 purge 后写入
- `summary` / `description` / `extra` / `kind` 全部是 write-once——`ingested_at` 设置后永不更新
- `kind` 是独立列（不在 description JSON 里），便于 SQL 查询和 pipeline 路由
- `ingest_status` 显式跟踪 ingest 进度；search_metadata 默认过滤 `ingest_status='done'`

### 8.2 审计层（3 张）

#### `audit_events` — 数据库变化事件流（90 天滚动）

**身份**：[共享基础设施]——所有写动作都触发

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `occurred_at` | timestamptz | |
| `kind` | str(64) | 见下方 kind 取值 |
| `session_id` | uuid \| null | |
| `conversation_id` | uuid \| null | |
| `task_id` | uuid \| null | |
| `payload` | json | 按 kind 不同的结构化负载 |

索引：`(occurred_at)` / `(session_id, occurred_at)` / `(conversation_id, occurred_at)` / `(task_id, occurred_at)` / `(kind, occurred_at)`

**kind 取值（仅记录数据库变化）：**
`file_created` / `file_updated` / `entry_created` / `entry_updated` / `lifecycle_changed` / `journal_entry_written` / `tag_created` / `tag_merged` / `entry_relation_upserted` / `catalog_moved` / `catalog_updated` / `view_updated` / `task_started` / `task_finished` / `task_failed` / `ingest_status_changed` 等。

**不记录** in-memory 的 tool_call / llm_call——那些保存在 conversations 表的 JSON 字段里。

**字段维护责任：**
- 所有字段：写一次永不更新
- `prune_audit_events` 任务每天扫一次，删 90 天前（`AUDIT_RETENTION_DAYS` 配置）的行
- 索引保证按 session/conversation/task/kind 任意维度按时间序读出全过程

#### `sessions` — 一次使用窗口的容器

**身份**：[共享基础设施]——系统在 session 边界写

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `started_at` | timestamptz | |
| `ended_at` | timestamptz \| null | |
| `end_reason` | str(16) \| null | `cleared` \| `normal` \| `unclean` |
| `initiating_user_message` | text | 第一句用户消息 |
| `turn_count` | int | |
| `total_input_tokens` | int | 累计指标 |
| `total_output_tokens` | int | |
| `total_cache_read` | int | |
| `total_tool_calls` | int | |
| `total_llm_calls` | int | |
| `total_cost_estimate` | numeric | |
| `total_duration_ms` | int | |

**字段维护责任：**
- `started_at` / `initiating_user_message`：session 开始时写入
- `ended_at` / `end_reason`：session 边界事件触发写入；非优雅关闭由 `recover_stuck_tasks` 标 `unclean`
- `turn_count`：每个 conversation 开始时增量
- `total_*`：每次 audit_event 写入时同事务增量更新

**边界规则：**
- caller 不绑定 CLI——end_reason 取值不假设入口形态
- 用户主动 `/clear` 触发 → `cleared`
- caller 优雅退出 → `normal`
- 进程崩溃/lease 过期 → `unclean`（由 `recover_stuck_tasks` 标记）
- 无空闲超时

#### `conversations` — session 内一轮活动的完整事实记录

**身份**：[共享基础设施]——每轮 turn 的活动记录

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `session_id` | uuid FK→sessions | |
| `turn_index` | int | session 内第几轮 |
| `started_at`, `ended_at` | timestamptz | |
| `user_message` | text | 用户这轮说的 |
| `agent_response` | text \| null | agent 这轮的最终文本回复 |
| `tool_calls` | json | `[{tool, params, result_excerpt, duration_ms, ...}]` |
| `llm_calls` | json | `[{phase, model, input_tokens, output_tokens, cache, duration_ms}]` |
| `total_input_tokens`, `total_output_tokens` | int | 累计指标 |
| `total_tool_calls`, `total_llm_calls` | int | |
| `total_duration_ms` | int | |
| `total_cost_estimate` | numeric | |

**字段维护责任：**
- `session_id` / `turn_index` / `started_at` / `user_message`：turn 开始时写入
- `agent_response`：终止检测后写入（最后一轮无 tool_call）
- `tool_calls` / `llm_calls`：每次 tool/llm 调用时 JSON append
- `total_*`：每次 audit_event 写入时同事务增量更新
- `ended_at`：终止检测后写入

**关键属性：**
- 没有 `tags` / `extra` / `plan` 字段——那些都不属于审计层
- 人类按时间序读 conversations 行就能完整重现 agent 当时的工作流
- agent 完全不读这张表——agent 的"过去经验"通过 `journal` 获取
- plan 文本仅作为 execute 阶段提示词内容，不持久化（保留在 `llm_calls` 的 plan 调用记录里）

---

### 8.3 AI 内部层（7 张）

#### `catalogs` — AI 分类树

**身份**：🏛️ 图书馆员写 / 🔍 调查员只读 + reflect_turn 写 extra

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `parent_id` | uuid \| null FK→catalogs | 父节点（null = 根） |
| `name` | str(255) | |
| `summary` | text \| null | AI 分类节点的描述 |
| `description` | json \| null | 结构化导航 |
| `extra` | text \| null | AI 对该节点的当前累积理解（mutable） |
| `tags` | json \| null | 该节点的特征 tag |
| `deleted_at` | timestamptz \| null | |
| `created_at`, `updated_at` | timestamptz | |

**字段维护责任：**
- `name` / `parent_id` / `summary` / `description` / `tags`：AI 创建节点时（ingest_file 写新节点 / restructure_catalogs 调整）写入
- `extra`：reflect_turn 在对话触及该 catalog 时覆盖式刷新；restructure_catalogs 也可能更新
- `deleted_at`：仅 restructure_catalogs 在合并节点时写入

**关键属性：**
- catalog 树由 AI 自由演化——不预设任何骨架
- 用户完全不可见，是图书馆员的工作工具
- `extra` 是当前累积理解（mutable），与 journal 不同——后者是历史笔记累积、前者是 entity 当前状态

#### `views` — 跨 catalog 的话题聚合

**身份**：🏛️ 图书馆员写 / 🔍 调查员只读 + reflect_turn 写 extra

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `name` | str(255) | |
| `summary` | text \| null | |
| `description` | json \| null | |
| `extra` | text \| null | mutable 累积理解 |
| `tags` | json \| null | |
| `filter_spec` | json | catalog_subtree / tags_all / tags_any / tags_none / facets / date_range |
| `created_at`, `updated_at` | timestamptz | |

**字段维护责任：**
- `name` / `summary` / `description` / `tags` / `filter_spec`：reflect_turn 或 restructure_catalogs 创建/调整时写入
- `extra`：reflect_turn 在对话触及该 view 时覆盖式刷新

**关键属性：**
- views 全部由 AI 创建（V1 无用户创建入口）
- `filter_spec` 是结构化过滤规范，agent 通过 `materialize_view` 实时跑过滤

#### `tags` — 受控词表（事后涌现）

**身份**：🏛️ 图书馆员写（ingest + normalize_tags） / 🔍 调查员只读

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `name` | str(255) | |
| `facet` | str(16) | `topic` \| `form` \| `time` \| `source` \| `language` \| `extra` |
| `alias_of` | uuid \| null FK→tags | 指向规范 tag |
| `doc_count` | int | 持有该 tag 的 entry 数 |
| `last_used_at` | timestamptz \| null | |
| `created_at`, `updated_at` | timestamptz | |

约束：`UNIQUE(name, facet)`

**字段维护责任：**
- `name` / `facet`：ingest_file 创建新 tag 时写入；后续不变
- `alias_of`：normalize_tags 合并时设置（指向规范 tag）
- `doc_count`：normalize_tags 跑完时重算
- `last_used_at`：每次该 tag 被 entry_tags 引用时更新

**关键属性：**
- facet 6 个轴是预定义机制（不违反涌现原则——facet 是机制，不是内容）
- `extra` facet 是兜底维度，新 facet 通过累积观察后由开发者手动加入代码
- `alias_of` 形成单向映射图——必须指向 alias_of IS NULL 的规范 tag（不能链式）

#### `tag_aliases` — authority file

**身份**：🏛️ 图书馆员写（normalize_tags / reflect_turn）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `from_name` | str(255) | 任意写法（含同义词、缩写、误拼） |
| `to_tag_id` | uuid FK→tags | 规范 tag |
| `note` | text \| null | 合并理由 |
| `created_at` | timestamptz | |

**字段维护责任：**
- 全部字段：normalize_tags 合并时写入；reflect_turn 在对话中发现新别名时写入
- 永不删除——历史合并是事实
- `resolve_tag(name)` 工具优先查 tags.name，未命中再查 tag_aliases.from_name

#### `entry_tags` — entry ↔ tag 关联

**身份**：🏛️ 图书馆员写（ingest + enrich_tags + normalize 合并）/ 🔍 调查员可写（reflect 增补）

| 字段 | 类型 | 说明 |
|---|---|---|
| `entry_id` | uuid FK→file_entries | PK 复合 |
| `tag_id` | uuid FK→tags | PK 复合 |
| `source` | str(16) | `ingest` \| `reflect` \| `enrich_tags` \| `dedup_seed` |
| `created_at` | timestamptz | |

**字段维护责任：**
- ingest_file：初始打 tag
- enrich_tags：用最新词表回填老 entry
- reflect_turn：对话中观察到的新 tag
- normalize_tags：合并时重写指向（DELETE 旧 + INSERT 新指向规范）
- 上传 service：dedup 时从源 entry 拷贝（source='dedup_seed'）

#### `entry_relations` — entry 对的结构性关联

**身份**：🔍 调查员写（仅 reflect_turn）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `entry_a_id` | uuid FK→file_entries | 对称对（a < b） |
| `entry_b_id` | uuid FK→file_entries | |
| `note` | text | 自由文本 |
| `source_kind` | str(16) | `reflect`（V1 仅此） |
| `last_observed_at` | timestamptz | |
| `observation_count` | int | 累积被观察次数 |
| `created_at` | timestamptz | |

约束：`UNIQUE(entry_a_id, entry_b_id)`（一对 entry 一行）

**字段维护责任：**
- 全部字段：仅 reflect_turn 写
- 新对：INSERT，observation_count=1
- 已有对：UPDATE observation_count += 1，last_observed_at，可能 append note

**关键属性：**
- ONE row per pair；构造时 entry_a < entry_b 保证对称性
- **无 kind 受控词表**——note 自由文本，agent 读取时自己判断关系性质
- ingest 不写（单文件视角无法可靠判断关联）

**访问模式：** `read_entries_metadata` 后端 JOIN entry_relations，返回结构里直接附 `related_entries`（默认 top-10 by observation_count）。无独立 `get_related_entries` / `traverse_relations` 工具——agent 想要多跳遍历时自行组合 `read_entries_metadata` 调用。

#### `journal` — 调查员的随身笔记本

**身份**：🔍 调查员写（仅 reflect_turn）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `conversation_id` | uuid FK→conversations | 永远关联到对话 |
| `note` | text | 反思后写的笔记 |
| `entry_ids` | json | 涉及的 entry 列表 `[E1, E4, E7]` |
| `tags` | json | 主题 tags |
| `source_kind` | str(16) | `reflect_turn`（V1 仅此一种） |
| `created_at` | timestamptz | |

**字段维护责任：**
- 全部字段：仅 reflect_turn 写一次（append-only）
- `search_journal` 工具读取（agent 起步翻笔记）

**关键属性：**
- journal 永远是 per-conversation——不是 per-file / per-pair / per-tag
- agent 通过 `search_journal(text?, entry_id?, tags?, since?)` 查找过去笔记
- V1 不淘汰

### 8.4 基础设施（2 张）

#### `tasks` — 统一异步任务队列

**身份**：[共享基础设施]——所有异步工作走这一张表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `kind` | str(64) | 任务类型 |
| `payload` | json | 任务参数 |
| `dedup_key` | str(255) \| null | 幂等键 |
| `status` | str(16) | `pending` \| `running` \| `done` \| `failed` \| `dead` |
| `priority` | int | 越小越优先 |
| `attempts`, `max_attempts` | int | |
| `last_error` | text \| null | |
| `scheduled_at` | timestamptz | 何时可被 claim |
| `lease_expires_at` | timestamptz \| null | 心跳租约 |
| `locked_by` | str(64) \| null | worker id |
| `created_at`, `started_at`, `finished_at` | timestamptz | |

索引：`(status, scheduled_at, priority)` / `(dedup_key)` / `(kind, status)`

**字段维护责任：**
- 创建：上传 service / reflect 终止检测 / periodic_tick / API 调试端点
- claim：worker 在 `_claim_batch` 时 UPDATE status='running' + locked_by + lease_expires_at + started_at + attempts++
- heartbeat：worker 每 N 秒续 `lease_expires_at`
- 完成：worker UPDATE status='done' + finished_at
- 失败：worker UPDATE status='pending'（重试）或 'dead'（达 max_attempts）+ last_error + scheduled_at（指数退避）
- recover_stuck_tasks：扫过期 lease 重置；扫 dead 给一次重试机会

**关键属性：**
- ingest_file、reflect_turn、所有离线 batch 全走这张表
- 零外部 broker（无 Redis/Celery/RabbitMQ）
- claim 机制：Postgres `FOR UPDATE SKIP LOCKED`；SQLite 单 worker 假设
- 幂等：`dedup_key` UNIQUE(WHERE status IN ('pending','running')) 保证同一逻辑任务不重复

#### `task_outcomes` — 任务对对象的处理记录

**身份**：[共享基础设施]——离线任务调度面的事实表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid7 PK | |
| `task_kind` | str(64) | `enrich_tags` / `reflect_turn` / `mine_xxx` ... |
| `object_kind` | str(32) | `file_entry` / `conversation` / `entry_pair` / `global` |
| `object_id` | str(255) | 对象主键，`global` 时填 `'global'` |
| `task_run_id` | str(36) \| null | 可选 → tasks.id（debug 时回溯哪一次 run） |
| `outcome` | str(16) | `applied` \| `noop` \| `rejected` \| `deferred` |
| `detail` | json \| null | 任务自定义结构（`tags_added`、`dropped_ids`、reject 理由等） |
| `completed_at` | timestamptz | |

索引：`(task_kind, object_kind, object_id)` / `(task_kind, completed_at)` / `(completed_at)`

**字段维护责任：**
- 写入：每个离线 task handler 在处理完一个对象后 INSERT 一行（事务内）
- 读取：仅基础设施（runner / dispatcher / 离线 handler 的调度判定部分）
- 删除：`prune_task_outcomes` 任务唯一删除路径，30 天滚动保留

**这张表为什么和 audit_events 分开？**
- audit_events 是**数据变化事件流**——给人类审计看，一切真实变化都在
- task_outcomes 是**调度判定事实表**——给基础设施自己看，回答"上次处理过这个对象吗 / 处理结果是什么"
- 用法不同 → 索引不同（audit 按时间窗口；outcomes 按 (task, object_id)）
- 读者不同 → 边界清晰（人类 vs 调度系统）
- 增长来源不同 → 量级估算独立
- 如果合并：每个新挖掘任务都要往 audit 加新 kind，audit 会被调度查询污染，最终人类无法读懂 audit

**关键属性：**
- INSERT-only（同 audit_events）；prune_task_outcomes 是唯一删除路径
- 同 (task_kind, object_id) 可以有多行（每次处理一行），查询时取 MAX(completed_at)
- detail JSON 由各任务自定义但必须文档化在 handler 模块顶部

---

## 9. 任务系统

所有异步工作走统一 `tasks` 队列。10 个业务 kind + 1 个 dispatcher（`periodic_tick`）。

### 9.1 优先级层级

数字越小越优先。层级反映 Marginalia 的价值排序——用户在场最优先、系统不能坏次之、用户意愿要兑现、质量基础先打牢、结构演化慢慢来、生命周期判断最后做。

| 优先级 | kind | 身份 | 何时触发 | 说明 |
|---|---|---|---|---|
| 30 | `reflect_turn` | 🔍→🏛️ | 每轮 conversation 结束 | 写笔记本（journal）+ 写 entry_relations + 增补 entry_tags + 刷新 entity.extra |
| 50 | `ingest_file` | 🏛️ 图书馆员 | 新 sha256 上传 | 入册新书：写 file 内容字段 + 该 entry 的 AI 字段 |
| 100 | `recover_stuck_tasks` | 🏛️（自愈） | 周期 10 分钟 | 重置 stuck 状态、清理过期 lease、修复未完成 ingest |
| 150 | `purge_deleted_files` | 🏛️（执行用户意愿） | 周期每天 | 兑现用户软删——保留期到了的 entry 真删 + 级联清理孤儿 file |
| 200 | `normalize_tags` | 🏛️ 图书馆员 | 周期 6 小时 | LLM 在 facet 内合并同义 tag、维护 tag_aliases |
| 215 | `enrich_tags` | 🏛️ 图书馆员 | 周期 5 天 | LLM 用最新词表给老 entry 补 tag |
| 220 | `restructure_catalogs` | 🏛️ 图书馆员 | 周期 7 天 | LLM 看 journal 提示决定 catalog 树重组 / 移动 entry |
| 240 | `suggest_demotion` | 🏛️ 图书馆员 | 周期 7 天 | active → demoted（基于活跃度信号） |
| 250 | `suggest_archival` | 🏛️ 图书馆员 | 周期 14 天 | demoted → archived |
| 260 | `prune_audit_events` | 🏛️ 图书馆员 | 周期每天 | 删 90 天前的 audit_events 行 |
| 265 | `prune_task_outcomes` | [共享基础设施] | 周期 7 天 | 删 30 天前的 task_outcomes 行 |
| 300 | `periodic_tick` | [共享基础设施] | 每 10 分钟 | dispatcher：扫各 kind 的 done 时间，按周期入队 |

### 9.2 因果依赖

```
recover_stuck_tasks ─→ 所有其他离线任务（必须先恢复才能跑其他）

normalize_tags ─→ enrich_tags
              ─→ restructure_catalogs

normalize_tags + restructure_catalogs ─→ suggest_demotion
                                       ─→ suggest_archival

purge_deleted_files 自包含（一个事务内删 entry + 检查孤儿 file 一并删）
```

依赖通过 `dedup_key` 串行化保证——每个 kind 同时只有一行 pending/running，下一轮间隔到了才入队。priority + interval + dedup_key 组合自然形成正确顺序，无需显式 DAG。

### 9.3 周期间隔

```python
PERIODIC_INTERVALS = {
    "recover_stuck_tasks":  timedelta(minutes=10),
    "purge_deleted_files":  timedelta(days=1),
    "normalize_tags":       timedelta(hours=6),
    "enrich_tags":          timedelta(days=5),
    "restructure_catalogs": timedelta(days=7),
    "suggest_demotion":     timedelta(days=7),
    "suggest_archival":     timedelta(days=14),
    "prune_audit_events":   timedelta(days=1),
    "prune_task_outcomes":  timedelta(days=7),
}
```

`periodic_tick` 自身每 10 分钟跑一次，扫表对每个 kind 看"最近一次 done 是什么时候"，超过间隔就入队。无外部 cron / systemd / Celery beat。

### 9.4 关键任务细节

#### `ingest_file`（🏛️ 入册新书）
- 输入：`{file_id}`
- 流程：根据 `files.mime_type`/`files.original_ext` 路由到对应 pipeline → pipeline 调用 LLM 一次 → 写入 `files.summary/description/extra/kind` + 创建该 entry 的 `catalog_id/extra/entry_tags`
- write-once：成功后设 `ingested_at` 锁定 file 内容字段
- 失败：标 `ingest_status='failed'`，等 `recover_stuck_tasks` 给一次重试机会

#### `reflect_turn`（🔍→🏛️ 写笔记本 + 刷新 extra）
- 触发：每轮 conversation 结束（agent 最后一轮无 tool call）
- 输入：`{conversation_id}`
- 流程：用强模型 + 长上下文读 conversation 完整事实记录 + 涉及 entry 的当前 metadata
- 产出（每项独立判断要不要写）：
  - 0-N 条 journal（note + entry_ids + tags）
  - 0-N 条 entry_relations 增量（INSERT 新对或 INCREMENT 已有）
  - 0-N 条 entry_tags 增补（source='reflect'）
  - 0-N 条 file_entries.extra 覆盖更新（刷新当前累积理解）
  - 0-N 条 catalogs.extra / views.extra 累积更新（仅当对话触及它们时）
  - 可能给后续离线任务留 hint（写 journal 标 tags=['hint:restructure_catalogs']）
- **不动** files.summary / files.description / files.extra（write-once）
- LLM 自己判断"无可记录" → 直接 done，不强制写 journal

**extra 与 journal 的协作语义：**
- `journal` 是历史快照——"那次对话学到了什么"，append-only
- `file_entries.extra` / `catalogs.extra` / `views.extra` 是 entity **当前累积理解**——可被 reflect 覆盖式更新
- 两者协作：journal 累积成历史；某次 reflect 综合若干 journal 后判断 extra 该刷新就 UPDATE entity 的 extra
- LLM 自己决定哪些洞察值得"晋升"到 extra（覆盖当前理解），哪些只值得记在 journal（历史快照）
- 没有 reflect 写 file_entries.extra 的机制，那个字段就只有 ingest 时被写一次，永远不更新——违反"当前累积理解"语义

#### `recover_stuck_tasks`（🏛️ 自愈）
四类职责：
1. 扫 `tasks WHERE status='running' AND lease_expires_at < now`：worker 死了或 lease 过期 → 重置为 pending
2. 扫 `tasks WHERE status='dead' AND created_at > 7d ago`：给一次重试机会，attempts 重置
3. 扫 `files WHERE ingest_status IN ('processing','failed') AND ingested_at IS NULL` 但对应 ingest_file 任务已不存在/dead：重新入队 ingest_file
4. 扫 `sessions WHERE ended_at IS NULL AND last_audit_event_age > 24h`：标 `end_reason='unclean'`

#### `normalize_tags`（🏛️ 整理卡片目录）
- LLM 任务，不是规则匹配
- 流程：取 facet 内 tag 列表（含 doc_count），分批喂 LLM "判断哪些同义需要合并" → 产出 `[{canonical, merge_in: [...]}]` → 应用合并：tags.alias_of 指向 canonical / 写 tag_aliases 历史 / entry_tags 重写指向 canonical
- 副产物：tag-tag 共现按需在任务内 ad-hoc SQL 计算（不维护 cooccurrence 表）
- 处理 `extra` facet 跨 facet 迁移
- 提议引入新 facet（极罕见）→ 写一条 journal 提醒开发者

#### `enrich_tags`（🏛️ 用新词表回填老书）
- LLM 任务
- 输入：lifecycle ∈ ('active', 'manual_active') 的 entry，最近 N 天没被 enrich 过
- 流程：拿当前规范 tag 词表（按 facet 分组）+ entry 的 description + 现有 tags → LLM 严格从词表选新增 tag
- 产出：INSERT entry_tags（source='enrich_tags'）；不 DELETE
- 不动 summary/description/extra

#### `restructure_catalogs`（🏛️ 重整书架分类）
- LLM 任务
- 输入：当前 catalog 树 + 最近 journal（包含 hint:restructure_catalogs 的）+ 高 doc_count 的 active entry 集合
- 流程：LLM 提议"哪些节点该合并/拆分/重命名"+"哪些 entry 该移到不同节点"
- 产出：UPDATE catalogs.{name, parent_id, summary, description, tags, extra} / UPDATE file_entries.catalog_id
- 软删旧节点（catalogs.deleted_at）而非硬删

#### `suggest_demotion` / `suggest_archival`（🏛️ 架位调整）
- 基于活跃度信号（最近 N 天未被 conversation 引用、对应 file 未被 read_files 工具读、entry_tags 稀疏度等）
- demotion：active → demoted；archival：demoted → archived
- 跳过 manual_* 状态的 entry（用户已锁定）

#### `purge_deleted_files`（🏛️ 兑现用户销毁意愿）
- 周期每天扫 `file_entries WHERE deleted_at IS NOT NULL AND purge_after < now`
- 对每个：DELETE entry → 检查同 file_id 是否还有活跃 entry → 没有就 DELETE file 行 + 删 storage 对象
- 一个事务内完成 entry 软删 + 孤儿 file 清理

#### `prune_audit_events`（🏛️ 覆盖旧监控录像）
- 每天跑：`DELETE FROM audit_events WHERE occurred_at < now - AUDIT_RETENTION_DAYS`
- 分批删避免长事务

---

## 10. Agent 工具集

12 个工具，全部由 🔍 调查员（在线 agent）使用。每个工具都对应一个"对外动作"——访问 DB / 存储 / 临时计算引擎，agent 自己组合判断。

### 10.1 工具清单

#### 起步层

| 工具 | 签名 | 作用 |
|---|---|---|
| `search_journal` | `(text?, entry_id?, tags?, since?, limit, order)` | 翻自己的笔记本——找过去对话怎么走的 / 哪些笔记涉及这个 entry |

#### 结构层（catalog/view/tag）

| 工具 | 签名 | 作用 |
|---|---|---|
| `list_catalogs` | `(parent_id?)` | 下钻 catalog 树 |
| `read_catalog` | `(id)` | 读 catalog 节点完整 metadata：summary/description/extra/tags/直接子节点/entry 计数 |
| `materialize_view` | `(id, limit)` | 跑 view filter 拿到当前命中 entry 清单 |
| `resolve_tag` | `(name)` | 任意写法（含同义词、缩写）→ 规范 tag id |

#### 搜索层

| 工具 | 签名 | 作用 |
|---|---|---|
| `search_metadata` | `(text?, tags_all?, tags_any?, tags_none?, catalog_id?, catalog_subtree?, view_id?, kind?, lifecycle?, include_container_paths?, limit)` | 缩小候选 entry 范围。text 走 ILIKE on summary+extra；catalog_id 精确单节点 vs catalog_subtree 递归（互斥）；include_container_paths 扫描容器内部文件路径 |

#### 用户层（agent 读用户文件夹作为先验提示）

| 工具 | 签名 | 作用 |
|---|---|---|
| `list_folders` | `(parent_id?)` | 用户文件夹树 |
| `list_files_in_folder` | `(folder_id, limit)` | 文件夹内 entry 清单 |

#### 文件内容层

| 工具 | 签名 | 作用 |
|---|---|---|
| `read_entries_metadata` | `(entry_ids, related_limit=10)` | 批量读 entry 完整 metadata + 自动附 `related_entries`（来自 entry_relations，默认 top-10 by observation_count） |
| `read_files` | `(requests)` | 批量读 file 原文。每 request: `{entry_id, locations[], search?}`。同一文件多次出现 → 单次 storage 打开。Locations 支持 unit=section / pages / lines / bytes / heading。search 用内存匹配，无索引 |

#### 结构化数据层（DuckDB 临时计算）

| 工具 | 签名 | 作用 |
|---|---|---|
| `query_table` | `(entry_id, sql, chart_hint?, chart_spec?)` | 打开 in-memory DuckDB，read_csv_auto/read_xlsx 原文件，跑 SQL 白名单（SELECT/SHOW/DESCRIBE/EXPLAIN/PRAGMA），关闭。Schema 在工具描述里 bind-time 注入。chart 参数产出 Vega-Lite spec（单向给用户） |
| `query_log` | `(entry_id, sql, chart_hint?, chart_spec?)` | 同 query_table，但格式由 ingest 时识别记录的 description.format 决定（json_lines / nginx / syslog / 自定义 regex） |

#### 容器层

| 工具 | 签名 | 作用 |
|---|---|---|
| `analyze_container` | `(container_entry_id, list_files?, read_files?, search?)` | 容器（git_repo / archive）内部按需探索：临时解压 + 列文件 / 读内部文件 / 内部 grep。一次调用共享一次解压。引用使用 `container_path` 角标 |

### 10.2 框架自动行为（不是工具）

#### 稳定层注入（提示词缓存核心）

每次 agent LLM 调用的系统提示前缀包含：

```
[系统提示]
[工具定义]
[catalog 一级节点：id + name + summary + entry 数] × N
[全部 view 列表：id + name + summary] × M
[tag 词表快照：按 facet 分组、按频次截断 top-K]
[最近 20 条 journal 简介：id + note 摘要 + entry_ids 数 + tags]
```

这部分由离线任务（normalize_tags 完成后）生成快照、整体替换。在线对话期间永不修改——保证 agent 多轮对话的前缀缓存命中率最大化。

#### 每轮末尾自动追加 budget

每轮 tool_results 之后，框架自动追加：

```
[turn N tail]
本轮已用：12 次工具调用 / 估算 18000 token / 剩余预算 60%
（不需要主动调工具查询；自动每轮追加）
```

agent 无 `report_budget` 工具——budget 由框架注入。

#### 引用自动收集

agent 在最终 markdown 答案里使用角标格式：

```markdown
扩散模型的训练目标是预测噪声 [^a]，而不是直接预测原图 [^b]。

[^a]: entry_id=E123, section_id=s3
[^b]: entry_id=E456, section_id=s7
```

框架在 conversation 终止后扫 markdown 提取角标，写入 `conversations.tool_calls` 的 citation 部分。agent 无 `mark_finding` 工具——引用是写作动作的副产品。

#### 终止检测

agent 最后一轮无 tool_calls = 自然终止：
- 把最终 agent_response 写入 conversation 行
- 提取角标 citations
- 入队 `reflect_turn` 任务（priority 30）

agent 无 `commit_answer` 工具——终止是状态而非动作。

#### plan-execute 分阶段

每个新 user_message 进入 conversation：

**Plan 阶段**（一次 LLM 调用，**`tools=[]`**）：
- 输入：系统提示 + 稳定层 + 用户问题
- 输出：plan 文本（步骤、预期产出、退出条件、预算估算）
- 这次 LLM 调用**不能调任何工具**——纯文本规划

**Execute 阶段**（多轮 LLM 调用，**完整 tool 绑定**）：
- 输入：稳定层 + 问题 + plan + 历次 tool 调用历史
- 每轮：LLM 决定调工具或给最终答案
- plan 作为约束嵌入提示词：偏离 plan 在 reasoning 中说明，不重新生成 plan（保持缓存）

#### 图表单向产出

`query_table` / `query_log` 接受可选 `chart_hint` / `chart_spec` 参数：
- agent 决定"这段说明配图更清楚"时传 chart 参数
- 后端拼出 Vega-Lite spec 返回（数据 + spec 一起）
- 图表 spec 存进 conversation.tool_calls 的工具结果，渲染时给用户看
- **图永不回流给 agent**——agent 不"看图"，只在生成最终答案时引用图（markdown 角标语法）

---

## 11. 文件 Ingest Pipeline

### 11.1 路由机制

`ingest_file` handler 按 `files.mime_type` + `files.original_ext` 查注册表，路由到对应 pipeline。新增文件类型 = 注册新 pipeline，不改 dispatcher。

### 11.2 Pipeline 通用契约

每个 pipeline 接受：
- 该 entry 的 `file_id`
- 文件原文（pipeline 自己从 storage 取，按需解析）
- 该 entry 的 folder 路径 + 同 folder 兄弟 display_names + 当前 catalog 一级 + 当前 tag 词表（作为提示）

产出（一次 LLM 调用）：
- `files.summary`（content-only，write-once）
- `files.description`（content-only，结构化 JSON，write-once）
- `files.kind`（独立列）
- `files.extra`（content-level 洞察，write-once）
- 该 entry 的 `catalog_id`（候选位置，可被后续 restructure 调整）
- 该 entry 的 `entry_tags`（按词表选；新概念可创新 tag）
- 该 entry 的 `extra`（位置感知洞察）

### 11.3 V1 Pipeline 优先级

**第一批（必须）**

| Pipeline | 触发 mime/ext | 描述形态 |
|---|---|---|
| `text_pipeline` | `text/markdown`, `text/plain`, `.txt` `.md` `.rst` | description.sections（heading-path / line-range anchors） |
| `code_pipeline` | `text/x-python`, `application/javascript`, `.py` `.ts` `.go` `.rs` 等 | description.symbols（class/function 列表 + 行号） |
| `pdf_pipeline` | `application/pdf` | description.sections（per-page anchors）；扫描件回退 OCR |
| `docx_pipeline` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | description.sections（heading-path） |
| `tabular_pipeline` | csv / xlsx / parquet / sqlite | description.columns + samples + row_count_approx；不预 ingest 到 DuckDB |
| `image_pipeline` | `image/*` | description.caption + ocr_text + elements（VLM 产出） |

**第二批**

| Pipeline | 描述 |
|---|---|
| `pptx_pipeline` | description.slides（slide-number anchors） |
| `ebook_pipeline` | description.chapters |
| `git_pipeline` / `archive_pipeline` | 容器形态（详见 11.4） |
| log（作为 tabular 的子情况） | description.format（json_lines / nginx / syslog / 自定义 regex） |

**第三批**

| Pipeline | 描述 |
|---|---|
| `audio_pipeline` | description.transcript + timestamps（Whisper） |
| `video_pipeline` | description.frames（关键帧 VLM）+ description.audio_transcript |
| `mailbox_pipeline` | description.threads（mbox 拆每封邮件） |

### 11.4 容器 Pipeline（A+ 方案）

**关键决策：** git 仓库 / 压缩包作为单个 file 行处理（NOT 拆成多个 leaf file）。Schema 零改动。

容器的 `description` 形态：

```jsonc
{
  "container_kind": "git_repo" | "zip_archive" | "tar_archive" | "mbox" | "notion_export",
  "file_count": 234,
  "total_uncompressed_bytes": 12345678,
  "primary_language": "python",                  // 仅 git_repo
  "frameworks_detected": ["FastAPI"],            // 仅 git_repo
  "tree": {                                      // 顶层目录概览（深度 2）
    "src/": {"file_count": 80, "kinds": ["code"]},
    "tests/": {"file_count": 40, "kinds": ["code"]}
  },
  "indexed_files": [                             // 完整内部清单（不做 LLM 处理）
    {"path": "src/auth/login.py", "size": 2048, "mime": "text/x-python"},
    {"path": "README.md", "size": 4096, "mime": "text/markdown"}
  ],
  "key_files": [                                 // 选中几个做轻量摘要
    {"path": "README.md", "summary": "FastAPI-based note system with SQLite backend"},
    {"path": "pyproject.toml", "summary": "deps: fastapi, sqlalchemy, alembic, pydantic"}
  ],
  "ingest_filters_applied": [".gitignore", "node_modules/", "*.lock"]
}
```

**git_pipeline 与 archive_pipeline 的差异：**
- `git_pipeline` 触发条件：含 `.git` / git URL / git zip。重点提取项目语义（README、manifests、入口、frameworks）
- `archive_pipeline` 触发条件：通用 zip / tar.gz 且非 git。重点提取内容性质（mime 分布、意图推断）

**ingest 阶段的容器处理流程：**
1. 流式下载 zip 到临时目录
2. 应用 `.gitignore` + 内置 ignore（`node_modules/`, `.venv/`, `dist/`, `*.lock`, 大型二进制）+ 安全限制（路径穿越、单文件大小、总解压大小、压缩比）
3. 枚举保留下来的文件 → `description.indexed_files`
4. 选取 key_files 做 1-2 句摘要
5. ONE LLM 调用产出 summary / description / tags / extra
6. **不为内部每个文件做 LLM 处理**——内部内容由 agent 时刻通过 `analyze_container` 工具临时探索

### 11.5 不同 kind 的 description JSON 形态

```jsonc
// kind='text'
{
  "sections": [
    {"id": "s1", "title": "...", "anchor": {"unit": "heading", "path": "1.2"}, "summary": "...", "key_terms": [...]}
  ]
}

// kind='table'
{
  "columns": [
    {"name": "amount", "type": "numeric", "semantic": "transaction value", "samples": [123, 456, 789]}
  ],
  "row_count_approx": 12000
}

// kind='log'（tabular 子情况）
{
  "format": "json_lines" | "nginx_combined" | "syslog" | "custom_regex:...",
  "fields": [...],
  "time_range": [start, end],
  "level_distribution": {"error": 12, "warn": 48, "info": 1024}
}

// kind='image'
{
  "caption": "...",
  "ocr_text": "...",
  "elements": ["chart", "logo", "person"]
}

// kind='code'
{
  "language": "python",
  "symbols": [{"name": "User", "kind": "class", "line": 42}],
  "imports": [...]
}
```

---

## 12. 关键流程

### 12.1 用户上传

```
[👤 用户] POST /folders/{folder_id}/upload (file)
   │
   ├─ 流式 sha256 + 写 storage（如果新内容）
   ├─ 查 files.sha256
   │
   ├─ sha256 命中（同内容已存在）：
   │   ├─ 复用 files 行（不写 storage、不改 summary/description）
   │   ├─ 找任一现有 file_entry（同 file_id）作为种子 entry
   │   ├─ INSERT file_entries 新行：
   │   │   ├─ folder_id, file_id, display_name (重名自动 (1)/(2)/...)
   │   │   ├─ lifecycle = 'active'
   │   │   ├─ catalog_id 拷贝自种子
   │   │   ├─ extra 拷贝自种子
   │   │   └─ INSERT entry_tags 拷贝种子的 tag 集合（source='dedup_seed'）
   │   ├─ ❌ 不拷贝 entry_relations（关联是观察记录，不是属性）
   │   └─ ❌ 不入队任何 ingest 任务
   │
   └─ sha256 未命中（新内容）：
       ├─ INSERT files 行（summary/description/extra/kind 暂空，ingest_status='pending'）
       ├─ INSERT file_entries 新行（AI 字段全空）
       └─ 入队 ingest_file 任务（priority 50）
           handler 完成后：
           ├─ 写 files.summary / description / kind / extra（write-once）
           ├─ 设 files.ingested_at + ingest_status='done'
           └─ 写该 entry 的 catalog_id / extra / entry_tags
```

**[共享基础设施]** 每一步写入触发对应 audit_event：`file_created` / `entry_created` / `ingest_status_changed` / `task_started` / `task_finished`。

### 12.2 Agent 在线推理（plan-execute）

```
[👤 用户] POST /sessions/{session_id}/turn (user_message)
   │
   └─ [共享基础设施] 创建 conversation 行（user_message 写入）
       │
       ├─ [🔍 调查员 Plan 阶段] 一次 LLM 调用
       │   ├─ 输入：系统提示 + 稳定层快照 + 用户问题
       │   ├─ 工具：tools=[]（绝对禁止调工具）
       │   └─ 输出：plan 文本（步骤、预期产出、退出条件、预算）
       │       audit_event(kind='llm_call', payload={phase='plan', tokens, ...})
       │
       └─ [🔍 调查员 Execute 阶段] 多轮 LLM 调用
           ├─ 系统提示注入 plan 作为约束
           ├─ 每轮：
           │   ├─ LLM 调用（带工具）
           │   │   增量更新 conversation.total_input_tokens/output_tokens 等
           │   ├─ 调工具（read_entries_metadata / read_files / search_journal / ...）
           │   │   conversation.tool_calls JSON append
           │   └─ 框架追加 [budget tail]
           ├─ 终止检测：最后一轮无 tool_call → agent 给出 markdown 答案
           ├─ 写 agent_response 到 conversation 行
           ├─ 扫 markdown 角标提取 citations → conversation.tool_calls.citations
           └─ 入队 reflect_turn 任务（priority 30）
```

### 12.3 反思（reflect_turn）

**身份：[🔍 调查员→🏛️ 图书馆员]** —— 调查员动作但产物服务于图书馆员

```
[🔍 调查员] reflect_turn handler
   │
   ├─ 输入：conversation_id
   ├─ 拉取 conversation 完整事实记录（user_message / agent_response / tool_calls / llm_calls）
   ├─ 拉取涉及 entry 的当前 metadata（含 summary / description / 已有 tags / 已有 entry_relations）
   │
   ├─ 强模型 + 长上下文 LLM 调用："根据这次对话学到了什么？"
   │
   └─ 产出（每项独立判断要不要写）：
       ├─ INSERT 0-N 条 journal（note + entry_ids + tags + source_kind='reflect_turn'）
       ├─ 0-N 条 entry_relations 操作：
       │   ├─ 新对（INSERT）：entry_a < entry_b, note, observation_count=1
       │   └─ 已有对（UPDATE）：observation_count += 1, last_observed_at, 可能 append note
       ├─ INSERT 0-N 条 entry_tags（source='reflect'）
       ├─ UPDATE 0-N 条 file_entries.extra（覆盖式刷新当前累积理解）
       ├─ UPDATE 0-N 条 catalogs.extra / views.extra（仅当对话触及它们时）
       └─ 可能给后续离线任务留 hint（写 journal 标 tags=['hint:restructure_catalogs'] 等）
```

**严格不动：** file.summary / file.description / file.extra（write-once）

**为什么 reflect 能更新 file_entries.extra 但不能更新 file.extra：**
- `file.extra` 描述内容本身（write-once，sha256 相同的内容描述始终一致）
- `file_entries.extra` 是 per-position 的累积洞察（mutable，反映当前对该 entry 在该 folder 上下文的理解）
- `journal` 是历史快照，extra 是当前状态——reflect 综合若干 journal 后可以决定刷新 extra
- 没有 reflect 写 file_entries.extra 的机制，那个字段就只有 ingest 时被写一次，永远不更新——违反"当前累积理解"语义

### 12.4 离线批处理周期

```
[共享基础设施] periodic_tick 每 10 分钟跑一次
   │
   ├─ 查 PERIODIC_INTERVALS 表
   ├─ 对每个 kind 查"最近一次 done 是什么时候"
   ├─ 超过间隔的 kind → enqueue 一次（dedup_key 防重入）
   └─ 入队顺序受 priority 控制：
       recover_stuck_tasks (priority 100, 每 10min) 总是先跑
       ↓
       purge_deleted_files (150) 每天
       ↓
       normalize_tags (200) 每 6h     ← 词表收敛
       ↓
       enrich_tags (215) 每 5d        ← 词表回填
       ↓
       restructure_catalogs (220) 每 7d
       ↓
       suggest_demotion (240) 每 7d   ← 生命周期判断最后做
       ↓
       suggest_archival (250) 每 14d
       ↓
       prune_audit_events (260) 每天
```

**为什么生命周期判断最后做：** AI 不该在自己还没看清楚一个 entry 的时候就把它打入冷宫。要等质量层（normalize/enrich）和结构层（restructure）跑过，再判断"什么不重要"。

---

## 13. Provenance：来源记录是一等公民

图书馆学传统视 provenance（来源记录）为编目的核心维度——一份资料从哪来、经过谁的手、何时被修改。Marginalia 在 schema 层面把这一点做成一等公民：

### 13.1 Provenance 字段

| 表 | 字段 | 记录什么 |
|---|---|---|
| `entry_tags` | `source` | 这个 tag 是怎么挂上去的（ingest / reflect / enrich_tags / dedup_seed） |
| `entry_relations` | `source_kind` | 这个关系是怎么观察到的（reflect / user） |
| `journal` | `source_kind` | 这条笔记来自什么动作（reflect_turn） |
| `tag_aliases` | `note` | 合并理由的自然语言说明 |
| 所有数据库变化 | `audit_events` | 完整事件流，按 session/conversation/task 维度可追溯 |

### 13.2 Agent 利用 provenance

Agent 通过工具看到的 metadata 都带 source：

- 看到 `entry_tags.source='ingest'` 知道这是 ingest 时打的初值，可信度看 ingest 模型
- 看到 `entry_tags.source='dedup_seed'` 知道这是从其他位置拷贝的种子，可能不完全适配当前位置
- 看到 `entry_tags.source='reflect'` 知道这是反思中观察到的，证据更强

这让 agent 在矛盾的元数据之间能作出加权判断——比规则化的"绝对真相"更接近真实图书馆员的工作方式。

### 13.3 Audit_events 是终极 provenance

任何字段的当前值之外，还能通过 audit_events 重建**任何历史时刻的状态**（90 天内）。这对调试、审计、理解 AI 决策路径至关重要。

---

## 14. 不变量与边界规则

汇总所有"绝对不能违反"的设计契约。

### 14.1 写权限边界

| 主体 | 可写字段 |
|---|---|
| 👤 **用户** | folders 全字段；file_entries（folder_id / display_name / lifecycle 部分转换 / 软删字段）；上传新 file 的物理字段 |
| 🏛️ **图书馆员**（离线 LLM 任务） | files 内容描述字段（summary / description / extra / kind） — write-once；file_entries.catalog_id；catalogs / views / tags / tag_aliases / entry_tags |
| 🔍 **调查员**（在线 agent 反思） | journal / entry_relations / entry_tags（reflect 增补）/ file_entries.extra / catalogs.extra / views.extra |
| [共享基础设施] | audit_events / sessions / conversations / tasks |

### 14.2 不可违反的写规则

1. **`files.summary` / `files.description` / `files.extra` / `files.kind` 是 write-once**——只在 `ingested_at IS NULL` 时可写一次，写完设 `ingested_at` 永久锁定。Service 层 enforce：UPDATE 路径检查 `ingested_at`，非 NULL 拒绝写入。

2. **AI 永不删除任何文件、entry、journal、entry_relations 行**——只能调 lifecycle / observation_count 增量等。删除是用户专属权力（软删 → purge_deleted_files 兑现）。

3. **`audit_events` 只 INSERT 不 UPDATE**——事实流，写一次永不修改。`prune_audit_events` 是唯一删除路径（按 occurred_at 时间窗口）。

3a. **`task_outcomes` 只 INSERT 不 UPDATE**——调度面事实表，写一次永不修改。`prune_task_outcomes` 是唯一删除路径（按 completed_at 时间窗口）。

4. **`conversations.session_id` 必须存在**——所有对话必属于某 session。

5. **`entry_relations` 永远对称对（a < b）**——构造函数时由 service 层 enforce id 大小关系。

6. **每个数据库变化必须同事务写入对应 `audit_events`**——保证审计完整性，由统一的 `audit.write()` 函数封装。

### 14.3 不可违反的读规则

1. **Agent 完全不读 audit_events / sessions / conversations**——这些是审计层；agent 的"过去经验"通过 `search_journal` 工具获取。

2. **离线任务（图书馆员）也不读 audit_events**——audit 是给人类审计看的事件流，业务逻辑（包括调度判定 / 幂等检查 / 最近性筛选）必须读真实数据或专门的调度记录表（`task_outcomes`，见 §8.4）。这样 audit 始终保持"事件流"语义，不被各种调度查询污染。

3. **用户完全不读 AI-internal 表**——catalogs / views / tags / journal / entry_relations / entry_tags / tag_aliases 不暴露给用户层 API。用户只看到自己的 folders / file_entries / files。

4. **AI 字段不能在用户视角"隔层暴露"**——比如不能在 `GET /file-entries/{id}` 返回 `catalog` / `tags` 字段。如果未来要给用户看 AI 标注，那是新功能（"图书馆员的标注卡"），需要单独设计。

### 14.4 涌现 / 累积语义

1. **`extra` 字段是 entity 当前累积理解**（mutable，覆盖式更新）；**`journal` 是历史笔记**（append-only，按对话累积）。两者协作：
   - journal 是单次 reflect 的快照——"那次对话学到了什么"
   - extra 是当前状态——综合若干 journal 后 reflect 决定要不要刷新
   - reflect_turn 可以同时写 journal（历史）和 UPDATE entity.extra（当前）
   - **唯一例外：** `files.extra` 是 write-once（描述内容本身，不随对话演化）；`file_entries.extra` / `catalogs.extra` / `views.extra` 都是 mutable

2. **`tag_aliases` 永不删除**——历史合并是事实，不可销毁。

3. **`tags.alias_of` 形成单向映射图**——alias_of 必须指向 alias_of IS NULL 的规范 tag（不能链式指向）。normalize 任务保证。

4. **lifecycle 状态机**：
   - `active → demoted → archived` 单向自动迁移（`suggest_demotion` / `suggest_archival`）
   - `manual_active` / `manual_archived` 是用户锁定，系统状态机不动
   - 任何 lifecycle 内 → manual_*：用户操作触发
   - manual_* → 任何 lifecycle 内：用户操作触发
   - 永远 NO 自动恢复 active → 升级只能由"使用"触发（实际查询 / 编辑）

### 14.5 容器边界

- 容器（git_repo / archive）作为单个 file 处理，**不创建内部 leaf file 行**
- 容器内部内容仅在 agent 调用 `analyze_container` 时临时解压
- 引用容器内部用 `[^a]: entry_id=<container>, container_path=src/auth/login.py, lines=42-58` 角标
- 容器内部的 file_relations / 内部 tag 由 reflect 写到 journal，不进 entry_relations 表（关系表只在 entry 之间）

---

## 15. 快速入门

### 15.1 安装与启动

```bash
# 克隆代码
git clone <repo>
cd marginalia

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env
# 编辑 .env，至少设置：
# - DB_BACKEND=sqlite (或 postgres)
# - STORAGE_BACKEND=local (或 s3)
# - OPENAI_API_BASE=https://api.deepseek.com/v1 (示例)
# - OPENAI_API_KEY=sk-...
# - DEFAULT_MODEL=deepseek-v4-flash

# 初始化数据库
alembic upgrade head

# 启动后端
uvicorn marginalia.main:app --reload

# 在另一个终端用 CLI
marg upload ~/papers/some.pdf
marg chat
```

### 15.2 第一次使用流程

1. **建几个文件夹**：`marg mkdir papers` / `marg mkdir notes`
2. **上传一些文件**：`marg upload ~/Downloads/some.pdf -t papers`
3. **等待 ingest 完成**：`marg tasks` 看 `ingest_file` 状态变 `done`
4. **问问题**：`marg ask "刚上传的那篇 PDF 讲了什么？"`
5. **进入对话模式**：`marg chat`，多轮 follow-up

### 15.3 常见问题

**Q：上传的文件去哪儿了？**
A：物理存储在 `data/objects/` 下（按 sha256 前缀分片）。用户层文件夹只是引用，物理文件零拷贝去重。

**Q：AI 怎么知道我之前问过什么？**
A：每次对话结束后 reflect_turn 任务会把"今天学到了什么"写到 journal 笔记本里。下次对话开始 agent 会先翻笔记。

**Q：能用本地 LLM 吗（Ollama 等）？**
A：可以——只要是 OpenAI 兼容 API。设 `OPENAI_API_BASE=http://localhost:11434/v1`。

**Q：怎么把 agent 的答案分享给同事？**
A：CLI 提供导出命令——`marg export <conversation_id> -o answer.md` 导出单文件 markdown（含引用角标）；`marg export <conversation_id> --bundle -o report.zip` 打包成含引用原文片段的 zip。后者适合分享给不能访问 Marginalia 实例的同事——研究报告 + 证据片段一起带走。详见 §16.5。

**Q：数据备份怎么做？**
A：V1 备份策略是用户自己拷贝 `data/` 目录（含 SQLite 数据库 + 物理文件存储）。Postgres 后端用 `pg_dump`。

---

## 16. V1 范围与延后项

### 16.1 V1 必做清单

**Pipeline（按优先级）**
- 第一批：text / code / pdf / docx / tabular / image
- 第二批：pptx / ebook / git_repo / archive / log（作为 tabular 子）
- 第三批：audio / video / mailbox

**核心模块**
- 用户层 API：folders CRUD / file_entries CRUD / 上传 / 下载 / 归档 / 软删
- ingest pipeline 注册表 + 第一批 pipeline 实施
- agent runtime（plan-execute + 12 工具）
- reflect_turn handler
- 离线 handler：normalize_tags / enrich_tags / restructure_catalogs / suggest_demotion / suggest_archival / recover_stuck_tasks / purge_deleted_files / prune_audit_events
- audit 模块（统一 audit_events 写入 + 累计指标维护）
- EntryTagsService（统一 entry_tags 写入路径）
- 上传 service 增加 dedup 拷贝种子逻辑
- **导出模块**（markdown 单文件 + zip bundle，详见 §16.5）
- CLI 客户端（基础命令集）
- 双后端实测（SQLite + 本地、Postgres + S3/MinIO）

### 16.2 Tier B 实施期可定的细节

| 编号 | 议题 | 备注 |
|---|---|---|
| B.1 | PDF OCR 选型 | tesseract / paddleocr / 云端 |
| B.2 | 探索预算实现 | 单位 / 超限行为 / 初始预算来源 |
| B.3 | DuckDB 资源管理 | 内存上限 / 超时 / 临时文件缓存 |
| B.4 | analyze_container 是否解压缓存 | V1 可不缓存 |
| B.6 | Vega-Lite chart_spec sanitize | 拒绝远程 data url / 外部 transform / expression |
| B.7 | 时区约定 | 所有判断按 UTC，显示由前端处理 |
| B.9 | 对话失败的处理 | 中途崩溃如何收尾、能否恢复 |
| B.10 | 备份 / 恢复 | V1 用户自己拷 data 目录 |
| B.11 | audit payload 大小 | 摘要 + hash，不存全文 |

### 16.3 不为企业准备的明确边界

V1 以及可见的未来都是单用户个人 KMS：

- 无 owner_id / organization_id 字段
- 无鉴权（API 默认 localhost）
- 无审计权限隔离
- 无 FTS / embedding（哲学拒绝）
- 无 SSO / 加密 / 计费 / 监控 / 备份增量
- 不预留多租户口子

如果将来真要做企业版，应**基于已运行成熟的个人版起新子项目**，而不是改造当前架构。这是"先做透一种再说另一种"的产品策略。

### 16.4 License

**AGPL-3.0**

理由：
- 强 copyleft 保护项目不被云厂商私改后做托管 SaaS 套利——任何运行 Marginalia 提供网络服务的人必须公开源码
- 个人单用户使用零障碍（AGPL 仅在网络服务场景生效）
- 与 Marginalia"不为企业准备"哲学契合——如果未来有人想做企业版托管，必须以同样开源条款回馈
- 同类自托管 KMS 系统（Logseq 等）多采用 AGPL

### 16.5 导出模块

**身份**：[共享基础设施]——服务用户拿走 agent 的产出

调查员（agent）每次回答用户问题时已经在产出 markdown——含角标引用的完整答案。导出模块把这份产出加上引用上下文打包，供用户带走。

**两种导出形态：**

#### 形态 1：单文件 markdown

```bash
marg export <conversation_id> -o answer.md
```

产出 markdown 文件结构：

```markdown
# 用户的原问题

调查员的完整答案（含 [^a] [^b] 角标）

---

## 引用

- [^a] entry_id=E123, section_id=s3
  > 摘录 F1 第 3 节的相关原文（默认 500 字截断）

- [^b] entry_id=E456, pages=[12,14]
  > 摘录 F2 第 12-14 页的相关原文

---

## 元数据

- 对话时间：2024-XX-XX
- 对话 ID：<conversation_id>
- Token 用量：input=X, output=Y
```

适合粘贴到笔记软件 / 邮件 / 即时通讯 / 学术草稿。

#### 形态 2：zip bundle（含引用附件）

```bash
marg export <conversation_id> --bundle -o report.zip
```

产出 zip 包结构：

```
report.zip
├── answer.md              # 主答案（同形态 1 的内容）
├── citations/             # 引用原文附件
│   ├── E123-section-s3.txt    # F1 完整章节原文
│   ├── E456-pages-12-14.pdf   # F2 相关页面切片（PDF kind 时）
│   └── ...
├── conversation.json      # 完整对话历史（可选 --include-trace）
└── metadata.json          # 时间戳、模型、token 用量、引用清单
```

适合长期归档、分享给不能访问 Marginalia 实例的他人、作为研究项目的"交付件"。

#### CLI 命令变体

```bash
marg ask "..." --export answer.md          # 一次性问答 + 直接导出
marg ask "..." --bundle report.zip         # 一次性问答 + 直接打包
marg export <conversation_id>              # 导出单轮答案到 stdout
marg export <conversation_id> -o file.md   # 保存到文件
marg export <conversation_id> --bundle     # 打包 zip
marg export <session_id> --all             # 整个 session 的所有对话
```

#### 引用片段的提取规则

按 `entry_id` + 引用类型不同：
- `section_id=s3`：从 file_entries 找 entry → file → 通过 description.sections 找到 anchor → 从 storage 读对应字节区间 → 文本截断 500 字（可配置）
- `pages=[12,14]`：从 PDF 提取这几页（PNG 图片或文本，看类型）
- `lines=[42,58]`：从源码读这几行
- `bytes=[100,500]`：直接读字节区间
- `container_path=src/auth/login.py`：从容器临时解压拿对应文件

引用提取**不修改**任何数据库——纯只读操作。

#### Schema 影响

**几乎没有**。现有 schema 已足够：
- `conversations.agent_response` 已有 markdown
- `conversations.tool_calls` 已含 citations
- 通过 `entry_id` → `file_entries` → `files` → `storage_key` 拿原文
- 引用片段**导出时临时生成**，不持久化

如果需要跟踪"谁导出了什么"，加 `audit_events.kind='exported'` 即可——不需要新表。

---

## 17. 未来路径

V1 之后可能的演进方向（不预先承诺，按真实需求驱动）：

### 17.1 浏览器 UI

CLI 之上加可选的浏览器 UI——文件夹拖拽、对话窗口、图表渲染。后端 API 已为此预留（OpenAPI），UI 完全独立开发。CLI 仍是核心客户端。

### 17.2 移动端

当用户长期积累后会想随时查阅。移动端可能只做"查"不做"管"——上传/整理仍在桌面 CLI 完成。

### 17.3 增强的导出形态

V1 已有 markdown / zip bundle 导出（§16.5）。未来可能扩展：
- 导出为静态 HTML 站点（多对话研究的合集展示）
- 导出为 PDF（学术写作场景）
- 共享只读链接（自部署场景下，把 bundle 上传到固定 URL，含到期回收机制）

### 17.4 更多 pipeline

按用户实际上传内容自然扩展：
- 网页书签 / `.url` / `.html`（抓正文）
- 笔记应用导出（Notion / Obsidian / Roam）
- 视频字幕 / 课程讲义
- 学术 BibTeX

### 17.5 离线模型支持深化

V1 已支持 OpenAI 兼容 API（含 Ollama）。未来可能针对纯本地部署优化：
- 量化模型选型推荐
- 离线 ASR / OCR / VLM
- 完全无外网依赖的部署模式

### 17.6 不会做什么

- ❌ **多租户 / SaaS 化** —— 见 16.3
- ❌ **向量检索** —— 哲学拒绝
- ❌ **AI 主动推送** —— 与"agent 仅在用户提问时活跃"哲学冲突
- ❌ **物理删除文件** —— "AI 永不删除"原则

---

## 18. 附录

### 18.1 身份归属总表

按身份归类所有动作，方便快速核查"某个新设计该归谁"：

#### 🏛️ 图书馆员（离线，整理藏书）

| 类型 | 动作 |
|---|---|
| 任务 | `ingest_file` / `normalize_tags` / `enrich_tags` / `restructure_catalogs` / `suggest_demotion` / `suggest_archival` / `recover_stuck_tasks` / `purge_deleted_files` / `prune_audit_events` |
| 写入 | files 内容描述字段（write-once）/ file_entries.catalog_id / catalogs / views / tags / tag_aliases / entry_tags 大部分操作 |

#### 🔍 调查员（在线，查阅资料）

| 类型 | 动作 |
|---|---|
| 流程 | plan 阶段 / execute 阶段 / 终止检测 |
| 工具 | 12 个工具全部由调查员调用 |
| 任务（产物） | `reflect_turn`（标 [🔍 调查员→🏛️ 图书馆员]，产物服务于离线整理） |
| 写入 | journal / entry_relations / entry_tags（reflect 增补）/ file_entries.extra / catalogs.extra / views.extra |

#### 👤 用户

| 类型 | 动作 |
|---|---|
| API | 上传 / 下载 / folders CRUD / file_entries lifecycle 切换 / 软删 / 恢复 |
| 写入 | folders 全字段 / file_entries 用户层字段 |

#### [共享基础设施]

| 类型 | 动作 |
|---|---|
| 容器 | sessions 边界 / conversations turn 记录 |
| 事件流 | audit_events 写入（任何身份的写入都触发） |
| 队列 | tasks 调度 / claim / heartbeat / retry |

### 18.2 关键数字速查

| 项目 | 数量 |
|---|---|
| 设计原则 | 13 条 |
| 业务表 | 14 张（+ alembic_version 元表 = 15） |
| 任务 kind | 10 个 + `periodic_tick` dispatcher |
| Agent 工具 | 12 个 |
| 数据架构层 | 4 层 |
| AI 内部回忆机制 | 3 个（entry_relations / journal / views） |
| Tag facet | 6 个（topic / form / time / source / language / extra） |
| Lifecycle 取值 | 5 个（active / demoted / archived / manual_active / manual_archived） |

### 18.3 字段维护责任速查（孤儿字段排查）

| 表 | 字段 | 写者 | 读者 |
|---|---|---|---|
| folders | id / parent_id / name / deleted_at / created_at / updated_at | 用户 API | agent (list_folders) + 上传 service + ingest（folder 路径作先验） |
| file_entries | id / folder_id / display_name / deleted_at / purge_after | 用户 API | agent (list_files_in_folder, read_entries_metadata) + purge_deleted_files |
| file_entries | lifecycle | 用户 API + suggest_demotion + suggest_archival | search_metadata + 离线 batch SQL filter |
| file_entries | catalog_id | ingest_file + restructure_catalogs + 上传 service (dedup) | search_metadata + read_entries_metadata |
| file_entries | extra | ingest_file + reflect_turn + 上传 service (dedup) | read_entries_metadata + search_metadata text 扫 |
| files | id / storage_key / sha256 / size_bytes / mime_type / original_ext | 上传 service | dedup 检查 / 存储读取 / pipeline 路由 |
| files | summary / description / extra / kind | ingest_file (write-once) | read_entries_metadata + search_metadata |
| files | ingest_status / ingested_at | ingest_file + recover_stuck_tasks | search_metadata 默认过滤 |
| files | deleted_at | purge_deleted_files | 物理删除标志 |
| audit_events | 全字段 | 所有数据库变化通过 audit.write() | 人类管理工具 + prune_audit_events |
| sessions | started_at / initiating_user_message | session 开始 | 人类管理工具 |
| sessions | ended_at / end_reason | session 结束 + recover_stuck_tasks (unclean) | 人类管理工具 |
| sessions | turn_count / total_* | 增量更新（audit + conversation 触发） | 人类管理工具 |
| conversations | session_id / turn_index / started_at / user_message | turn 开始 | 人类管理工具 |
| conversations | agent_response / ended_at | 终止检测 | reflect_turn |
| conversations | tool_calls / llm_calls | 实时 append | reflect_turn + 人类管理工具 |
| conversations | total_* | 增量更新 | 人类管理工具 |
| catalogs | id / parent_id / name / summary / description / tags | ingest_file 创建 + restructure_catalogs | agent (list_catalogs, read_catalog) + reflect 写 extra |
| catalogs | extra | reflect_turn + restructure_catalogs | agent (read_catalog) |
| catalogs | deleted_at | restructure_catalogs (合并节点) | 列出过滤 |
| views | id / name / summary / description / tags / filter_spec | reflect_turn + restructure_catalogs | agent (materialize_view) + reflect 写 extra |
| views | extra | reflect_turn + restructure_catalogs | agent |
| tags | id / name / facet | ingest_file 创建 | agent (resolve_tag, list_tags) + search_metadata |
| tags | alias_of | normalize_tags | resolve_tag 解析链 |
| tags | doc_count | normalize_tags 重算 | 词表快照截断 |
| tags | last_used_at | entry_tags 写入时更新 | 词表快照排序 |
| tag_aliases | from_name / to_tag_id / note | normalize_tags + reflect_turn | resolve_tag fallback |
| entry_tags | entry_id / tag_id / source | ingest / reflect / enrich_tags / dedup_seed / normalize 合并重写 | search_metadata + read_entries_metadata |
| entry_relations | 全字段 | reflect_turn | read_entries_metadata 自动附 related_entries |
| journal | 全字段 | reflect_turn (append-only) | search_journal |
| tasks | 全字段 | 创建（多处） + worker claim/heartbeat/done/fail + recover_stuck_tasks | TaskRunner.claim_batch + 调试 API |

**孤儿字段排查结论：** 14 张表所有字段都有明确的写者和读者，无孤儿字段。

---

**文档版本：** v1（设计审视完成、待进入实施）
