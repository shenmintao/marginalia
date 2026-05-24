# Marginalia

> English: [README.md](README.md)

一个受图书馆学启发的个人知识库系统。你上传文档，背后的图书馆员
（一个 LLM agent）默默给它们编目、关联、归类。需要查什么时，调查员
agent 会翻自己的笔记本（journal），整理上下文，给出带引用的回答。

## 为什么叫"图书馆学"

大多数"AI 搜本地文件"系统是 RAG 问答——AI 只是被动的检索消费者。
Marginalia 把 AI 当成图书馆员：分类树、tag、交叉引用、journal 都归
它管。文件本身保留你自己的文件夹结构；其他东西（catalog、tags、
relations、summary）属于 agent，由使用过程慢慢塑造成形。

## 5 分钟入门

```bash
# 1. 安装
python -m venv .venv
source .venv/Scripts/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. 初始化工作目录（在你想放笔记的地方）
mkdir my-library && cd my-library
marginalia init                           # 生成 .env / data/ / .marginalia/
# 编辑 .env 填 LLM_DEFAULT_API_KEY
alembic upgrade head

# 3. 打开 marginalia
marginalia
marginalia> /upload paper.pdf /
marginalia> 帮我对比 Raft 和 Paxos
```

`marginalia` 一个命令进程通吃——server、worker、CLI 都在同一进程
里，跟 Claude Code、DeepSeek TUI 同样的形态。不用开两个终端。

默认情况下你的文件以真实的文件夹树存在
`~/Marginalia/library/research/llm/paper.pdf`，可以在 Finder 里浏览、用
`rsync` 或 `git` 备份、用任何编辑器修改——这个文件夹**就是**你的库，
marginalia 只是给它建索引。在 marginalia 之外动过文件后，跑 `/check`
看差异、`/ingest --all` 同步 db 和磁盘。要把整套（db + library + 缓存）
搬到别处，设 `MARGINALIA_HOME=/some/path` 即可。

只有当你想多机共享同一个知识库（笔记本 + 台式机）时，才把 server
拆出来跑成独立进程，CLI 通过 `--server URL` 连过去。见下文"部署
形态"。

## CLI 长这样

`marginalia` 是 Claude Code 风格的 REPL。`/` 开头是 slash 命令，其他
内容直接转给 agent 当对话。

```
/help                                  列出所有命令
/upload <本地> <远端>                  从 vault 外把文件拷进 vault
/upload <本地> <远端> --name X         显式指定 display_name
/check                                  对比 vault 磁盘和 db 的差异（只读）
/ingest <vault_path>                    把 vault 内某个文件同步到 db
/ingest --all                           整个 vault 一次性同步（git add -A 风格）
/tree                                  文件夹树
/ls [parent_id]                        列子文件夹
/cd <path>                             切换"远端 cwd"，影响 /upload 的相对路径
/search <q>                            按文件名 + 摘要召回
/info <entry_id>                       用户可见 metadata + 一句话摘要
/download <entry_id|folder_id>         文件 → 字节流；文件夹 → zip
/export [<conv_id>]                    把对话 + 引用文件打包成 zip
/on-conflict rename|error|skip         切换重名策略
/clear / /new                          关闭 / 开启对话 session
/quit
```

跟 agent 对话时不是死等的 spinner——是带状态反馈的事件流：

```
marginalia> 帮我对比 Raft 和 Paxos
⠋ 制定调查计划...
⠋ 调用 search_journal(q="raft consensus")
⠋ 调用 read_files(entry_id=...)
⠋ 调查员思考中...
✓ 回答已就绪

# Raft vs Paxos
Raft 把 Paxos 拆成了三个相对独立的子问题...
[^a]: entry_id=...

  [tokens in=3300 out=340 tools=2 llm_calls=3 4521ms]
```

## 架构一句话概括

```
五层数据：
  audit_events            数据变化事件流（仅人类审计）
  sessions/conversations  容器 + 累计指标
  AI-internal             catalogs / tags / journal / entry_relations
  user-visible            folders / file_entries / files
  基础设施                tasks / task_outcomes
```

```
三个 LLM 角色：
  🔍 investigator  在线 agent — 翻 journal、调工具、回答
  🏛 librarian     离线 batch — ingest / normalize_tags / restructure...
  📋 reflector     每轮对话后 — 写 journal 给将来的自己用
```

```
12 个 task / 12 个 agent 工具 / 8 条 ingest pipeline
  text / pdf（含扫描版 PDF 走 VLM OCR）/ image（VLM 输入自动下采样）
  docx / spreadsheet / log（含 logrotate 变体）
  archive（zip / tar.* / 7z / rar / .gz / .bz2 / .xz / iso / cab，py7zz 50+ 格式）
```

