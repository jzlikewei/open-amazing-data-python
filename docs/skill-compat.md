# SKILL.md 覆盖度对照表

> **状态口径已迁移。** 本文讲「每个方法映射到哪个 tgw 调用、哪个 function_id」，
> 属于映射细节。**当前能不能跑通**看 `skill-capabilities.md`（由
> `skill-capabilities.json` 生成）。本文下方「端到端待替身」的说法写在
> 行情族突破之前，行情两项的协议层已实测跑通，缺口只在 compat 是 stub。

`docs/skill-capabilities.md` 的 6 大类 39 个方法，逐条映射到
`src/tgwpy/cli.py` 的参数解析、AmazingData 业务层入口、契约 `function_id`
和底层 tgw 调用。

这张表是覆盖度的验收依据，也是防漂移的锁：`tests/test_cli.py` 会把本文档的
表格逐行与 `tgwpy.cli.CATEGORIES` 对账，改了一边忘了另一边，测试就红。

## 状态口径

| 状态 | 含义 |
| --- | --- |
| CLI 已实现 | 参数解析、分发目标、JSON 序列化都已落地，有单测覆盖 |
| 端到端待替身 | 业务层调用本身要等 `tgwpy.compat` 的纯 Python tgw 替身装上才能跑通 |
| 待验证 | 代码路径在，但依赖的字段结构尚未在真实网关上确认，缺数据时报错而非猜值 |

**39 个方法的 CLI 层全部已实现**，端到端全部卡在同一个前置条件上：
`tgwpy.compat.install()` 目前是占位实现，替身落地后下表无需改动即可逐条验证。

## 汇总

| 分类 | 方法数 | 底层调用构成 |
| --- | --- | --- |
| `base` | 8 | QueryThirdInfo × 4、QuerySecuritiesInfo × 4 |
| `market` | 2 | QueryKline × 1、QuerySnapshot × 1 |
| `finance` | 5 | QueryThirdInfo × 5 |
| `shareholder` | 7 | QueryThirdInfo × 7 |
| `index` | 6 | QueryThirdInfo × 6 |
| `other` | 11 | QueryThirdInfo × 10、QueryETFInfo × 1 |
| **合计** | **39** | QueryThirdInfo × 33、QuerySecuritiesInfo × 4、QueryKline × 1、QuerySnapshot × 1、QueryETFInfo × 1 |

36 个方法落在契约表的 55 个 `function_id` 上（`history_status` 一条命令对应
两个 `function_id`）。剩下 6 个没有 `function_id`：`code_info`、`future_codes`、`option_codes`
（走 `QuerySecuritiesInfo`），以及 `kline` / `snapshot`（走行情通道）、
`etf_pcf`（走 `QueryETFInfo`）——
它们走的是行情与证券信息通道，不走 `QueryThirdInfo` 的具名参数通道。

## 逐条映射

### base

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `code_list` | `A010010011` | `QueryThirdInfo` + `QuerySecuritiesInfo` | `BaseData.get_code_list` | `--type` | CLI 已实现 / 端到端待替身 |
| `code_info` | — | `QuerySecuritiesInfo` | `BaseData.get_code_info` | `--type` | CLI 已实现 / 端到端待替身 |
| `calendar` | `A010061003` | `QueryThirdInfo` | `BaseData.get_calendar` | `--market` | CLI 已实现 / 端到端待替身 |
| `stock_basic` | `A010010001` | `QueryThirdInfo` | `InfoData.get_stock_basic` | `--codes` | CLI 已实现 / 端到端待替身 |
| `future_codes` | — | `QuerySecuritiesInfo` | `BaseData.get_future_code_list` | `--type` | CLI 已实现 / 端到端待替身 |
| `option_codes` | — | `QuerySecuritiesInfo` | `BaseData.get_option_code_list` | `--type` | CLI 已实现 / 端到端待替身 |
| `history_status` | `A010060002` / `A010060005` | `QueryThirdInfo` | `InfoData.get_history_stock_status` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `bj_code_mapping` | `A010010010` | `QueryThirdInfo` | `InfoData.get_bj_code_mapping` | 无 | CLI 已实现 / 端到端待替身 |

### market

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `kline` | — | `QueryKline` | `MarketData.query_kline` | `--codes` `--begin` `--end` `--period` `--adjust` | CLI 已实现 / `--adjust` 公式已与参照对账（见下文 2）/ 端到端待替身 |
| `snapshot` | — | `QuerySnapshot` | `MarketData.query_snapshot` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |

