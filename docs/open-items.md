# 本地开放项核销工作台 · Open-items workbench v1

面向账务数据整理人员，把**已平衡**的往来分录按明确、允许的 party 关系落账核销。
工作台只做整数最小货币单位的本地核算：**不做汇兑、不做模糊匹配、不做外部财务认证、不自动生成分录**。
仅依赖 Python 标准库（含 `sqlite3`），无网络服务。

试算核验（`analyze(data)`、原 `journal-check --source ... --output ...` 命令与 v1 报告）保持不变；
本功能是独立的 `open-items` 子命令组和独立的 Python 包 `journal_check.openitems`。

---

## 1. 数据模型与标识规则

所有标识符（`batch_id`、`items[].id`、`entry`、`party`、`settlement_key`、科目 id、币种 id）
沿用 v1 规则：非空字符串、去首尾空白后与自身相等、长度 ≤ 128、不含控制字符。
金额与数量均为整数，禁止浮点和布尔；开放项金额上限沿用分录行的 `1..10**12`。

一个**开放项（open item）引用一条原始分录行**：

- `line` 是该分录 `lines` 的**零基序号**；
- 开放项的初始剩余量 `initial = remaining = 该行 amount`；
- `side`、`currency`、`account` 全部从被引用的原行推导，调用方不能另行指定；
- 同一 `(entry, line)` 全库只能开放一次；不同分录行即使科目相同也是不同来源，互不合并。

---

## 2. `ingest`：导入一个批次

### 2.1 输入文件

接收一个受现有 **8 MB 严格 JSON** 约束的文件（重复键、`NaN`/`Infinity`、未定义字段一律拒绝）。
顶层对象**恰为**三个字段：

| 字段 | 约束 |
| --- | --- |
| `batch_id` | 标识符，全库唯一 |
| `journal` | 沿用 v1 的 `accounts` / `entries` 结构与整数、借贷平衡规则 |
| `items` | 开放项数组，至少 1 条，最多 20000 条 |

每个 `items` 元素**恰为** `id`、`entry`、`line`、`party`、`settlement_key`、`priority`：

| 字段 | 约束 |
| --- | --- |
| `id` | 标识符，开放项 id **全库唯一** |
| `entry` | 必须是本批次 `journal.entries` 中存在的分录 id |
| `line` | 非负整数，且是该分录 `lines` 的合法零基序号 |
| `party` / `settlement_key` | 标识符规则 |
| `priority` | `0..10**6` 的整数 |

### 2.2 跨批次规则

- 跨批次出现**相同科目 id 时 `normal` 必须一致**；允许引入新科目。
- **禁止分录 id 重复**（即使在不同批次）。
- 开放项 id 全库唯一；同一分录行不能重复开放。
- 历史分录不可改写：分录与分录行只插入、不更新。

### 2.3 幂等与原子性

- 相同 `batch_id` **仅当原文件字节 SHA256 相同**时幂等返回（回执 `replayed: true`），
  字节不同则报 `ConflictError`，不写入任何内容。
- 任何冲突或非法引用都必须整体失败：**不留下半批、半成品科目或半条开放项**。
  全部校验先在内存完成，写入放在单个 `BEGIN IMMEDIATE` 事务里，异常即回滚。

### 2.4 回执字段

```json
{
  "batch_id": "demo-batch-001",
  "source_sha256": "<原文件字节 SHA256>",
  "stored_sha256": "<已登记的同值 SHA256>",
  "replayed": false,
  "item_ids": ["C1", "C2", "D1", "D2"],
  "item_count": 4
}
```

---

## 3. `status`：当前开放项与未核销额

输出逐项的初始量与剩余量，并按 **币种 × 科目**汇总**未核销借方额 `open_debit` 与贷方额 `open_credit`**。
两个方向分别累计，**不得用科目净额掩盖方向相反的开放项**（同一科目可同时出现正数的未核销借方和贷方）。

