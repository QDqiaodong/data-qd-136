# 本地期间结转链 · Close-chain v1

面向账务数据整理人员，把连续期间的**完整分录快照、科目结转规则与独立期初控制数**
组织成可追溯的关账结果。每次成功 `apply` 发布一个**不可变 generation**，旧 generation
永不改写；`show` 可定点读取任一历史期间并校验文件哈希。

本工作台只做本地整数核算：**不读写开放项数据库、不做往来核销、不做汇兑、不做日期推断**。
各币种分别计算，绝不跨币种相抵；仅使用 Python 标准库，无外部服务。

试算核验（`analyze(data)`、原 `journal-check --source ... --output ...` 命令与 v1 报告）
与开放项工作台的行为保持不变；本功能是独立的 `close-chain` 子命令组和独立的 Python 包
`journal_check.closechain`。

---

## 1. 标识符、金额与请求文件

- `chain_id`、期间 `id`、科目 id、币种 id 沿用 v1 标识符规则：非空字符串、去首尾空白后
  与自身相等、长度 ≤ 128、不含控制字符。
- 所有金额均为整数，禁止浮点与布尔；分录行金额沿用 `1..10**12`。
- 余额统一采用**借正贷负的有符号整数**；符号只由实际借贷方向决定，**不得从
  `normal_balance` 推断符号**，因此反常余额也按实际方向处理。
- `--source` 读取一份受现有 **8 MB 严格 JSON** 约束的请求：重复键、`NaN`/`Infinity`、
  未定义字段一律拒绝；其字节 SHA256 与发布结果一并保存。

请求顶层对象**恰为**四个字段：

| 字段 | 约束 |
| --- | --- |
| `chain_id` | 标识符；同一 store 内不可改变 |
| `expected_head` | 初建为 `null`；此后必须是调用者读到的当前 generation id（64 位小写十六进制） |
| `start` | 一基位置，范围 `1..N+1`（N 为当前链上期数）；初建必须为 1 |
| `periods` | 非空连续替换片段；整链至多 **60** 期 |

替换语义：

- 覆盖旧位置时，该位置的期间 id **必须不变**，否则 `ConflictError`；
- 超过链尾即从 `N+1` 起**连续追加**，`start` 留出缺口直接拒绝；
- 旧后缀**不得隐式删除**；全链期间 id 必须唯一。

---

## 2. 期间对象

每个期间**恰为** `id`、`journal`、`roles`、`retained`、`incoming`、`opening_checks`。

### 2.1 `journal`

沿用 v1 的 `accounts` / `entries` 结构、整数规则与借贷平衡规则（每分录借和=贷和，
未知科目、重复分录、布尔金额等均拒绝）。空 `entries` 合法。

### 2.2 `roles`

为本期**每个已定义科目恰一次**的 `{account, kind}` 对象数组；`kind` 仅允许
`carry`（结转）或 `result`（损益）。遗漏、重复或引用未知科目均拒绝。

### 2.3 `retained`

留存科目 id：必须是本期 `kind=carry` 且 `normal=credit` 的科目。

### 2.4 `incoming`

从上期**每个 carry 科目恰一次**到本期 carry 科目的有理比例分配，**零余额源也必须列出**。

- 首期 `incoming` 必须为空数组；
- 每项恰为 `{account, targets}`，`targets` 非空，元素恰为 `{account, weight}`；
- 同一源行内目标科目不得重复；目标必须是本期 carry 科目，不得引用 result 或未知科目；
- `weight` 为 `1..10**6` 的整数；多个源可合并到同一目标；
- 源集合必须恰好等于上期全部 carry 科目（含零余额者）。

### 2.5 `opening_checks`

独立于计算结果的 `{currency, account, amount}` 数组：

- `account` 必须是本期 carry 科目；
- `amount` 为 `abs(value) <= 10**30` 的有符号整数，**禁止布尔**；
- 键集合必须恰好覆盖“本期出现的全部币种 × 本期全部 carry 科目”的笛卡尔积，**含零值**；
- 币种集合 = 前期继承币种 ∪ 本期 journal 币种；首期只取本期币种；
- 每个控制数必须与 `incoming` 形成的实际期初相等；任一不符即**拒绝整次 apply**，
  报告按 `(currency, account)` 排序的首个预期值与实际值；
- 漏零项、重复键、错额分别拒绝；双空集合（无币种且无检查项）合法。