完整设计见 [`design.md`](design.md)。架构概览随 samples 一起：
`samples/architecture.md`。

## API

REST 端点全部在 `/v1/` 前缀下：

```
POST /v1/upload                         上传文件
GET  /v1/folders                        文件夹树
GET  /v1/file-entries/{id}/...          单文件操作
GET  /v1/search                         元数据召回
POST /v1/sessions                       开 session
POST /v1/chat/{session_id}              聊天（SSE 流）
POST /v1/sessions/{id}/close            关 session
GET  /v1/conversations/{id}/export      导出对话 zip
GET  /health                            探活（不带 v1，监控惯例）
```

`POST /v1/chat/{session_id}` 返回 `text/event-stream`，事件类型：
`conversation` / `planning` / `plan` / `thinking` / `tool_call` /
`tool_result` / `answer` / `error` / `done`。这是 CLI 状态机渲染的
基础。

## 配置

所有设置走 `.env`。重点：

```ini
MARGINALIA_HOME=~/Marginalia     # 单一根目录；db + library + objects 都在这下面
DB_BACKEND=sqlite                # 或 postgres
SQLITE_PATH=                     # 留空 → <home>/marginalia.db

STORAGE_BACKEND=mirror           # 默认。用户可读的文件夹树
                                 # <home>/library/research/llm/paper.pdf
                                 # 配合 /check 和 /ingest 跟磁盘双向同步。
                                 # 备选：'local'（UUID 散列、开 dedup、高 churn
                                 # 场景下快约 5 倍），'s3'（多机部署）。
MIRROR_VAULT_ROOT=               # 留空 → <home>/library
LOCAL_STORAGE_ROOT=              # 留空 → <home>/objects（仅 local 用）

WORKER_ENABLED=true              # embedded 模式默认；TaskRunner 跑在 CLI/server 进程内

LLM_DEFAULT_PROVIDER=openai      # 或 anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
# 5 个 profile 的覆盖项（chat / reflect / ingest / vision / audio）：
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o

# 多机模式专用
MARGINALIA_SERVER=               # 不空 = remote 模式，跳过 embedded
```

OpenAI-compatible 端点（Together、Groq、DeepSeek、本地 vLLM / ollama）
通过 `LLM_*_BASE_URL` 支持。

## 部署形态

**默认（embedded）**：`marginalia` 一启动，FastAPI app + TaskRunner 都
在同一进程里。HTTP 不出进程，`httpx.ASGITransport` 直接调 ASGI 函数。
99% 的使用场景就是这样。

```
   ┌──────────────────────────────────────┐
   │  marginalia  (CLI + ASGI + worker)   │
   └──────────────────────────────────────┘
```

**多机共享**（可选）：把 server 拆成独立进程，CLI 通过 HTTP 连过去。
SQLite 同时只能被一个进程写——多机请用 Postgres。

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=true)
   └─────────────┘         └────────┬─────────┘
                                    │  共享 Postgres + 对象存储
                                    │
                            其他客户端连同一 server
```

启动远端 server：

```bash
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
# CLI 端：
marginalia --server http://server.lan:8000
# 或写到 ~/.marginalia/.env：
MARGINALIA_SERVER=http://server.lan:8000
```

## 开发

```bash
# 跑单个 e2e 测试
.venv/Scripts/python tests/test_agent_e2e.py

# 跑所有 e2e（30 个）
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

30 个 e2e 测试覆盖：upload / ingest / reflect / dispatcher / purge /
normalize_tags / enrich_tags / lifecycle / restructure / agent runtime /
agent tools / user mgmt / CLI / image pipeline / user files / export /
pdf / pdf-with-images / pdf-OCR / duckdb tools / worker daemon /
mine_corpus_evidence / mine_session_cooccurrence / propose_views /
refresh_entry_extra / container / git repo / compression / archive /
office (docx + spreadsheet) / cli upgrade（含 embedded 模式 smoke）。

## 状态

V1 端到端功能完整，但未在真实数据规模上压测。已知边界：

- 推荐式后台挖掘（共现 / 随机漫游）在下一 cycle 计划里 ——
  `mine_session_cooccurrence` 占位 task 已存在，但打分逻辑还很浅
- 没有语义 / embedding 检索。召回靠 name + summary + tags + FTS5。
  对个人库够用；如果你需要向量检索请另寻方案
- 音视频文件能接收但还没 pipeline，语音转写排在未来 cycle

## 许可证

Copyright (c) 2026 shenmintao

Marginalia 采用 GNU Affero General Public License v3.0 或更新版本
(AGPL-3.0-or-later) 授权。完整条款见 [LICENSE](LICENSE)。

如果你以网络服务形式运行 Marginalia 的修改版本，AGPL 要求你必须向
使用该服务的用户提供对应源码。
