# Marginalia

> English: [README.md](README.md)
> 设计文档:[DESIGN.md](DESIGN.md)

图书馆学风格的个人知识库系统。AI 在后台编目、归类、交叉引用;你提问,
调查员 agent 翻自己的笔记本(journal)、收集上下文,给出带引用的回答。

**不用向量库、不嵌入、不切块。** 检索靠结构化访问点(分类树 / tags /
views)+ metadata 搜索 + agent 直接读原文。LLM 提供语义理解,schema
负责账本。

## 它怎么工作

三种身份,严格分离:

- **🏛️ 图书馆员** —— 离线批处理。入册新文件、归并同义 tag、重整
  catalog。AI 内部状态绝大部分由它写。
- **🔍 调查员** —— 在线 agent。Plan → 工具调用 → 带引用的答案。每轮
  对话结束写 journal + 观察到的 entry 关联。
- **👤 你** —— 上传、整理文件夹、归档、删除。库是你的;AI 的工作产物
  独立存放。

调查员的笔记本是真的一张表(`journal`),图书馆员后续重整时会读它——
这个反馈回路就是库越用越懂你的机制。

文件按内容寻址(sha256)。每一处摆放(folder + display_name)各自有
独立的 AI 字段(catalog / extra / tags),所以同一份 PDF 在
`/工作` 和 `/研究` 下可以有完全不同的解读。

## 快速开始

```bash
python -m venv .venv
source .venv/Scripts/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

mkdir my-library && cd my-library
marginalia init                          # 生成 .env / data/ / .marginalia/
# 编辑 .env 填 LLM_DEFAULT_API_KEY
alembic upgrade head

marginalia
marginalia> /upload paper.pdf /
marginalia> 比较一下 raft 和 paxos
```

`marginalia` 命令是单进程——server / worker / CLI 全在里面,不需要开
第二个终端。

默认你的文件以真实文件夹形式存在 `~/Marginalia/library/...` 下。可以
在 Finder 里浏览、用 `rsync` / `git` 备份、用任何编辑器修改——库就是
你的文件夹,marginalia 只负责索引。在 marginalia 之外改了文件后,跑
`/check` 看 diff,`/ingest --all` 同步。

`MARGINALIA_HOME=/some/path` 把整个目录(db + library + cache)挪到
任意位置。

## 桌面应用

