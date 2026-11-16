# data-qd-136 · 复式记账试算核验

Exact minor-unit journals and currency-separated trial balances。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。
本地开放项核销工作台（独立 `open-items` 子命令组）的字段、报告结构与异常约定详见 [开放项工作台契约](docs/open-items.md)。
本地期间结转链工作台（独立 `close-chain` 子命令组）的请求字段、报告结构、异常与退出码详见 [期间结转链契约](docs/close-chain.md)。
报价证据驱动的多币种分录折算（独立 `translate` 子命令）的报价图、折算账务、待处理与报告结构详见 [折算工作台契约](docs/translate.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/journal-check --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/journal_check/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 开放项核销工作台（open-items）

在保留 v1 试算命令与 `analyze(data)` 的同时，新增独立子命令组，用标准库 SQLite 账簿
（`--db`）把已平衡往来分录按明确允许的 party 关系落账核销；不做汇兑、模糊匹配或外部财务认证。

```sh
journal-check open-items ingest --db run/ledger.sqlite --source fixtures/open_items_batch.json
journal-check open-items status --db run/ledger.sqlite
journal-check open-items plan   --db run/ledger.sqlite --request fixtures/open_items_plan.json
journal-check open-items commit  --db run/ledger.sqlite --plan-id <plan_id>
```

- `ingest` 沿用 8MB 严格 JSON；顶层恰为 `batch_id`、`journal`、`items`，失败不留半批。
- `plan` 在隐式二分图上先最大化整数核销总量，再按规则/开放项优先级与 id 的规范序做字典序最优；
  结果由 `plan_id` 持久化，确定性且不依赖输入或行序。
- `commit` 只认 `plan_id`，提交前重新判定组件，状态、扣减、可追溯分配与回执同事务全成全败；
  竞争提交最多一份生效，重复提交跨进程返回原回执。
- Python 接口在 `journal_check.openitems`（`connect/ingest/status/plan/commit/get_plan`），
  异常均为 `ValueError` 子类，CLI 失败退出 2。完整契约见 [docs/open-items.md](docs/open-items.md)。

## 期间结转链工作台（close-chain）

在保留 v1 试算命令、`analyze(data)` 与开放项工作台的同时，新增独立子命令组，把连续期间的
完整分录快照、科目结转规则与独立期初控制数发布为**不可变 generation** 的可追溯关账结果。
不读写开放项数据库，不做往来核销、汇兑或日期推断；各币种分别计算，仅用标准库。

```sh
journal-check close-chain apply --store run/chain --source fixtures/close_chain_request.json
journal-check close-chain show  --store run/chain --generation <generation_id> --period <period_id>
```

- `apply` 沿用 8MB 严格 JSON；顶层恰为 `chain_id`、`expected_head`、`start`、`periods`，
  初建 `expected_head` 为 `null`、`start` 为 1，此后按调用者读到的当前 head 做乐观并发控制，
  竞争发布最多一份成功，失败者明确 `ConflictError` 且不隐式重基。
- 每期间恰为 `id`、`journal`、`roles`、`retained`、`incoming`、`opening_checks`；
  借正贷负整数余额，result 科目期末清零、代数和转入 credit-normal 的 retained，
  调整前后逐币种借贷守恒。
- `incoming` 用纯整数最大余数法按权重分配 carry 余额（含零余额源、禁止浮点与逐单位循环）；
  `opening_checks` 独立核对“币种 × carry 科目”笛卡尔积上的实际期初，任一不符整次拒绝。
- 修订第 k 期必须逐期重算 k 至链尾并重新校验后期原始控制数；发布通过单原子 head 切换完成，
  崩溃只可能暴露完整旧链或完整新链；`show` 定点读取历史 generation 并核文件哈希。
- Python 接口在 `journal_check.closechain`（`open_store/apply/show`，纯算法在
  `journal_check.closechain.engine`），异常均为 `ValueError` 子类，CLI 失败退出 2。
  完整契约见 [docs/close-chain.md](docs/close-chain.md)。

## 报价证据折算工作台（translate）

在保留 v1 试算命令、`analyze(data)` 与另两个工作台的同时，新增独立子命令，把原币分录快照按
逐日报价证据折算为单一展示币种试算表，并把报价不可采信（矛盾或无路可达）的分录整条列为待处理。
全程精确有理计算、四舍六入五成双，不用浮点；不读写开放项数据库与结转链存储，仅用标准库。

```sh
journal-check translate --source fixtures/translate_request.json --output run/translated.json
```

- 顶层恰为 `journal`、`target`、`rounding_account`、`entry_dates`、`quotes`，沿用 8MB 严格 JSON；
  journal 先通过原 `parse_journal` 全部校验，原金额、借贷平衡与重复科目行规则不变。
- 逐日期只采用当天有效报价，允许正/反方向换算；连通分量仅当所有闭合路径倍率乘积**严格为 1**
  才可采信，输出精确约分倍率与可核算报价 ID 路径；矛盾给闭合见证、无路可达给可达币种集合，
  不枚举全部路径、不用浮点容差或最短路掩盖矛盾，结果不随输入排列变化。
- 每条原行分别精确折算并五成双舍入，重复科目行不提前合并；舍入为 0 的行仍逐行留存但不进试算表，
  折算差额作为单独标记的追加行进 `rounding_account`，逐分录及整体核借贷相等。
- 仅 `ready` 分录入目标账，`inconsistent`/`missing` 整条待处理且金额不泄漏；有待处理项仍退出 0，
  结构/校验/计算/I/O 失败退出 2 并保留旧报告，输出拒绝符号链接与源同路径。
- Python 接口在 `journal_check.translate`（`translate/run_file/publish`，纯报价图在
  `journal_check.translate.rates`，校验与账务在 `journal_check.translate.engine`）。
  完整契约见 [docs/translate.md](docs/translate.md)。

## 当前业务与样例
# Journal trial balance v1

为账务数据整理人员校验全量分录快照，生成按币种分开的试算表。accounts为1..1000个唯一id及normal(debit/credit)，entries最多5000个唯一id、currency和2..100条lines(account,side,amount)。金额为1..10^12整数最小货币单位，禁止浮点/布尔；currency是调用方统一的币种标识，不自动查询精度或汇率。
每条分录单一币种，借方和必须等于贷方和；未知科目和重复分录失败，重复科目行合法并累计，不擅自去重。每币种输出全部科目借贷累计、净借/净贷及按正常余额方向的有符号余额。试算总借=总贷，全部净借=净贷。样例USD现金借方净额700、收入贷方1000、成本借方300，借贷流水各1300。
空entries合法并输出空币种表；未用科目仍在出现币种表中显示0。无期初、日期分期、汇兑、税务或真实财务认证；一次完整快照重算，不是账簿CRUD或银行对账。样例虚构。

