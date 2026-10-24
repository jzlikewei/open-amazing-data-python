# OpenAmazingData 与厂商 SDK 的差异

`OpenAmazingData` 的对外接口（类名、方法名、参数位置、返回形状、本地缓存格式）
与厂商 AmazingData 1.1.15 一致。厂商代码里有一批缺陷：崩溃、写错文件、把真实值
抹成空、改动调用方传入的对象、同一个查询命中缓存与否返回形状不同。自研包**修正**
这些缺陷，不照抄。下面逐条写厂商的表现与自研包的表现。

判定为缺陷的标准：厂商代码与它自己明显的意图矛盾。看起来是有意为之的策略
（增量起点不加一天、靠去重合并；成分股读本地不回落下载；日志文案）照厂商保留，
不算缺陷。

每条的「厂商表现」都在 AmazingData 1.1.15 的 `.pyc` 上用假网关实测过（2026-10-17）。

## 汇总

| 编号 | 涉及 | 厂商表现 | 自研包 |
| --- | --- | --- | --- |
| V01 | `get_kzz_put_call_item` 等 3 个 | `is_local=True` 必然 `AttributeError` | 读自己的缓存，没有就下载 |
| V02 | `get_kzz_put_explanation` | 结果写进兄弟表，覆盖别人的缓存 | 写自己的文件 |
| V03 | `get_announcement_stock` | 传空清单表必然 `AttributeError` | 与基金、债券两支一致 |
| V04 | `get_history_stock_status` | `MARKET_CODE` 整列 NaN | 填真实代码 |
| V05 | `get_index_weight` / `get_industry_weight` | 命中缓存与否返回顺序不同 | 都按日期倒序 |
| V06 | 同上 | 无数据时一个丢键、一个 `KeyError` | 都给一张空表 |
| V07 | `get_treasury_yield` | 命中缓存与否返回的列不同 | 形状一致 |
| V08 | `save_report_abstract_md` | 往调用方传入的表上加列 | 不动调用方的对象 |
| V09 | `get_hist_code_list` | 某种空回包时永久挂住 | 不挂住 |
| V10 | `query_kline` / `query_snapshot` | 混查时缺省时窗由第一个代码定 | 每个代码按自己的交易所取 |
| V11 | `get_index_weight` / `get_industry_weight` | 开始日期晚于结束日期时永久挂住 | 返回空表 |

## 逐条

### V01 可转债三个方法 is_local=True 必然 AttributeError
- 涉及：`InfoData.get_kzz_put_call_item`、`InfoData.get_kzz_put_explanation`、`InfoData.get_kzz_call_explanation`
- 厂商 1.1.15：读本地分支取 `LocalDataFolder` 上不存在的成员（`Kzz_Put_call_item`、`Kzz_Put_explanation`、`Kzz_Call_explanation`；枚举里的实名是 `Kzz_Put_Call_Item`、`Kzz_Put_Explanation`、`Kzz_Call_Explanation`）。`is_local=True`（缺省值）时抛 `AttributeError: type object 'LocalDataFolder' has no attribute 'Kzz_Put_call_item'`，本地有没有缓存都一样，一个请求也发不出去。
- OpenAmazingData：与同族其余可转债方法一致，读各自的缓存文件，读不到就下载。

### V02 get_kzz_put_explanation 把结果写进兄弟表
- 涉及：`InfoData.get_kzz_put_explanation`
- 厂商 1.1.15：下载时读自己的表，落盘却写 `kzz_put_call_item.h5`。先下 `get_kzz_put_call_item` 再下 `get_kzz_put_explanation`，前者的缓存被覆盖成 15 列的回售执行说明，`kzz_put_explanation.h5` 从来不会被创建。
- OpenAmazingData：写自己的 `kzz_put_explanation.h5`，回售赎回条款那张表不受影响。

### V03 股票公告 PDF 的空表分支返回不存在的属性
- 涉及：`InfoData.get_announcement_stock`
- 厂商 1.1.15：传空的清单表时返回 `self.announcement_bond_stock_tag_df`，这个属性没人建过，必然 `AttributeError: 'InfoData' object has no attribute 'announcement_bond_stock_tag_df'`。基金、债券两支正常。
- OpenAmazingData：三支一致，空表时返回各自的 `<品种>_list_tag_df`。

### V04 历史证券状态的 MARKET_CODE 整列是 NaN
- 涉及：`InfoData.get_history_stock_status`
- 厂商 1.1.15：两张表都带 `MARKET_CODE`，合并后变成 `MARKET_CODE_x`、`MARKET_CODE_y`，列清单里的 `MARKET_CODE` 落到补列上。实测 `600519.SH` 60 行，`MARKET_CODE` 的 dtype 是 `float64`，取值全是 NaN。
- OpenAmazingData：`MARKET_CODE` 填这次请求的代码，其余 10 列与列序不变。

### V05 权重表命中缓存与否返回顺序不同
- 涉及：`InfoData.get_index_weight`、`InfoData.get_industry_weight`
- 厂商 1.1.15：排序后的表只放进返回值，落盘的是排序前那张。实测第一次查询 `TRADE_DATE` 从 `20251231` 开始倒序，第二次读本地从 `20240101` 开始升序。
- OpenAmazingData：落盘排序后的表（文件名与 HDF5 key 不变），读到厂商写的升序旧缓存时也按日期倒序返回。命中缓存与否顺序一致。