### finance

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `balance_sheet` | `A010050001` | `QueryThirdInfo` | `InfoData.get_balance_sheet` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `income` | `A010050002` | `QueryThirdInfo` | `InfoData.get_income` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `cash_flow` | `A010050003` | `QueryThirdInfo` | `InfoData.get_cash_flow` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `profit_express` | `A010050007` | `QueryThirdInfo` | `InfoData.get_profit_express` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `profit_notice` | `A010050005` | `QueryThirdInfo` | `InfoData.get_profit_notice` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |

### shareholder

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `top10` | `A010040001` | `QueryThirdInfo` | `InfoData.get_share_holder` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `holder_num` | `A010040005` | `QueryThirdInfo` | `InfoData.get_holder_num` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `equity_structure` | `A010010004` | `QueryThirdInfo` | `InfoData.get_equity_structure` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `pledge_freeze` | `A010040004` | `QueryThirdInfo` | `InfoData.get_equity_pledge_freeze` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `restricted` | `A010040003` | `QueryThirdInfo` | `InfoData.get_equity_restricted` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `dividend` | `A010030001` | `QueryThirdInfo` | `InfoData.get_dividend` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `right_issue` | `A010030002` | `QueryThirdInfo` | `InfoData.get_right_issue` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |

### index

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `constituent` | `A010200002` | `QueryThirdInfo` | `InfoData.get_index_constituent` | `--codes` | CLI 已实现 / 端到端待替身 |
| `weight` | `A010200005` | `QueryThirdInfo` | `InfoData.get_index_weight` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `industry_info` | `A010200006` | `QueryThirdInfo` | `InfoData.get_industry_base_info` | 无 | CLI 已实现 / 端到端待替身 |
| `industry_constituent` | `A010200003` | `QueryThirdInfo` | `InfoData.get_industry_constituent` | `--codes` | CLI 已实现 / 端到端待替身 |
| `industry_weight` | `A010200008` | `QueryThirdInfo` | `InfoData.get_industry_weight` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `industry_daily` | `A010200007` | `QueryThirdInfo` | `InfoData.get_industry_daily` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |

### other

| method | function_id | 底层调用 | 业务层入口 | 参数 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `margin_summary` | `A010080002` | `QueryThirdInfo` | `InfoData.get_margin_summary` | `--date` | CLI 已实现 / 端到端待替身 |
| `margin_detail` | `A010080001` | `QueryThirdInfo` | `InfoData.get_margin_detail` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `long_hu_bang` | `A010070002` | `QueryThirdInfo` | `InfoData.get_long_hu_bang` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `block_trading` | `A010070001` | `QueryThirdInfo` | `InfoData.get_block_trading` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `etf_pcf` | — | `QueryETFInfo` | `BaseData.get_etf_pcf` | `--codes` | CLI 已实现 / 端到端待替身 |
| `fund_share` | `A010090002` | `QueryThirdInfo` | `InfoData.get_fund_share` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `fund_iopv` | `A010090003` | `QueryThirdInfo` | `InfoData.get_fund_iopv` | `--codes` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `treasury_yield` | `A010050009` | `QueryThirdInfo` | `InfoData.get_treasury_yield` | `--terms` `--begin` `--end` | CLI 已实现 / 端到端待替身 |
| `option_basic_info` | `A010500001` | `QueryThirdInfo` | `InfoData.get_option_basic_info` | `--codes` | CLI 已实现 / 端到端待替身 |
| `kzz_issuance` | `A010400008` | `QueryThirdInfo` | `InfoData.get_kzz_issuance` | `--codes` | CLI 已实现 / 端到端待替身 |
| `kzz_conv` | `A010400003` | `QueryThirdInfo` | `InfoData.get_kzz_conv` | `--codes` | CLI 已实现 / 端到端待替身 |

## 映射的证据来源

`function_id` 全部来自抓包实证；业务层入口和底层调用来自
`build/AmazingData-1.1.15-cp313-none-any` 下各 `.pyc` 的实测：

- **方法名与签名**：解出 `BaseData` / `MarketData` / `InfoData` 的 code object，
  读 `co_varnames` 得到形参表。
- **底层调用**：扫各函数的 `LOAD_GLOBAL tgw` 后的 `LOAD_ATTR`，直接读出
  `QueryThirdInfo` / `QueryKline` / `QuerySnapshot` / `QuerySecuritiesInfo` /
  `QueryETFInfo`。
