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
cp .env.example .env                     # 然后编辑 LLM_DEFAULT_API_KEY
alembic upgrade head

# 2. 起 server + worker（生产用两个进程）
uvicorn marginalia.main:app               # 终端 1
marginalia-worker                         # 终端 2

# 3. 灌入示例数据
python samples/seed.py

# 4. 对话
marginalia
marginalia> /tree
marginalia> /search 共识
marginalia> 帮我对比 Raft 和 Paxos
```

## CLI 长这样

`marginalia` 是 Claude Code 风格的 REPL。`/` 开头是 slash 命令，其他
内容直接转给 agent 当对话。

```
/help                                  列出所有命令
/upload <本地> <远端>                  尾斜杠 = 文件夹；带扩展名 = 文件名
/upload <本地> <远端> --name X         显式指定 display_name
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
12 个 task / 12 个 agent 工具 / 3 条 ingest pipeline
```

完整设计见 [`design.md`](design.md)。架构概览随 samples 一起：
`samples/architecture.md`。

## 配置

所有设置走 `.env`。重点：

```ini
DB_BACKEND=sqlite                # 或 postgres
SQLITE_PATH=./data/marginalia.db

STORAGE_BACKEND=local            # 或 s3
LOCAL_STORAGE_ROOT=./data/objects

WORKER_ENABLED=false             # true = TaskRunner 跑在 API 进程内（开发）

LLM_DEFAULT_PROVIDER=openai      # 或 anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
# 5 个 profile 的覆盖项（chat / reflect / ingest / vision / audio）：
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o
```

OpenAI-compatible 端点（Together、Groq、DeepSeek、本地 vLLM / ollama）
通过 `LLM_*_BASE_URL` 支持。

## 部署形态

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=false)
   └─────────────┘         └────────┬─────────┘
                                    │  共享 DB + 对象存储
                                    │
                            ┌───────▼────────────┐
                            │ marginalia-worker  │  (TaskRunner)
                            └────────────────────┘
```

## 开发

```bash
# 跑单个 e2e 测试
.venv/Scripts/python tests/test_agent_e2e.py

# 跑所有 e2e
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

20 个 e2e 测试覆盖：upload / ingest / reflect / dispatcher / purge /
normalize_tags / enrich_tags / lifecycle / restructure / agent runtime
/ agent tools / user mgmt / CLI / image pipeline / user files / export
/ pdf / pdf-with-images / duckdb tools / worker daemon。

## 状态

V1 端到端功能完整，但未在真实数据规模上压测。已知边界：

- 扫描 PDF 标 `needs_ocr` 后跳过（OCR pipeline 待做）
- 容器文件（zip / tar / git repo）能接收但还没 pipeline，停在
  `ingest_status='pending'`
- 推荐式后台挖掘（共现 / 随机漫游）在下一 cycle 计划里

## 许可证

（在此插入 license 文件链接）