```json
{
  "open_item_count": 4,
  "open_items": [
    {
      "id": "D1",
      "batch_id": "demo-batch-001",
      "entry": "sale-alpha",
      "line": 0,
      "party": "party-alpha",
      "settlement_key": "monthly-netting",
      "priority": 10,
      "side": "debit",
      "currency": "USD",
      "account": "receivable",
      "initial_amount": 5,
      "remaining": 5
    }
  ],
  "unsettled": [
    { "currency": "USD", "account": "receivable", "open_debit": 10, "open_credit": 10 }
  ],
  "plans": { "planned": 0, "committed": 0 },
  "batches": 1
}
```

---

## 4. `plan`：确定性核销规划

### 4.1 请求

- `seeds`：非空开放项 id 数组。seed 不存在 → `NotFoundError`；存在但剩余量为 0 → `ExhaustedError`。
- `rules`：最多 **500** 条允许关系，每条恰为 `{debit_party, credit_party, priority}`：
  - 双方为**不同** party；`priority` 为 `0..10**6` 整数；
  - 有向 party 对唯一（`(A,B)` 与 `(B,A)` 视为不同方向，可分别存在）。

### 4.2 隐式二分图

取数据库中**所有剩余量 > 0** 的开放项建隐式二分图。一条边同时要求：

1. 借贷双方 `currency` 相同；
2. `settlement_key` 相同；
3. `side` 相反；
4. 有向 party 对 `(debit.party, credit.party)` 被某条 rule 允许。

只规划 seed 所在的连通分量；**孤点合法**，得到 0 分配。每个分量硬性上限
**100 个开放项、2500 条候选边**，超限抛 `LimitError`，**禁止截断、禁止按最小货币单位展开成单位节点**。

### 4.3 边序与优化目标

边按以下键**升序**排列（即规范序，含全局 `edge_seq`）：

```
(rule.priority, debit_item.priority, credit_item.priority, debit_item.id, credit_item.id)
```

在每个端点剩余容量内：

1. 先**最大化整数核销总量**（整数最大流）；
2. 再按上述边序，对**完整分配向量（含 0 边）字典序最大化**。

输出非零分配、总量、逐项计划后剩余量，以及可持久读取的 `plan_id`。
相同状态与相同规则必然产生相同分配与相同 `plan_id`，**不依赖数据库行序或输入排列**
（规则与 seed 先规范化排序，plan_id 由规范 JSON 的 SHA256 得到）。

### 4.4 验收样例 D1/D2/C1/C2

`D1=5、D2=5、C1=5、C2=5`，候选边 `D1-C1、D1-C2、D2-C1`，且 `D1-C1` 优先级最高。
全局最大总量为 **10**，字典序最优解必须让 **`D1-C1 = 0`**、`D1-C2 = 5`、`D2-C1 = 5`；
局部“先到先核销”得到的 `D1-C1 = 5` 不是合法结果。另含多最优解的规范选择、孤点与 `10**12` 容量验收。

### 4.5 计划报告字段

```json
{
  "plan_id": "<64 位十六进制 SHA256>",
  "state": "planned",
  "seeds": ["C1", "C2", "D1", "D2"],
  "rules": [ { "debit_party": "...", "credit_party": "...", "priority": 0 } ],
  "components": [["C1", "C2", "D1", "D2"]],
  "edges": [
    { "edge_seq": 0, "debit_item_id": "D1", "credit_item_id": "C1",
      "rule_priority": 0, "debit_priority": 10, "credit_priority": 10 }
  ],
  "allocations": [
    { "debit_item_id": "D1", "credit_item_id": "C2", "amount": 5 }
  ],
  "total_amount": 10,
  "item_remaining_after": [
    { "item_id": "D1", "remaining_before": 5, "remaining_after": 0 }
  ]
}
```

计划只读取当时的开放项快照，不修改任何剩余量；计划持久化在 `plans` 表中，可随时用 `plan_id` 重读。

---

## 5. `commit`：重新判定后的原子落账

### 5.1 输入与重新判定

`commit` **只接收 `plan_id`**。提交时必须依据该计划保存的规则与 seed，**重新判定当前组件**。
仅当以下全部与规划时一致才允许扣减，否则整次 `ConflictError` 失败，**绝不隐式改算后提交**：

