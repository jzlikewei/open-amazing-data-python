# open-amazing-data

星耀数智 AmazingData SDK 的自研替代实现。纯 Python，能在 arm macOS 上原生跑，
不需要厂商的 wheel、原生库或业务层代码。

与中国银河证券、星耀数智无隶属、授权或背书。许可证是 MIT。
账号自备。仓库不带凭据、厂商发行物、手册、行情数据。

两样东西：

- `OpenAmazingData`：与厂商 AmazingData 1.1.15 同一套查询接口
  （`BaseData` / `InfoData` / `MarketData`，类名、方法名、参数位置、返回形状、
  本地缓存格式都一样），原来的调用代码把 `import AmazingData as ad` 换成
  `import OpenAmazingData as ad` 就能用。厂商代码里的缺陷这里修正了，
  逐条见 [`docs/vendor-differences.md`](docs/vendor-differences.md)。
- `tgwpy`：协议层，加一个 39 个方法的命令行。

---

## 装

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a && . ./.env && set +a
```

四个环境变量：`TGW_HOST`、`TGW_PORT`、`TGW_USER`、`TGW_PASSWORD`。

网关的 CA 证书随仓发布在 `src/tgwpy/cert/dgw-ca.crt`（只有一张自签证书，
没有私钥，有效期到 2031-04-20）。服务端换证或证书过期时，用 `TGW_CA_CERT`
指向新证书。

## 当 SDK 用

```python
import sys; sys.path.insert(0, "src")
import os
import OpenAmazingData as ad

ad.login(os.environ["TGW_USER"], os.environ["TGW_PASSWORD"],
         os.environ["TGW_HOST"], os.environ["TGW_PORT"])

info = ad.InfoData()
income = info.get_income(["600519.SH"], is_local=False)      # {代码: DataFrame}

base = ad.BaseData()
calendar = base.get_calendar()                               # 交易日列表
md = ad.MarketData(calendar)
kline = md.query_kline(["600519.SH"], 20260417, 20260422, ad.constant.Period.day.value)
```

本地缓存默认落在 `~/.amazingdata_local/`，用 `TGW_LOCAL_DATA_ROOT` 改
（必须以斜杠结尾）。缓存是 HDF5，文件名、key 与厂商一致，两边可以互读。

交易日历按 3 年一段往回拼，服务端单次只回 1000 行，一次查到底会被截断。
往回拼到哪年用 `TGW_CALENDAR_FIRST_DAY` 改（默认 `19900101`），
设得越近请求越少，但更早的日期区间就查不到了。

## 命令行

```bash
.venv/bin/python -m src.tgwpy.cli market kline \
    --codes 600519.SH --begin 20260417 --end 20260422 \
    --period day --adjust qfq
```

stdout 只有 JSON。退出码：`0` 成功，`2` 参数错，`3` 凭据、登录或网络失败，
`4` 本地缺件（比如 `TGW_CA_CERT` 指向的证书不存在），`5` 网关不可用、已停手。

6 类，39 个方法：`base`、`market`、`finance`、`shareholder`、`index`、`other`。

这 5 个服务端不回数据，`status=-82`：

- `base.code_list`
- `base.code_info`
- `base.future_codes`
- `base.option_codes`
- `other.etf_pcf`

代码表不要靠它们。

## 批量

```bash
.venv/bin/python -m src.tgwpy.batch_run market kline \
    --codes-file hs_codes.txt --begin 20260930 --end 20260930 \
    --period day --adjust none \
    --out ./out/hs_daily.jsonl --state ./out/hs_daily.state.json
```

中断后用同一个 `--state` 再跑会跳过已完成的批次。

节流在 `src/tgwpy/guard.py`，用环境变量改：
`TGW_LOCK_ROOT`、`TGW_MIN_INTERVAL`、`TGW_BACKOFF_BASE`、
`TGW_BACKOFF_MAX`、`TGW_MAX_FAILURES`。

批量打网关必须节流：累计一百多次请求后网关会回 HTTP 404，持续约两小时。
账号同时只允许一个会话，两个进程同时登录会互相踢线。