[Releases 页面](https://github.com/shenmintao/marginalia/releases) 提
供 Windows、macOS(Apple Silicon)和 Linux 的开箱即用桌面包。每个包
都内置了自己的 Python 运行时——无需系统 Python。

- **Windows**:`Marginalia_<version>_windows_x86_64-setup.exe`(NSIS
  安装版)或 `Marginalia_<version>_windows_x86_64_portable.zip`(解
  压即用绿色版)。需要预装 Microsoft Edge WebView2 Runtime(当前
  Windows 10 / 11 已经默认带了)。
- **macOS**:`Marginalia_<version>_aarch64.dmg`,仅支持 Apple Silicon。
- **Linux**:`.deb`、`.rpm` 或 `.AppImage`。

### 首次启动须知(未签名二进制)

由于没有 Apple Developer / Microsoft EV 证书,这些包都没有做代码签
名,第一次打开时系统会弹警告。点一下放行就好,以后再打开就不会再弹
了。

- **Windows SmartScreen** — 弹出"Windows 已保护你的电脑"。点
  **更多信息** → **仍要运行**。
- **macOS Gatekeeper** — 报"Marginalia.app 已损坏"或"无法验证开
  发者"。把 App 拖到 `/Applications` 之后,先跑一次:

  ```bash
  xattr -dr com.apple.quarantine /Applications/Marginalia.app
  ```

- **Linux AppImage** — 先加可执行权限:`chmod +x
  Marginalia*.AppImage`,之后双击或终端运行。

桌面应用默认把数据库、library、`.env` 都放在 `~/Marginalia/` 下。启
动前设置 `MARGINALIA_HOME` 可以挪到别的位置。

## CLI

`marginalia` 是 Claude-Code 风格的 REPL。`/` 开头是 slash 命令,其他
内容直接发给 agent。

```
/help                           列出所有命令
/upload <local> <remote>        从外部拷文件进库
/check                          对比磁盘和 db(只读)
/ingest <vault_path>            同步单个文件
/ingest --all                   同步整个库
/discover <entry_id> [N]        查看语料库为它链接到的 entry
/tree                           文件夹树
/ls [parent_id]                 列文件夹
/cd <path>                      切换"远端 cwd"(用于相对路径上传)
/search <query>                 按文件名 + summary 召回
/info <entry_id>                查看 entry 的用户可见 metadata + summary
/download <entry_id|folder_id>  文件 → 字节;文件夹 → zip
/export [<conv_id>]             把对话 + 引用打包成 zip
/on-conflict rename|error|skip  设置重名策略
/clear  /  /new                 结束 / 开始 chat session
/quit
```

一次对话 turn 渲染成事件流:

```
marginalia> 比较一下 raft 和 paxos
⠋ planning the investigation...
⠋ calling search_journal(q="raft consensus")
⠋ calling read_files(entry_id=...)
⠋ investigator thinking...
✓ answer ready

# Raft vs Paxos
Raft 把 Paxos 拆成三个相对独立的子问题……
[^a]: entry_id=...

  [tokens in=3300 out=340 tools=2 llm_calls=3 4521ms]
```

## 架构

**14 张表,4 层**:

```
audit_events                — 事件流(90 天滚动)
sessions / conversations    — 容器 + 累计指标
catalogs / views / tags /   — AI 内部:图书馆员的工作知识
  tag_aliases / entry_tags /  (用户看不到这层)
  entry_relations / journal
folders / file_entries /    — 用户可见
  files
tasks / task_outcomes       — 基础设施
```

**11 种任务,13 个 agent 工具,8 条 ingest pipeline**:

- text / pdf(含扫描件 OCR via VLM)/ image(VLM 缩放)
- docx / spreadsheet / log(含 logrotate 变种)
- archive(zip / tar.* / 7z / rar / .gz / .bz2 / .xz / iso / cab,50+ 种 via py7zz)

### Discovery(减少 agent 循环次数)

调查员一旦找到一个相关 entry,discovery 层立即把可能的邻居塞给它——
下一步不需要再烧一轮 search + read_files。三个 miner + 一个 LLM 关卡
喂养 `entry_relations`;random walk 服务消费 vetted 后的图;结果预填
进 search 和 metadata 响应。

```
mine_session_cooccurrence    journal 里 X 和 Y 在同一对话中被提及
mine_tag_overlap             Jaccard ≥ 0.30 且共享 ≥ 2 个 tag
mine_citation_graph          X 和 Y 在同一 agent 答案中被同时引用
                ↓
       entry_relations(原始,带 source_kind)
                ↓
   vet_relations              LLM 关卡,逐对判断 → vetted=True/False
                ↓
       entry_relations.vetted=True(干净的图)
                ↓
   services.recommend.find_related   带重启的 random walk,alpha=0.15
                ↓
   /discover <entry_id>            CLI 入口
   search/get_metadata.related_entries   预填 top-3 / top-8
```

Miner + vet 由 periodic dispatcher 驱动(默认每天;`/tend` 也会触发)。
Random walk 是查询时的只读操作。

完整设计见 [`DESIGN.md`](DESIGN.md)。

## API

业务 endpoint 全在 `/v1/`:

```
POST /v1/upload                        上传文件
GET  /v1/folders                       文件夹树
GET  /v1/file-entries/{id}/...         单文件操作
GET  /v1/search                        metadata 召回
POST /v1/sessions                      开 chat session
POST /v1/chat/{session_id}             chat(SSE 流)
POST /v1/sessions/{id}/close
GET  /v1/conversations/{id}/export     导出对话 zip
GET  /health                           liveness probe(无版本)
```

`POST /v1/chat/{session_id}` 返回 `text/event-stream`。事件:
`conversation` / `planning` / `plan` / `thinking` / `tool_call` /
`tool_result` / `answer` / `error` / `done`。CLI 状态机就是按这些事件
渲染的。

## 配置

`.env`:

```ini
MARGINALIA_HOME=~/Marginalia     # 一个根目录;db + library + objects 都在这下面
DB_BACKEND=sqlite                # 或 postgres

STORAGE_BACKEND=mirror           # 默认。文件以可读文件夹形式存:
                                 #   <home>/library/research/llm/paper.pdf
                                 # 备选:'local'(UUID 扁平,dedup,
                                 # 高频改写场景快约 5 倍)/ 's3'

WORKER_ENABLED=true              # embedded 模式默认开

LLM_DEFAULT_PROVIDER=openai      # 或 anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o

MARGINALIA_SERVER=               # 非空 = 远程模式,跳过 embedded
```

OpenAI 兼容 endpoint(Together / Groq / DeepSeek / 本地 vLLM / ollama)
通过 `LLM_*_BASE_URL` 切换。

## 部署形态

**默认(embedded)**:`marginalia` 在自己进程里挂 FastAPI + TaskRunner。
HTTP 不经过 socket——`httpx.ASGITransport` 直接调 ASGI app。99% 场景
应该用这个。

```
   ┌──────────────────────────────────────┐
   │  marginalia  (CLI + ASGI + worker)   │
   └──────────────────────────────────────┘
```

**多机部署**(可选):server 拆成独立进程,CLI 通过 HTTP 连。SQLite
同时只允许一个写进程——多机部署用 Postgres。

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=true)
   └─────────────┘         └────────┬─────────┘
                                    │  共享 Postgres + storage
```

```bash
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
marginalia --server http://server.lan:8000
# 或写入持久配置: MARGINALIA_SERVER=http://server.lan:8000 -> ~/.marginalia/.env
```

### Docker

`docker-compose.yml` 启动 api + worker + Postgres + MinIO:

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
marginalia --server http://localhost:8000
```

Compose 在 api 启动时跑 `alembic upgrade head`,通过一次性 init
容器创建 MinIO bucket。卷(`pgdata` / `miniodata` / `margdata`)
跨重启持久化。

## 开发

```bash
.venv/Scripts/python tests/test_agent_e2e.py
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

35 个 e2e 测试覆盖整个栈——upload / ingest / reflect / dispatcher /
purge / normalize_tags / enrich_tags / lifecycle / restructure /
agent runtime / agent tools / CLI / image / pdf / pdf-OCR / docx /
spreadsheet / container / git / archive pipeline / mirror 存储 /
scan + sync / discovery。

## 状态

v1:端到端可用,尚未对真实世界数据做硬化。

已知缺口:

- 没有语义 / embedding 检索。召回靠文件名 + summary + tags + ingest
  文本的 FTS5 + entry_relations 上的 random walk discovery。个人知识
  库够用;不是向量检索的替代品。
- 音视频文件能上传但没有 pipeline。语音转写是未来的工作。

## License

Copyright (c) 2026 shenmintao

AGPL-3.0-or-later。完整条款见 [LICENSE](LICENSE)。

如果你以网络服务的形式运行修改过的 Marginalia,AGPL 要求你向你的用户
公开对应源码。
