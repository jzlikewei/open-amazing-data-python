<!-- 本文件由 src/tgwpy/gen_skill_caps.py 从 skill-capabilities.json 生成，
     不要手改。改事实源后重跑那个脚本。 -->

# /amazingdata skill 能力清单与实现缺口

SSOT 是 `skill-capabilities.json`，对外方法契约的表格——
那是用户实际会调的接口面。这张表回答一个问题：
**这 39 个能力里，我们现在能真的跑通几个，剩下几个卡在哪。**

## 状态口径

| 状态 | 数量 | 含义 |
| --- | --- | --- |
| ✅ 已验证 | 34 | 真实网关上该 function_id 回了真实数据，字段与参照实现逐项对得上 |
| 🟡 待网检验证 | 5 | 代码写完并用抓包原文+真业务层验过，但没跑过真实网关 |
| ⚠️ 参数待调 | 0 | 协议通、服务端回业务错误，样例参数不对 |
| 🚧 代码待写 | 0 | 底层 compat 调用还是 stub |
| ❓ 未测过 | 0 | 从未测过 |

「已验证」里只有 **4 个**跑过**完整 CLI 链**并与生产库对账，其余是协议层实测。各方法下带出处的就是这批：

## 汇总

**34 / 39 已验证。**

按阻塞原因分：

| 阻塞面 | 方法数 | 方法 |
| --- | --- | --- |
| 🟡 待网检验证 | 5 | `base.code_list`、`base.code_info`、`base.future_codes`、`base.option_codes`、`other.etf_pcf` |

## 执行清单

**11 / 12 完成。** 这是 cron 的唯一需求来源，Director 每次 tick 只推进最细的未完成层。

| 层 | 项 | 状态 | 解锁 |
| --- | --- | --- | --- |
| `L0` | `L0.1` 实现 compat.QueryKline，接通 market.kline | ✅ | `market.kline` |
| `L0` | `L0.2` 实现 compat.QuerySnapshot，接通 market.snapshot | ✅ | `market.snapshot` |
| `L0` | `L0.3` 实现 compat.QuerySecuritiesInfo | ✅ | `base.code_info`、`base.future_codes`、`base.option_codes`、`base.code_list` |
| `L0` | `L0.4` 实现 compat.QueryETFInfo | ✅ | `other.etf_pcf` |
| `L0` | `L0.5` 定 ReqGetComplete 的触发条件（证据冲突，未定性） | ✅ |  |
| `L0` | `L0.6` 传输层改成单连接单通道 | ✅ |  |
| `L1` | `L1.1` 行业类 3 个：喂对行业指数码 | ✅ | `index.industry_constituent`、`index.industry_weight`、`index.industry_daily` |
| `L1` | `L1.2` option_basic_info：用真期权码重测 | ✅ | `other.option_basic_info` |
| `L1` | `L1.3` equity_structure 与 pledge_freeze：茅台确定有数据却报错 | ✅ | `shareholder.equity_structure`、`shareholder.pledge_freeze` |
| `L1` | `L1.4` restricted / right_issue / long_hu_bang / profit_express：换有数据的标的 | ✅ | `shareholder.restricted`、`shareholder.right_issue`、`other.long_hu_bang`、`finance.profit_express` |
| `L2` | `L2.1` 实测 A010061003 交易日历 | ✅ | `base.calendar` |
| `L1.5` | `L1.5` 验清 SPI 层怎么解读 deliver_response 的 status | ⬜ |  |

### L0 · 传输层：compat 替身落地

compat 里 4 个调用还是 _unimplemented 桩，直接压住 7 个能力。协议参数和响应解码在 2026-10-10 已用 MITM 抓参照 SDK 实测确认，这里只差把桩换成真实现并把行情请求接到 /amd/dgw/dgw 通道。

- [x] **L0.1** 实现 compat.QueryKline，接通 market.kline
  - 门禁：compat.QueryKline 不再调 _unimplemented
  - 门禁：请求走 PATH_MARKET（/amd/dgw/dgw），period_type 用 10100 档
  - 门禁：响应按 decode_kline_rows 还原成 {code: [records...]}
  - 门禁：用真实网关跑通，把命令与输出摘要记进提交信息
  - 门禁：tests/test_skill_contract.py 里 market.kline 的 status 从 impl_stub 转 verified
