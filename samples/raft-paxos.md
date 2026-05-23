# Raft 与 Paxos 共识算法对比

## 1. 引言

Raft 和 Paxos 都是分布式系统中常用的共识算法。它们解决同一个问题——
让一组节点就一个值达成一致——但路径不同。Raft 的目标是**易于理解**，
Paxos 的目标是**通用性**。

## 2. Raft

Raft 是 leader-based 算法。系统中只有一个 leader，所有写入都先经过它。

### 2.1 Leader 选举

每个节点处于 follower / candidate / leader 三个状态之一。当 follower
长时间没收到 leader 心跳时，它转变为 candidate 并发起选举。candidate
向其他节点请求投票，赢得多数票就成为新 leader。

### 2.2 Log Replication

leader 接收客户端写请求，先 append 到自己的日志，再广播给所有 follower。
当多数节点确认收到，该日志条目就被认为已 commit。

## 3. Paxos

Paxos 是 quorum-based 算法。没有固定 leader 概念，提议者（proposer）
通过两阶段投票推动决议。

### 3.1 Prepare 阶段

proposer 选一个递增的 proposal number n，向 acceptors 发送 prepare(n)。
acceptors 承诺不再接受 number 小于 n 的提议。

### 3.2 Accept 阶段

收到多数 prepare 响应后，proposer 发送 accept(n, v)，其中 v 是要决议
的值。acceptors 根据 prepare 阶段的承诺决定是否接受。

## 4. 对比

| 维度 | Raft | Paxos |
|---|---|---|
| 易理解性 | 高 | 低 |
| 通用性 | 中 | 高 |
| 性能 | leader 是瓶颈 | 多 proposer 并发 |
| 实现复杂度 | 中 | 高 |

## 5. 选择建议

新项目通常推荐 Raft。Paxos 的灵活性主要在多数据中心、Byzantine 容错
等场景才显现价值。