### V06 网关没给这只代码时丢键或抛 KeyError
- 涉及：`InfoData.get_index_weight`、`InfoData.get_industry_weight`
- 厂商 1.1.15：网关回「成功、无数据」时，行业权重返回的 dict 里没有这个代码（实测 `{}`），指数权重抛 `KeyError: 'TRADE_DATE'`。
- OpenAmazingData：两支都给这个代码一张 0 行、列齐全的空表，不抛错。

### V07 国债收益率命中缓存与否返回的列不同
- 涉及：`InfoData.get_treasury_yield`
- 厂商 1.1.15：只在下载那条路上 `set_index('DATE')`，读本地时原样返回带 `DATE` 列的表。实测第一次 `['YIELD']`（index 是日期），第二次 `['YIELD', 'DATE']`（index 是 `0, 1, 2`）。
- OpenAmazingData：读本地也以 `DATE` 为 index，两次形状一致。落盘的表保留 `DATE` 列，增量起点照旧从它推。

### V08 导研报 md 时改动调用方传入的表
- 涉及：`InfoData.save_report_abstract_md`
- 厂商 1.1.15：对传入的字典只做浅拷贝，加的 `file_name` 列留在调用方的 DataFrame 上（实测多出 `file_name = ['1002.0.md', '1039.5.md', '1077.0.md']`）。落盘文件与返回值本身没错。
- OpenAmazingData：先逐张复制再处理，调用方的字典与表不变；返回值与落盘文件照旧。

### V09 历史代码表在某种空回包下永久挂住
- 涉及：`BaseData.get_hist_code_list`
- 厂商 1.1.15：逐日下载时，回调先把待收计数减一，再取回包的 `MARKET_CODE` 列。某天回「成功、零行」时这一步抛 `KeyError`，唤醒等待的那句执行不到。如果这条回包恰好是本批最后一个到达的，函数在等待上永不返回。实测空回包日在区间最后一天时 40 秒后仍在等。
- OpenAmazingData：回调不在回包上取列，不会挂住。那天没有落盘，按厂商自己的规则处理：区间最后一天并入实时代码表，其余日子打印后跳过。
- 未验证：真网关对某个交易日回「零行」还是 `status=-100`。回 `-100` 时两边一致，重试 3 轮后抛 `Exception('查询失败')`。

### V10 混查时缺省时窗由第一个代码的交易所定
- 涉及：`MarketData.query_kline`、`MarketData.query_snapshot`
- 厂商 1.1.15：没传 `begin_time` / `end_time` 时，缺省时窗在代码循环里只判断一次，后面的代码沿用第一个代码的。混查 `["600519.SH", "IF2512.CFE"]` 时两条请求的 `begin_time` 都是 900，期货拿到日盘时窗；顺序反过来时两条都是 2000，股票拿到夜盘时窗。快照同理（90000000 / 200000000）。只查一个代码时没有问题。
- OpenAmazingData：每个代码按自己的交易所取缺省时窗。显式传了 `begin_time` / `end_time` 就照用。

### V11 权重表开始日期晚于结束日期时永久挂住
- 涉及：`InfoData.get_index_weight`、`InfoData.get_industry_weight`
- 厂商 1.1.15：`begin_date` 晚于 `end_date` 时日期窗口切出空列表，一个请求也不发，随后等待回包的那句没有任何回调能唤醒，调用永不返回（实测 280 秒超时）。
- OpenAmazingData：不发请求，返回空表。

### V12 日线区间响应丢数时有界补查并保留缺口
- 2026-10-23 串行只读复现：同一证券在起点无成交的窗口返回 `status=-100, tag=DataEmpty`，缩小到后续日期有数据；更宽窗口成功但漏掉尾部。原因只能定位到上游区间响应，不推断服务端内部算法。
- `MarketData.query_kline` 的日线按调用方提供的交易日历检查缺日，逐日补查缺失日期。完整窗口只发原请求；额外查询与重试共用 `TGW_KLINE_MAX_RECOVERY_REQUESTS`（默认 64）上限。分钟与周/月/季/年周期不套用逐日完整性规则。
- 只有 `status=-100`、`tag=DataEmpty`、空载荷且无未完成分包的信封成为 typed `DataEmpty`。裸 -100、权限、限流、网络故障均不能冒充空结果。快照空响应仍按失败处理，不能推断停牌。
- 单日仍空保留 `None` / 缺日，不补 OHLC、不沿用前收。`last_kline_report` 记录交易日、已返回日、空响应日、未解决日、请求数；`complete` 只在日线逐日齐全时为真。stderr 同时记录 `kline_coverage_gap`，原 JSON 结构不变；调用方仍须验证无成交快照并执行 coverage fail-closed。
- 真实错误或预算耗尽仍抛 `Exception('查询失败')`；附加 `partial_results`（未复权原始结果，仅用于审计）与 `coverage`，不让 CLI 输出部分成功 JSON。网关停手仍为 `ServerUnavailable` / 退出码 5。qfq/hfq 对恢复出的全部真实行照常执行因子完整性检查。
- 合成回归覆盖起点空、静默漏尾、全空、单日真实错误、混合代码、去重排序、冲突、全调用预算和复权缺口。等价门禁 `truncated` 场景只登记 `result/requests` 差异；原完整行情场景保持一致。