- [x] **L0.2** 实现 compat.QuerySnapshot，接通 market.snapshot
  - 门禁：compat.QuerySnapshot 不再调 _unimplemented
  - 门禁：begin_time/end_time 用 90000000/170000000，不是 K 线的 900/1700
  - 门禁：多帧按 headers.all_pack_num 收齐（实测单日 4 帧 5432 行）
  - 门禁：真实网关跑通并记录证据
  - 门禁：status 从 impl_stub 转 verified
- [x] **L0.3** 实现 compat.QuerySecuritiesInfo
  - 门禁：先用 MITM 抓参照 SDK 的真实报文确定 method 与 params，不能照 .rodata 猜
  - 门禁：**照 SDK 方法名发会静默失败**：线上是 ReqGetCodeTableList / ReqGetETFCodeTableList
  - 门禁：compat 里两个函数都不再调 _unimplemented
  - 门禁：请求字节与抓包原文逐字段相等，有回归测试钉住
  - 门禁：**若服务端恒回 -82，如实把 4+1 个能力标成 unverified 并写明是服务端侧限制，不许伪造成通过**
- [x] **L0.4** 实现 compat.QueryETFInfo
  - 门禁：先抓参照 SDK 的 ETF 申赎请求确定 method 与 params
  - 门禁：线上是 ReqGetETFCodeTableList，入参是**列表**不是单项（base_data.dis.txt:483-188）
  - 门禁：compat.QueryETFInfo 不再调 _unimplemented
  - 门禁：**若恒回 -82，如实标 unverified 并写明服务端侧限制**
  - 门禁：ETF 的 [信息表,成分表] 分帧规则是推断，需在代码里标明未验证
- [x] **L0.5** 定 ReqGetComplete 的触发条件（证据冲突，未定性）
  - 门禁：写一个最小复现：单连接、连发两个请求，第二个必然带 ReqGetComplete
  - 门禁：去掉它，观察是立刻断连还是延迟断、还是根本不���
  - 门禁：若确为必需：client.query 要在收满一页后补发，并补 offset 翻页
  - 门禁：补测试覆盖「整页响应后续查」这个场景
  - 门禁：结论写进代码注释，并更正任何与之冲突的旧记录
- [x] **L0.6** 传输层改成单连接单通道
  - 门禁：WssTransport 改成单连接，走 PATH_MARKET，登录一次供两族共用
  - 门禁：34 个已实测的资讯 function_id 迁到该通道后仍要全部返回数据
  - 门禁：同一条连接上「行情 → 资讯 → 行情」连打三次都要正常（已实测可行，需回归覆盖）
  - 门禁：wire.py 的 _Channel 抽象按新拓扑收敛，别留双连接的死代码
  - 门禁：补一条测试钉住「只登录一次」这条不变量

### L1 · 参数层：样例参数不对的 10 个

协议通、服务端回业务错误。多数是样例标的选错，少数（如 equity_structure）该数据确定存在却报错，是真参数问题。本层依赖 L0.3 提供 option_codes 的真实码。

- [x] **L1.1** 行业类 3 个：喂对行业指数码
  - 依赖：`L0.3`
  - 门禁：从已验证的 A010200006（industry_info）取真实行业指数码
  - 门禁：三个接口都返回真实数据
  - 门禁：把可用的行业码样例记进提交信息
- [x] **L1.2** option_basic_info：用真期权码重测
  - 依赖：`L0.3`
  - 门禁：样例标的从 600519.SH 换成真实期权合约码
  - 门禁：返回真实数据；仍失败则记为服务端侧限制并写明证据
- [x] **L1.3** equity_structure 与 pledge_freeze：茅台确定有数据却报错
  - 门禁：换多只标的、多个日期区间试，定位是字段名还是取值问题
  - 门禁：若确属服务端限制，附上多标的复现记录再改状态
- [x] **L1.4** restricted / right_issue / long_hu_bang / profit_express：换有数据的标的
  - 门禁：各自选一只确定命中该类数据的标的（配股/限售/龙虎榜/快报）
  - 门禁：返回真实数据；仍失败则记为服务端侧限制并写明证据

### L2 · 补测：从没测过的

base.calendar 的 A010061003 压根不在 verify_function_ids.py 的 54 个样例里。

- [x] **L2.1** 实测 A010061003 交易日历
  - 门禁：把 A010061003 的参数补齐并跑通
  - 门禁：返回开市日期数组，与参照实现输出比对

### L1.5 · 待验证疑点

r4-exfactor 的 agent 从字节码推出「status 命中成功码会记进 error_list」，若成立则复权下载会一直重试后抛异常。但那段反编译读起来是反的（成功码记为错误），说明至少一处解读错了；且实测传 0 时 32 条数据完整回来、没有触发重试。要跑完整 cli.py → 业务层 → compat 链路才验得到，而 66 上只有 python3.10，load 不了 3.13 的 .pyc。

