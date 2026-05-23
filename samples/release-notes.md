# Marginalia v1 — 发布说明

V1 是 Marginalia 的首个完整可用版本：从上传文件到对话回答，端到端
跑通。本文档列出主要能力、已知边界和后续路线。

## 主要能力

### 用户视角
- `marginalia` CLI，类似 Claude Code 的 slash 命令体验
- `/upload` 单文件上传，三种重名策略（rename / error / skip）
- `/search` 按文件名 + 内容召回，但不暴露 AI 字段
- `/info` 查文件元数据 + 一句话摘要（"标注卡"）
- `/download` 单文件 / 文件夹 zip
- `/export` 把对话和引用文件打包成 zip

### Agent 视角
- 12 个工具覆盖：journal 检索、结构浏览、metadata 批量、文件原文
  读、表格 SQL、日志过滤
- Plan-Execute 循环带预算 tail，超过 11 轮开始督促收尾，15 轮硬上限
- prompt cache 友好：稳定层 system prompt + 工具列表 + 知识库快照

### 离线维护
- 12 个 task kind，从 ingest 到周期清理全自动化
- normalize_tags 和 restructure_catalogs 都是 LLM 把关
- lifecycle 自动降级：30 天没被 journal 提到的 active entry → demoted
- audit 90 天滚动，task_outcomes 30 天滚动

## 已知边界

- 扫描 PDF 暂不处理（PdfNeedsOcrError 标 failed）
- 容器（zip/tar/git）pipeline 未做，将在 Cycle 21 加
- 推荐式后台挖掘（共现、随机漫游）将在 Cycle 22+ 加
- 真实 LLM 联调待用户提供 key 后跑

## 数据规模

- 测试在 SQLite + local storage 跑通，目标支持 ~10万 entries
- Postgres + S3 后端框架就位但未压测
- 单机部署可行（API server + worker daemon 两进程足够）

## 测试覆盖

20 个 e2e 测试，覆盖：upload / ingest / reflect / dispatcher / purge /
normalize_tags / enrich_tags / lifecycle / restructure / agent runtime
/ agent tools / user mgmt / cli / image pipeline / user files / export
/ pdf / pdf-with-images / duckdb tools / worker

## 路线图

- Cycle 21: container pipeline + analyze_container 工具
- Cycle 22: mine_session_cooccurrence（journal-based 使用驱动挖掘）
- Cycle 23: mine_corpus_evidence（结构引导随机漫游）
- Cycle 24+: propose_views / refresh_entry_extra 等
