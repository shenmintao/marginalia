# Marginalia Quickstart

Marginalia 是一个个人知识库系统。设计灵感来自图书馆学：你上传文档，
图书馆员（一个 LLM agent）在背后默默把它们编目、关联、归类。需要查
什么的时候，你和它对话，它去翻书架找答案。

## 三个核心动作

1. **上传**：把文件交给 Marginalia，它会做 dedup、自动建文件夹路径
2. **入册**：后台 worker 异步读文件、生成结构化描述、打标签、归类
3. **对话**：你问问题，agent 翻自己的笔记本（journal）、浏览结构、
   读必要的文件，给出基于证据的回答

## 第一次使用

```bash
# 起服务（API + worker 分两个进程是生产做法）
uvicorn marginalia.main:app
marginalia-worker

# 进 CLI
marginalia
marginalia> /upload ./paper.pdf /research/llm/
marginalia> 帮我对比 Raft 和 Paxos
```

## 几个细节

- 文件路径有歧义时（`/repos/marginalia` 没扩展名也没尾斜杠），
  CLI 会要求 `--name` 显式指定
- agent 的回答里 `[^a]` 角标对应文件引用，含 entry_id + section_id
  + reason
- `/export` 不带参数时自动导出最近一次对话（含引用的文件）