- [ ] **L1.5** 验清 SPI 层怎么解读 deliver_response 的 status
  - 门禁：在能跑 3.13 业务层的机器上跑 `market kline --adjust qfq` 全链路
  - 门禁：确认 BackwardFactorSpi 没有把成功响应记进 error_list
  - 门禁：若确实有问题，按厂商 SPI 的真实约定改 deliver_response 的 status 取值
  - 门禁：改完必须重跑行情/资讯两条已验证路径，不能只验复权

### 通用门禁

- 可运行：CLI 能真实启动并接上真实网关，不是 stub 返回
- 端到端：输入 → 运行 → 结果 → 证据链完整，stdout 只有 JSON
- 禁止伪造：不得用 mock 数据、假成功响应冒充通过
- 验证日志：记录真实跑过的命令与实际输出摘要
- 文档一致：同批更新本文件的 status/gap 与 skill-capabilities.md 视图
- 提交姿态：code-first，纯测试或纯文档批次不作为 checkpoint

## 基础数据 base（8）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `code_list` | 代码表（每日最新） | `--type` | `QueryThirdInfo` + `QuerySecuritiesInfo` | `A010010011` | 🟡 待网检验证 | 协议已按抓包原文实现，请求字节与线上报文逐字段相等（有回归测试）。**但真实网关实测该 method 恒回 status=-82（kFlowOverLimit）**，官方 SDK 发同样报文也回 -82，7 种 security_type 的报文逐字节相同——属服务端侧限制，未取到数据。所以状态是 unverified 而不是 verified。 |
| `code_info` | 证券信息（简称、昨收、涨跌停价） | `--type` | `QuerySecuritiesInfo` | — | 🟡 待网检验证 | 协议已按抓包原文实现，请求字节与线上报文逐字段相等（有回归测试）。**但真实网关实测该 method 恒回 status=-82（kFlowOverLimit）**，官方 SDK 发同样报文也回 -82，7 种 security_type 的报文逐字节相同——属服务端侧限制，未取到数据。所以状态是 unverified 而不是 verified。 |
| `calendar` | 交易日历 | `--market` | `QueryThirdInfo` | `A010061003` | ✅ 已验证 | — |
| `stock_basic` | 上市公司基础信息 | `--codes` | `QueryThirdInfo` | `A010010001` | ✅ 已验证 | — |
| `future_codes` | 期货代码表 | `--type` | `QuerySecuritiesInfo` | — | 🟡 待网检验证 | 协议已按抓包原文实现，请求字节与线上报文逐字段相等（有回归测试）。**但真实网关实测该 method 恒回 status=-82（kFlowOverLimit）**，官方 SDK 发同样报文也回 -82，7 种 security_type 的报文逐字节相同——属服务端侧限制，未取到数据。所以状态是 unverified 而不是 verified。 |
| `option_codes` | 期权代码表 | `--type` | `QuerySecuritiesInfo` | — | 🟡 待网检验证 | 协议已按抓包原文实现，请求字节与线上报文逐字段相等（有回归测试）。**但真实网关实测该 method 恒回 status=-82（kFlowOverLimit）**，官方 SDK 发同样报文也回 -82，7 种 security_type 的报文逐字节相同——属服务端侧限制，未取到数据。所以状态是 unverified 而不是 verified。 |
| `history_status` | 历史证券信息 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010060002` / `A010060005` | ✅ 已验证 | — |
| `bj_code_mapping` | 北交所新旧代码对照 | 无 | `QueryThirdInfo` | `A010010010` | ✅ 已验证 | — |

### 本类里跑过真实网关的

- `calendar` — 2026-10-11 实跑：分段拼出 8738 个交易日（1990-12-19 ~ 2026-10-09），3.2 秒；进程内 + 磁盘两级缓存生效，跨进程不再重拼
- `history_status` — 2026-10-11 实跑：30 只 × 2026-09-30 对生产库，is_st/is_xr/is_suspended/is_withdrawn 四项零差零缺

## 行情数据 market（2）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `kline` | 历史 K 线 | `--codes` `--begin` `--end` `--period` `--adjust` | `QueryKline` | — | ✅ 已验证 | — |
| `snapshot` | 历史快照（Level-1 行情） | `--codes` `--begin` `--end` | `QuerySnapshot` | — | ✅ 已验证 | — |

### 本类里跑过真实网关的

- `kline` — 2026-10-11 实跑：30 只 × 2026-09-30 不复权日线对生产库逐字段 180 值零差；ETF 159915.SZ/510050.SH 12 值零差；`--period min1` 单日 242 根、242 个不同时间戳、09:30 开盘到 15:00 收盘；`--adjust qfq` 600519.SH 2026-04-20 收 1378.274611；分批入口 120 只分 4 批全通，续跑跳过已完成批
- `snapshot` — 2026-10-11 实跑 `market snapshot --codes 600519.SH --begin 20260930`：exit 0，stdout 纯 JSON，35 字段；`pre_close`=1235.58 与库内 2026-09-29 收盘一致，涨跌停 1112.02/1359.14 等于 ±10%

## 财务数据 finance（5）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `balance_sheet` | 资产负债表 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010050001` | ✅ 已验证 | — |
| `income` | 利润表 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010050002` | ✅ 已验证 | — |
| `cash_flow` | 现金流量表 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010050003` | ✅ 已验证 | — |
| `profit_express` | 业绩快报 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010050007` | ✅ 已验证 | — |
| `profit_notice` | 业绩预告 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010050005` | ✅ 已验证 | — |