- **能不能传 `--begin/--end`**：读 `co_flags & CO_VARKEYWORDS`。`get_stock_basic`、
  `get_bj_code_mapping`、`get_industry_base_info`、`get_index_constituent`、
  `get_index_constituent` 之外的 `get_kzz_*` / `get_option_*` 都没有 `**kwargs`，
  所以这些方法的参数表里没有 `--begin/--end`，硬传会 `TypeError`。
- **`MarketData` 要交易日历**：`MarketData.__init__(self, calendar)` 形参确认为
  `calendar`，CLI 用 `BaseData().get_calendar("str", "SH", today)` 造一份
  （该方法返回排好序的交易日整数列表，见 `base_data.pyc` `get_calendar` 第 53 行
  `list(trade_days_df['TRADE_DAYS'].sort_values(ascending=True))`）。

## 三个没有 function_id 的方法

| method | 为什么没有 | 证据 |
| --- | --- | --- |
| `kline` | 走行情 K 线通道，不经 `QueryThirdInfo` 的具名参数通道 | `market_data.pyc` 里 `query_kline` 只碰 `tgw.MarketType` 和 `tgw.ReqKline` |
| `snapshot` | 走快照通道 | `market_data.pyc` 里 `query_snapshot` 只碰 `tgw.ReqDefault` / `tgw.MarketType` |
| `etf_pcf` | 走 ETF 申赎成分通道 | `base_data.pyc` 里 `get_etf_pcf` 调 `tgw.SubCodeTableItem` + `tgw.QueryETFInfo` |

`code_info` / `future_codes` / `option_codes` 同样没有 `function_id`：
它们复用 `A010010011` 拉到的代码表，只是换用 `QuerySecuritiesInfo` 补字段。

## 待验证与需要注意的三处

### 1. 业务层 `login()` 失败会污染 stdout 并返回 0

`tgw_login.pyc` 的 `login` 失败分支是 `print('login fail')` + `exit(0)`
（第 424-458 行与第 500-534 行两处），成功分支是 `print('login success')`。

后果有两条，都得在 CLI 层挡：报错信息会进 stdout 破坏 JSON-only 的约定；
退出码 0 会让登录失败在 shell 里看起来像成功。

CLI 的处理：调 `login()` 时把 stdout 用 `contextlib.redirect_stdout` 整个转到
stderr，并把 `SystemExit` 当作登录失败转成 `RuntimeFailure`（退出码 3）。
`main()` 的网络层兜底也统一带「与参数无关」的字样，避免解析失败和链路失败
在调用方眼里混成一类。

### 2. `--adjust` 复权

业务层 `query_kline` 不吃 adjust 参数——`market_data.pyc` 全文没有任何 `adjust`
字段。复权因子走 `BaseData.get_backward_factor`：`ReqGetExFactor` 回的是 5 列
CSV，其中 `cum_factor` 是阶梯累积值，业务层落进 HDF5 的也是它（宽表
`{code: Series}`，索引 = 交易日）。前复权就是同一张表除以末值，不需要再取一张
`get_adj_factor` 表——那张表落的是什么值仍未验证。

CLI 的处理照参照实现的复权公式（= 参照
`~/AmazingData/cli.py:127`）：`open/high/low/close` 四个价格列都乘
（`volume` / `amount` 不动），`qfq` 除 `factor.iloc[-1]`、`hfq` 直接用，
结果 `round(6)`。

任何一行做不出复权（缺因子、整只没有因子），缺省整次失败：退出码 3，stdout
为空，stderr 列出代码与日期。`--allow-partial` 才交部分结果——剔除缺因子的行和
整只做不出来的代码，不保留未复权价（2026-10-18 改；原先缺因子的行原样保留
未复权价、退出码 0）。命令行缺省不读本地缓存，因子每次现取。

与参照实现的端到端对账：
`1410.89 × (8.446992 / 8.646881) = 1378.274611`，与参照 CLI 的 stdout 逐位相等，
这条数字钉在 `tests/test_output_shapes.py::test_前复权除末值且价格对得上`。

SKILL.md 提到「ETF 等无复权因子的品种自动降级」，这条降级路径还没实现——
`get_backward_factor` 对 ETF 返回什么，取决于服务端。

### 3. `snapshot` 的返回形状

**与参照实现的形状不同是有意的，别拿参照输出当 oracle 对账。**