---

## 3. 结转与关账计算

### 3.1 期初

- 第一期所有科目期初为零。
- 此后每个科目的期初，由上期**结转后**的 carry 余额经本期 `incoming` 分配产生。

### 3.2 期内与期末

1. 先计算 `期初 + 本期变动`；期内变动仍累加分录原始借贷额（借正贷负）。
2. 再把每个 `result` 科目的期末余额**清为零**：清零额即该科目调整前余额。
3. 全部 result 清零额的**代数和**作为留存转入额加到 `retained`；carry 科目保持其余余额。
4. 调整前与调整后都必须**逐币种**满足借贷守恒（有符号余额合计 = 有符号期初合计
   + 有符号变动合计），不能靠跨币种相抵。

验收：收入贷记 100、费用借记 30 时，两个 result 科目全部清零，留存净增加**贷记 70**
（有符号转入额为 `-70`）。`normal` 不同或反常余额一律按借贷实际方向处理。

### 3.3 最大余数整数分配（禁止浮点）

逐币种对源余额**绝对值**按权重比例分配整数：

1. 各目标先取向下整数份额 `floor(|amount| * weight / 总权重)`；
2. 剩余单位按余数（精确有理数 `余数分子/总权重`）**降序**、并列时按目标 id **升序**
   逐个补足；
3. 最后恢复源的符号。

不使用浮点，也不按最小货币单位逐个循环，`10**12`（及更大）金额可直接计算。
正负源对称且逐源守恒。验收：`+5` 与 `-5` 按相同 `1:1:1` 权重分给 `a/b/c`，
分别得到 `+2/+2/+1` 与 `-2/-2/-1`；反转输入顺序不改变结果。

### 3.4 修订传播

修订第 k 期后，必须用本次候选链**从 k 逐期重算到链尾**，不能只重算被替换片段：

- 前期 carry 分布或科目角色变化时，后期必须消费新的结转与映射结果，并重新校验其
  **原始 `opening_checks`**；前期汇总净额不变不能作为跳过后期的依据；
- 任一后续期间映射失效（例如前期某 carry 改为 result，旧 `incoming` 源失效）或控制数
  不符，**整次发布失败**：当前 head、全部历史结果与源证据不变；
- 同一请求同时修正后续规则与控制数，则可整体发布。

联合验收：建立 A→B→C 后只修订 A 的 carry 分布而保持总额，B 原控制数不符时拒绝且 C 不变；
再连同 B/C 的必要控制数修订提交，三期新期初/期末逐项联动，旧 generation 仍能重现原结果。

---

## 4. 结果结构

每个期间的结果文件含：

```json
{
  "period": "B",
  "currency_order": ["USD"],
  "accounts": ["a", "b", "c", "retained", "revenue"],
  "currencies": [
    {
      "currency": "USD",
      "accounts": [
        {
          "account": "a",
          "kind": "carry",
          "opening": 24,
          "movement_debit": 0,
          "movement_credit": 0,
          "closing_before": 24,
          "closing_after": 24
        }
      ],
      "result_clearings": [
        {"account": "revenue", "closing_before": -100,
         "cleared_amount": -100, "closing_after": 0}
      ],
      "retained_transfer": {
        "account": "retained",
        "closing_before": 0,
        "transfer_amount": -70,
        "closing_after": -70
      }
    }
  ],
  "incoming": [
    {
      "account": "cash",
      "currencies": [
        {
          "currency": "USD",
          "source_amount": 70,
          "allocations": [
            {"account": "a", "weight": 1, "amount": 24},
            {"account": "b", "weight": 1, "amount": 23},
            {"account": "c", "weight": 1, "amount": 23}
          ],
          "remainder_basis": [
            {"account": "a", "weight": 1, "floor_share": 23,
             "remainder_numerator": 1, "remainder_denominator": 3,
             "extra_unit": 1, "allocated_magnitude": 24}
          ],
          "conserved": true
        }
      ]
    }
  ],
  "opening_checks": [
    {"currency": "USD", "account": "a", "amount": 24}
  ]
}
```

- 科目行按科目 id、币种块按币种 id 规范排序；
- `incoming[].currencies[]` 给出每个源在每个币种的源金额、各目标分配额与余数分配依据，
  `conserved` 表示该源各目标分配额之和等于源金额；