## 股东股本 shareholder（7）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `top10` | 十大股东数据 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010040001` | ✅ 已验证 | — |
| `holder_num` | 股东户数 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010040005` | ✅ 已验证 | — |
| `equity_structure` | 股本结构（总股本/流通股/限售股） | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010010004` | ✅ 已验证 | — |
| `pledge_freeze` | 股权冻结/质押 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010040004` | ✅ 已验证 | — |
| `restricted` | 限售股解禁 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010040003` | ✅ 已验证 | — |
| `dividend` | 分红数据 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010030001` | ✅ 已验证 | — |
| `right_issue` | 配股数据 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010030002` | ✅ 已验证 | — |

## 指数与行业 index（6）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `constituent` | 指数成分股 | `--codes` | `QueryThirdInfo` | `A010200002` | ✅ 已验证 | — |
| `weight` | 指数成分股日权重 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010200005` | ✅ 已验证 | — |
| `industry_info` | 行业指数基本信息 | 无 | `QueryThirdInfo` | `A010200006` | ✅ 已验证 | — |
| `industry_constituent` | 行业指数成分股 | `--codes` | `QueryThirdInfo` | `A010200003` | ✅ 已验证 | — |
| `industry_weight` | 行业指数成分股日权重 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010200008` | ✅ 已验证 | — |
| `industry_daily` | 行业指数日行情 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010200007` | ✅ 已验证 | — |

## 其他 other（11）

| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `margin_summary` | 融资融券成交汇总 | `--date` | `QueryThirdInfo` | `A010080002` | ✅ 已验证 | — |
| `margin_detail` | 融资融券交易明细 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010080001` | ✅ 已验证 | — |
| `long_hu_bang` | 龙虎榜 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010070002` | ✅ 已验证 | — |
| `block_trading` | 大宗交易 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010070001` | ✅ 已验证 | — |
| `etf_pcf` | ETF 申赎和成分股 | `--codes` | `QueryETFInfo` | — | 🟡 待网检验证 | 协议已按抓包原文实现（线上 method 是 ReqGetETFCodeTableList，不是 QueryETFInfo，照 SDK 名发会静默失败）。**但真实网关实测恒回 status=-82**，属服务端侧限制，未取到数据。另外 [信息表,成分表] 的分帧规则是推断，未验证。 |
| `fund_share` | ETF 基金份额 | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010090002` | ✅ 已验证 | — |
| `fund_iopv` | ETF 收盘 IOPV | `--codes` `--begin` `--end` | `QueryThirdInfo` | `A010090003` | ✅ 已验证 | — |
| `treasury_yield` | 国债收益率 | `--terms` `--begin` `--end` | `QueryThirdInfo` | `A010050009` | ✅ 已验证 | — |
| `option_basic_info` | 期权基本资料 | `--codes` | `QueryThirdInfo` | `A010500001` | ✅ 已验证 | — |
| `kzz_issuance` | 可转债发行数据 | `--codes` | `QueryThirdInfo` | `A010400008` | ✅ 已验证 | — |
| `kzz_conv` | 可转债转股数据 | `--codes` | `QueryThirdInfo` | `A010400003` | ✅ 已验证 | — |

## 输出契约

- `code_list` → `字符串数组`
- `code_info` → `{type: [records...]}`
- 默认 → `{code: [records...]}`