- 组件的正剩余项集合（成员闭包）不变；
- 每项身份与属性（side/currency/account/party/settlement_key/priority）不变；
- 每项当前剩余量等于规划快照中的剩余量；
- 候选关系集合不变；新出现的桥接开放项（把规划组件连到原不相连项）同样使计划失效。

账簿新增**桥接项或相关余额变化**必须使旧计划失效；其他**不连通组件**的批次导入或核销不得使它失效。

### 5.2 事务、并发与重试

- 计划由 `planned → committed`、开放项扣减、可追溯分配（`plan_allocations`、`item_flows`）
  与提交回执（`commits`）在**同一事务里全成或全败**。
- 两个独立连接同时提交竞争计划：写事务用 `BEGIN IMMEDIATE` 串行化，**最多一份生效**，
  失败者在重新判定时发现余额/组件已变而 `ConflictError`，且不得出现负余额。
- **提交点之前**抛错，重开数据库仍可提交原计划；**提交点之后**重试不会再次扣减。
- 已提交的同一 `plan_id` 在跨进程重试时返回**原回执**（即使原组件已耗尽或发生后续变化）。

### 5.3 守恒不变式

存储层对每个开放项维护并检查：

```
initial_amount = remaining + SUM(该开放项已提交的 item_flows.amount)
```

借、贷两侧各按**实际参与金额**扣减；不依赖“试算借贷相等”来认定核销正确。
该不变式同时由提交事务中的显式校验和 SQLite 触发器 `trg_open_items_conservation` 强制，
直接写库绕过应用层也会被触发器拒绝。

### 5.4 回执字段

```json
{
  "plan_id": "<sha256>",
  "commit_seq": 1,
  "state": "committed",
  "total_amount": 10,
  "allocations": [
    { "debit_item_id": "D1", "credit_item_id": "C2", "amount": 5 }
  ]
}
```

---

## 6. 命令行

数据库通过 `--db` 指定为标准库 SQLite 文件（不存在则创建，使用 WAL）。

```sh
journal-check open-items ingest --db run/ledger.sqlite --source fixtures/open_items_batch.json
journal-check open-items status --db run/ledger.sqlite
journal-check open-items plan   --db run/ledger.sqlite --request fixtures/open_items_plan.json
journal-check open-items commit  --db run/ledger.sqlite --plan-id <plan_id>
```

- `plan` 的 `--request` 是恰含 `seeds` 与 `rules` 的 JSON 文件。
- 报告以排序键、无 `NaN` 的 JSON 打印到 stdout；`status`/`plan`/`commit` 可选 `--output`
  走与 v1 相同的原子发布（符号链接拒绝、失败保留旧文件）。
- 任何业务错误打印消息到 stderr 并以 **退出码 2** 结束；成功退出 0。

## 7. Python 接口（可被测试直接调用）

```python
from journal_check.openitems import connect, ingest, status, plan, commit, get_plan
from journal_check.openitems.errors import (
    OpenItemError, ValidationError, ConflictError,
    NotFoundError, ExhaustedError, LimitError,
)

conn = connect('run/ledger.sqlite')
ingest(conn, data, source_sha256)   # data 为已解析的严格 JSON 对象
status(conn)
plan(conn, seeds, rules)
commit(conn, plan_id)
get_plan(conn, plan_id)
conn.close()
```

纯算法部分（不接触数据库）在 `journal_check.openitems.planner`，
存储与触发器在 `journal_check.openitems.storage`。

## 8. 异常约定

所有工作台异常都是 `ValueError` 的子类，CLI 统一映射为退出码 2：

| 异常 | 触发场景 |
| --- | --- |
| `ValidationError` | 输入结构、标识符、引用、规则非法；失败的 ingest 不留任何数据 |
| `ConflictError` | 批次字节冲突、id 唯一性、跨批科目 normal、计划组件在提交前变化 |
| `NotFoundError` | 批次/开放项/计划 id 不存在 |
| `ExhaustedError` | seed 开放项剩余量为 0 |
| `LimitError` | 分量超过 100 项或 2500 边、规则超过 500 条等硬上限 |

样例数据均为原创虚构（见 `fixtures/SOURCES.md`），不代表真实主体或真实账务。
