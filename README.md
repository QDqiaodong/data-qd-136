# data-qd-136 · 复式记账试算核验

Exact minor-unit journals and currency-separated trial balances。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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

## 当前业务与样例
# Journal trial balance v1

为账务数据整理人员校验全量分录快照，生成按币种分开的试算表。accounts为1..1000个唯一id及normal(debit/credit)，entries最多5000个唯一id、currency和2..100条lines(account,side,amount)。金额为1..10^12整数最小货币单位，禁止浮点/布尔；currency是调用方统一的币种标识，不自动查询精度或汇率。
每条分录单一币种，借方和必须等于贷方和；未知科目和重复分录失败，重复科目行合法并累计，不擅自去重。每币种输出全部科目借贷累计、净借/净贷及按正常余额方向的有符号余额。试算总借=总贷，全部净借=净贷。样例USD现金借方净额700、收入贷方1000、成本借方300，借贷流水各1300。
空entries合法并输出空币种表；未用科目仍在出现币种表中显示0。无期初、日期分期、汇兑、税务或真实财务认证；一次完整快照重算，不是账簿CRUD或银行对账。样例虚构。