- `opening_checks` 回显本期通过校验的控制数。

---

## 5. 持久化、generation 与发布点

成功 `apply` 发布一个不可变 generation，记录：

- 各期**完整源数据**与结转结果，期间源与结果**独立成文件**；
- 每个文件字节的 SHA256（写入 meta）；
- 本次**原请求字节 SHA256** 与父 generation id。

当前 head 通过**一次原子替换**切换到完整候选链；该替换是唯一发布点：

- 发布点之前进程中断：重开仍读到完整旧链；未发布的暂存残留不成为可读历史，
  也不阻碍后续合法发布（下次持锁打开时清理，且清理绝不删除已发布 generation 引用的文件）；
- 发布点之后中断：重开只能读到完整新链，读者不会看到混代、缺失文件或部分后缀；
- 发布点前写入失败必须保留旧 head。

两个独立进程以同一 `expected_head` 发布竞争修订：整店排他锁串行化，**最多一份成功**，
另一份得到明确 `ConflictError`，不能把自己的旧计算套到新 head 上；即使计算结束后 head
已变也必须拒绝，**不得隐式重基**。

`show --generation ID --period ID` 可读取任一历史期：先固定 generation，再只打开该期间的
源/结果两个文件并核对目标文件哈希，**不读取其他期间的文件正文**；默认 `show` 先固定
当前 head，整个读取只属于该 generation。哈希不符或文件缺失抛 `CorruptionError`。

故障注入点（环境变量，触发一次，用于验收）：
`JOURNAL_CHECK_FAULT_SOURCE_WRITE`、`JOURNAL_CHECK_FAULT_RESULT_WRITE`、
`JOURNAL_CHECK_FAULT_BEFORE_HEAD_SWITCH`、`JOURNAL_CHECK_FAULT_AFTER_HEAD_SWITCH`。
存储目录布局、锁策略与恢复算法均为实现细节，不作为契约。

---

## 6. 命令行

```sh
journal-check close-chain apply --store run/chain --source request.json [--output report.json]
journal-check close-chain show  --store run/chain [--generation <id>] --period <id> [--output report.json]
```

- `--store` 指定 store 目录，不存在则创建；
- `show` 省略 `--generation` 时固定当前 head；
- 报告以排序键、无 `NaN` 的 JSON 打印到 stdout；`--output` 走与 v1 相同的原子发布
  （符号链接拒绝、失败保留旧文件）；
- 任何业务错误打印消息到 stderr 并以 **退出码 2** 结束；成功退出 0。

`apply` 成功回执：

```json
{
  "chain_id": "c1",
  "generation_id": "<64 位小写十六进制>",
  "parent": "<父 generation id 或 null>",
  "request_sha256": "<原请求字节 SHA256>",
  "head_generation_id": "<与 generation_id 相同的当前 head>",
  "period_count": 2
}
```

`show` 回执：`chain_id`、`generation_id`、`parent`、`request_sha256`、`position`、
`period_id`、`source`、`source_sha256`、`result`、`result_sha256`。

## 7. Python 接口（可被测试直接调用）

```python
from journal_check.closechain import apply, show, open_store
from journal_check.closechain.engine import (
    allocate, compute_chain, parse_period, prepare_candidate,
)
from journal_check.closechain.errors import (
    CloseChainError, ValidationError, ConflictError,
    NotFoundError, LimitError, CorruptionError,
)

store = open_store('run/chain')
receipt = apply(store, request_obj, request_sha256)   # 已解析的严格 JSON 对象
record = show(store, generation_id, period_id)
```

纯算法（不接触文件系统）在 `journal_check.closechain.engine`，
持久化与锁在 `journal_check.closechain.store`。

## 8. 异常约定

所有异常都是 `ValueError` 的子类，CLI 统一映射为退出码 2：

| 异常 | 触发场景 |
| --- | --- |
| `ValidationError` | 请求/期间结构、标识符、引用、incoming 规则或期初控制数非法；失败不发布任何内容 |
| `ConflictError` | `expected_head` 与当前 head 不符、覆盖位置期间 id 改变、chain_id 改变；绝不隐式重基 |
| `NotFoundError` | store 无发布、generation id 或期间 id 不存在 |
| `LimitError` | 整链超过 60 期等硬上限 |
| `CorruptionError` | 存储文件缺失、无法解析或哈希校验失败；读取不会修改 head |
