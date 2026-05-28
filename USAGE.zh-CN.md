# Marginalia 操作手册

> English: [USAGE.md](USAGE.md)
>
> 给已经会用终端的用户看。装好 → 跑通一次完整流程 → 知道出问题该看哪。
>
> 本手册不解释为什么这么设计——那是 [`DESIGN.md`](DESIGN.md) 的事。
> 命令清单和 CLI 参数在 [`README.zh-CN.md`](README.zh-CN.md)。

---

## 1. 安装

需要 Python 3.11+。

```bash
git clone <repo>
cd marginalia
python -m venv .venv
source .venv/Scripts/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

成功标志:`marginalia --help` 能跑出命令列表。

---

## 2. 初始化一个库

库 = 一个目录,装着 db + 你的文件 + 缓存。

```bash
mkdir my-library && cd my-library
marginalia init
```

`init` 在当前目录生成:
- `.env` —— 配置文件,需要手动填 API key
- `data/` —— SQLite 数据库会落在这
- `library/` —— 你上传的文件以可读文件夹形式存这
- `.marginalia/` —— 缓存

如果想把库放别处,设 `MARGINALIA_HOME`:

```bash
export MARGINALIA_HOME=/some/other/path
marginalia init
```

---

## 3. 配置 LLM(以 DeepSeek V4 Flash 为例)

打开 `.env`,改这几行:

```ini
LLM_DEFAULT_PROVIDER=openai-compatible
LLM_DEFAULT_BASE_URL=https://api.deepseek.com/v1
LLM_DEFAULT_API_KEY=sk-你的key
LLM_DEFAULT_MODEL=deepseek-v4-flash
```

DeepSeek 走的是 OpenAI 的协议格式,但不接受 OpenAI 的严格
`json_schema` response_format。所以 `provider` 要写成 `openai-compatible`
——adapter 会自动改用 `json_object`,把 schema 作为指令注入到 system
prompt。只有指向 OpenAI 自家服务时才用 `openai`。

`LLM_DEFAULT_*` 默认覆盖所有任务。需要的话也有按任务粒度的覆盖
(`LLM_REFLECT_MODEL` / `LLM_INGEST_MODEL` 等),把"贵但低频"的环节
路由到更强的模型——比如 `deepseek-v4-pro`——但 `v4-flash` 的智能其实
足够了,通常不用费这个心。

vision(图片 ingest)和 audio(音频转写)需要独立配,因为通常用不同
provider:

```ini
# 没有视觉模型就不填,图片会跳过 VLM 流程
LLM_VISION_BASE_URL=https://api.deepseek.com/v1
LLM_VISION_MODEL=deepseek-vl
```

长调研答案如果在最终回答阶段撞到模型 token 上限,运行时会在服务端续写,
GUI 仍然只收到一个合并后的 `answer` 事件:

```ini
AGENT_EXECUTE_MAX_TOKENS=2048
AGENT_FINAL_ANSWER_CONTINUE_TURNS=3
AGENT_FINAL_ANSWER_MAX_CHARS=120000
```

改完 `.env` 跑一次迁移:

```bash
alembic upgrade head
```

---

## 4. 跑通一次完整流程

```bash
marginalia
```

进入 REPL。下面这 5 步是最小可行流程:

```
marginalia> /upload paper.pdf /
   ↳ 把 paper.pdf 拷进 vault 根目录,入队 ingest 任务

marginalia> /tree
   ↳ 看一下文件夹结构,确认 paper.pdf 进来了

marginalia> /info <entry_id>
   ↳ 看 ingest 状态。status=pending → processing → done 才算入册完毕
   ↳ 第一次跑要等十几秒到几十秒(看模型快慢)

marginalia> 这篇论文讲了什么?
   ↳ 进入 agent 流程:planning → tool calls → answer
   ↳ 答案末尾会有 [^a] [^b] 角标,引用具体段落

marginalia> /export
   ↳ 把刚才那次对话(含引用原文)打包成 zip,落到当前目录
```

出现 `✓ answer ready` 就是这一轮跑完。

---

## 5. 怎么读懂事件流

提一个问题之后,屏幕上会逐行刷出来:

```
⠋ planning the investigation...      ← Plan 阶段(零工具,只想)
⠋ calling search_journal(...)         ← Execute 阶段调工具
⠋ calling read_files(entry_id=...)
⠋ investigator thinking...            ← LLM 在写答案
✓ answer ready
```

**没看到 `planning` 就直接到 `answer ready`**:正常。Plan 阶段判定
是"闲聊"或"简单查找",直接出答案。

**`thinking` 卡很久**:LLM 在生成最终答案,正常等。

**循环出现同一个 `calling X(...)`**:agent 在重复调同一工具,框架的
doom-loop 保护会在 6 次内强制收尾。如果反复出现,看 §8 故障处理。

**末尾的 `[tokens in=X out=Y tools=N llm_calls=M ...]`**:这一轮的
计费明细。

---

## 6. 上传更多文件

### 单文件

```
marginalia> /upload ~/Downloads/paper.pdf /papers
```

第二个参数是 vault 内的路径。`/` 开头 = 绝对路径(从 vault 根算起);
不以 `/` 开头 = 相对当前 remote cwd。

### 切换 remote cwd 后批量上传

```
marginalia> /cd /papers/2024
marginalia> /upload ~/Downloads/p1.pdf
marginalia> /upload ~/Downloads/p2.pdf
   ↳ 都进 /papers/2024/ 下
```

### 一次拷一个目录

```
marginalia> /upload ~/Downloads/notes /
   ↳ 把整个 notes/ 目录搬进 vault 根