参照吐的是 `{date: {code: "<pandas repr 文本>"}}`——内层是一段被 pandas 自己
截断过的字符串（中间夹着 ` ... `），内容不可用。成因是参照 `to_json` 的 dict
分支写错了：

```python
r[k] = json.loads(to_json(v)) if isinstance(obj, pd.DataFrame) else v
#                                                          ^^^^ 应该是 v
```

判的是外层 `obj`（一个 dict），恒为 False，于是 DataFrame 原样落进
`json.dumps(..., default=str)`，被 `str()` 成 repr 文本。

我们拍平成 `{code: [records...]}`（`_flatten_by_code`），符合 SKILL.md，
日期那一层压进 record（非 RangeIndex 的索引先 `reset_index` 落成列），日期不丢。

`query_snapshot` 的返回值**没有**错误表：字节码实测
`build/AmazingData-1.1.15-cp313-none-any/AmazingData/query_api/market_data.pyc`
的 `query_snapshot`，`result` 由 `BUILD_MAP 0`（offset 844）建出，函数体内
`STORE_SUBSCR` 写的是 `result[date][code]`，末尾只有一条
`self.lock.release(); return result`（`RETURN_VALUE` @ offset 1946），
全函数零个 `BUILD_TUPLE`。所以 CLI 侧不再拆 2-tuple。

## 与 SKILL.md 的两处有意偏离

| 项 | SKILL.md | 本实现 | 原因 |
| --- | --- | --- | --- |
| `future_codes` 的 `--type` | 示例写 `--type ZJ_FUTURE` | 默认值设成 `ZJ_FUTURE` | 通用默认是 `EXTRA_STOCK_A`，用 A 股类型查期货代码表没有意义。显式传 `--type` 的行为不变 |
| `--period` / `--adjust` | 未标注是否必填 | 只有 `market kline` 收这两个参数 | 其它方法没有 K 线周期概念，传了也是被忽略的噪音。argparse 对每个方法只挂它真收的参数 |

## 与参照实现的三处有意偏离

输出口径**按 SKILL.md，不按参照实现**。参照对 16 个没有代码维度的方法吐裸数组
`[records...]`，我们一律分组成 `{code: [records...]}`——record 里本来就带着代码，
摆在键上比让调用方再扫一遍划算。逐方法的分组列登记在
`src/tgwpy/cli.py` 的 `MethodSpec.group_column` 上：

| 自然键 | 方法 |
| --- | --- |
| `MARKET_CODE` | `stock_basic` `profit_express` `profit_notice` `top10` `holder_num` `equity_structure` `dividend` `right_issue` `long_hu_bang` `block_trading` `option_basic_info` `kzz_issuance` `kzz_conv` |
| `NEW_CODE` | `bj_code_mapping`（没有 `MARKET_CODE`，新旧代码映射的自然键是新股代码） |
| `INDEX_CODE` | `industry_info`（行业分类表，本质是按行业指数组织的） |
| `EXCHANGE` | `margin_summary`（全市场融资融券汇总，没有代码维度，按交易所分比按代码分合理） |

| 项 | 参照 | 本实现 | 原因 |
| --- | --- | --- | --- |
| 上述 16 个方法的顶层形状 | 裸数组 `[records...]` | `{code: [records...]}` | SKILL.md 约定的是后者。参照没做分组是因为它直接把业务层返回的 DataFrame 序列化，不是设计选择 |
| 分组键的形态 | 不适用 | 列值原样，`600519.SH` 不裁成 `600519` | 参照在本就返回 `{code: [...]}` 的 16 个方法里，键也是带后缀的（`600519.SH` / `000300.SH` / `801030.SI`），裁了跟它们对不上；补后缀则要先猜交易所 |
| `snapshot` | `{date: {code: "<repr 文本>"}}` | `{code: [records...]}` | 参照的 `to_json` 有 bug，内层 DataFrame 被 `str()` 成截断的 repr 文本，不可用。详见上文第 3 节 |

`index weight` / `industry_weight` 的**体积**不对齐参照：参照的这两个方法不吃
`--begin` / `--end`，走 SDK 内部的全历史区间，单次 336 MB / 130 MB。
我们的 CLI 有日期参数，对齐形状即可，别对齐体积。

`tests/test_output_shapes.py` 把这三条钉住：分组结果拆开拼回去必须逐条等于
参照的裸数组，分组键的形态单独测，`snapshot` 的拍平单独测。
