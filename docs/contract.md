# Journal trial balance v1

为账务数据整理人员校验全量分录快照，生成按币种分开的试算表。accounts为1..1000个唯一id及normal(debit/credit)，entries最多5000个唯一id、currency和2..100条lines(account,side,amount)。金额为1..10^12整数最小货币单位，禁止浮点/布尔；currency是调用方统一的币种标识，不自动查询精度或汇率。
每条分录单一币种，借方和必须等于贷方和；未知科目和重复分录失败，重复科目行合法并累计，不擅自去重。每币种输出全部科目借贷累计、净借/净贷及按正常余额方向的有符号余额。试算总借=总贷，全部净借=净贷。样例USD现金借方净额700、收入贷方1000、成本借方300，借贷流水各1300。
空entries合法并输出空币种表；未用科目仍在出现币种表中显示0。无期初、日期分期、汇兑、税务或真实财务认证；一次完整快照重算，不是账簿CRUD或银行对账。样例虚构。

---

# 开放项核销工作台（独立功能）

在不改变本 v1 试算契约的前提下，新增基于标准库 SQLite 的本地开放项核销工作台，命令为
`journal-check open-items ...`，Python 接口为 `journal_check.openitems`。它复用本文件的标识符、
整数金额、8MB 严格 JSON 与借贷平衡规则，但拥有独立的字段、报告结构和异常约定，详见
[open-items.md](open-items.md)。

---

# 期间结转链工作台（独立功能）

在不改变本 v1 试算契约与开放项工作台的前提下，新增本地不可变期间结转链工作台，命令为
`journal-check close-chain ...`，Python 接口为 `journal_check.closechain`。它复用本文件的
标识符、整数金额、8MB 严格 JSON 与借贷平衡规则，但拥有独立的请求字段、结果结构、持久化
版本管理与异常约定，且不读写开放项数据库、不做往来核销、汇兑或日期推断，详见
[close-chain.md](close-chain.md)。

---

# 报价证据驱动的多币种分录折算（独立功能）

在不改变本 v1 试算契约、开放项与结转链工作台的前提下，新增把原币分录快照折算为单一展示币种
试算表的本地工作台，命令为 `journal-check translate --source ... --output ...`，Python 接口为
`journal_check.translate`。它复用本文件的标识符、整数金额、8MB 严格 JSON 与借贷平衡规则
（journal 先通过原 `parse_journal` 全部校验），但拥有独立的请求字段、逐日期报价证据、精确有理
折算与四舍六入五成双规则、待处理分录与报告结构；不消费另两个工作台的存储、不核销开放项、
不结转期间。详见 [translate.md](translate.md)。