```

### 已经存在同名文件

```
marginalia> /on-conflict rename     # 自动加 (1) (2)
marginalia> /on-conflict skip       # 跳过
marginalia> /on-conflict error      # 报错(默认)
```

设置只对当前 session 生效。

### 在 marginalia 之外改了文件

```
marginalia> /check
   ↳ diff 磁盘 vs db,只读不动
marginalia> /ingest --all
   ↳ 把变化同步进 db(类似 git add -A)
```

单文件同步:

```
marginalia> /ingest /papers/edited.md
```

---

## 7. 提问的几种姿势

### 一次性问答(不进 REPL)

```bash
marg ask "我收藏的扩散模型论文里关于 score-based 方法的有哪些?"
```

直接在终端打印答案,不开 session。适合快速查询。

### 进入 chat session(多轮)

```bash
marg chat
   或在 REPL 里: marginalia> /new
```

每轮 follow-up 都带上文。结束:`/clear`(标 cleared)或直接 `/quit`。

### 导出对话

```bash
# 单文件 markdown(含引用列表)
marg export <conv_id> -o answer.md

# zip bundle(含引用原文片段)
marg export <conv_id> --bundle -o report.zip

# 整个 session 的所有对话
marg export <session_id> --all --bundle -o session.zip
```

`<conv_id>` 在每轮答案末尾的 `[...]` 行里能看到。也可以用 `$LAST`
代表最后一轮。

---

## 8. 故障处理

### 文件卡在 processing

```
marginalia> /info <entry_id>
   ↳ 看 ingest_status。卡 5 分钟以上不动 = 真卡了
```

每 10 分钟会有 `recover_stuck_tasks` 自动跑一次,把超时任务重置为
pending。手动触发整轮维护:

```
marginalia> /tend
```

### ingest 反复失败

```
marginalia> /info <entry_id>
   ↳ 看 last_error。前缀会标 Parse: / LLM: / Route:
```

- `Parse:` —— 文件解析失败。可能文件损坏 / 格式不支持。
- `LLM:` —— 调用 LLM 失败。99% 是 API key / quota / 网络。
- `Route:` —— 自动归档失败。极少见,通常是 LLM 输出 schema 不合规,
  下一轮 recover 会重试。

### LLM API key 失效

任务会反复失败到 `dead`。修好 `.env` 后重启 marginalia,
`recover_stuck_tasks` 会给 dead 任务一次重试机会。

### 想强制重新 ingest 一个文件

目前没有专用命令。最直接的办法:

```
marginalia> /info <entry_id>
   ↳ 记下 file_id
```

然后通过 API 或 SQL 把对应 `files` 行的 `ingested_at` 清成 NULL,
`ingest_status` 改回 `pending`,下一轮 recover 会重新入队。

> 提醒:这是绕过 write-once 的口子,只在 ingest 出 bug、需要重跑时用。
> 正常使用不需要。

---

## 9. 备份与迁移

### 单机备份(SQLite + 本地 / mirror 存储)

直接 `cp -r` 整个 `MARGINALIA_HOME` 目录。db 文件 + library + objects
都在里面。

跑着的时候 cp 也能拷,但更稳的做法是先 `/quit`,再拷。

### 从 SQLite 迁到 Postgres

1. 先把 SQLite 库正常关停(`/quit`)
2. 起 Postgres,改 `.env`:

```ini
DB_BACKEND=postgres
POSTGRES_DSN=postgresql+asyncpg://user:pass@localhost:5432/marginalia
```

3. 跑迁移:

```bash
alembic upgrade head
```

4. 数据迁移目前没有自动化工具——v1 假设你迁的时候库还小,可以接受
   "重新 ingest 一遍"。要保留历史的话,自己写个脚本从 SQLite dump
   出来 INSERT 进 Postgres。

### 从 mirror / local 存储迁到 S3

```bash
marginalia storage migrate --to s3
```

会把所有 `files.storage_key` 重写指向 S3 对象,物理文件批量 PUT。
跑之前 `.env` 里 S3 配置必须先填好。

---

## 10. 多机共享一个库

SQLite 不支持多进程写,要多机必须先迁到 Postgres + S3。

**A 机(server)**:

```bash
# .env: DB_BACKEND=postgres, STORAGE_BACKEND=s3, WORKER_ENABLED=true
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
```

**B 机(client)**:

```bash
marginalia --server http://A.lan:8000
```

或在 B 机的 `~/.marginalia/.env` 里写 `MARGINALIA_SERVER=http://A.lan:8000`,
之后直接 `marginalia` 即可。

### Docker compose 一键起栈

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
```

会启动 api + worker + Postgres + MinIO。`alembic upgrade head` 在 api
启动时自动跑,MinIO bucket 由一次性 init 容器创建。

```bash
marginalia --server http://localhost:8000
```

---

## 11. 看后台在干什么

```
marginalia> /tend
   ↳ 触发一次完整的离线维护周期(normalize_tags / enrich_tags / restructure /
     suggest_demotion 等),实时打印每个任务的产出
```

被 demoted / archived 的 entry 默认不参与新查询的检索范围。想看哪些
被自动降级了:

```
marginalia> /search <keyword>
   ↳ 默认只搜 active。没搜到再加 --include-archived 看全集
```

恢复一个被自动归档的 entry:

```
marginalia> /restore <entry_id>
```

---

## 12. 退出

```
marginalia> /quit
```

或直接 Ctrl+D。embedded 模式下会同时关停 server + worker,跑到一半的
任务在下次启动时由 `recover_stuck_tasks` 接管恢复。
