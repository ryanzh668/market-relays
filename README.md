# market-relays — 公共市场数据的 Actions 中继

## 这个仓库存在的唯一理由

本机对两个公共数据源**够不着**(2026-09-05 实测):

| 源 | 本机结果 | 性质 |
|---|---|---|
| `https://farside.co.uk/…` | HTTP **403** | Cloudflare 挡本机出口 |
| `https://www.cftc.gov/files/dea/history/*` | `SSL_ERROR_SYSCALL`(curl 35) | cftc.gov 与 publicreporting.cftc.gov 两个域名 SSL 全封锁 |

GitHub Actions 的出口 IP 大概率能通。于是:**Actions 每天抓一次 → 提交进本仓 `data/` →
本地经 `raw.githubusercontent.com` 读**。这就是"中继"的全部含义,没有别的功能。

上游需求来自 `~/note/2026-09-05-资金入场管道-研究规划v0.1.md`:
ETF 净流史太短(2024-01 起,D3 只能判未定谳),COT 在 S1 里是**记录在案的盲区**——
两者都要先攒史,攒够了再另立检验增量,**不得混入已冻结的批次**。

## ⚠️ 本仓是 PUBLIC 仓

- 只放**公共市场数据**(任何人打开源站就能看到的东西)。
- **永远不放任何密钥、token、私有数据、个人信息**。两个脚本都不需要凭证,
  workflow 除 `GITHUB_TOKEN`(Actions 自带,仅用于提交本仓 `data/`)外不用任何 secret。
- 加新中继前先自问:这份数据公开出去有没有代价?有 → 不进这个仓。

## 状态:Actions 可达性已实测(2026-09-05)

两个抓取脚本的网络分支在**本机**永远无法验证(见上表),所以"从 Actions 够不够得着"
只能由首跑回答。首跑(run 33982364829)与修复后复跑的结果:

| 脚本 | Actions 可达性 | 首跑结果 |
|---|---|---|
| `fetch_cftc_cot.py` | ✅ **通** | 一次回补 2018–2026 共 718 行,`2018-04-10 → 2026-09-01` |
| `fetch_etf_farside.py` | ✅ **通**(Cloudflare 没挡 Actions 出口) | 首跑**报红**:落地页只有 15 天,被 `min_rows` 门拦下;换全史页后 681 行入库 |

首跑那次报红是**设计内的正确行为**,不是事故:当时脚本假设 `https://farside.co.uk/btc/`
是全史页,实测它只列最近 15 天。门槛拦住了"把 15 天当成全部历史写进库"这件事,
诊断完换成真全史页 `https://farside.co.uk/bitcoin-etf-flow-all-data/`(681 行)。
**教训记在这里:公开页面的"全表"是个假设,不是事实,先量行数再入库。**

解析器分支由 `tests/` 的合成夹具离线覆盖(见下),两个解释器绿。

## 数据产物

### `data/etf_flows_farside.json`

```json
[ {"date": "2024-01-11", "total_net_flow_usd_m": 655.3}, ... ]
```

美国 BTC 现货 ETF **每日合计净流**,单位 US$ 百万(源站口径),按 `date` 升序去重。
取的是 farside 表格最右侧 `Total` 列;各家 ETF 分列不入库(需要时再加)。

源按顺序试(`SOURCES`):

1. `https://farside.co.uk/bitcoin-etf-flow-all-data/` — 全史(2026-09-05 实测 681 行,
   2024-01-11 起),门槛 `min_rows=300`;
2. `https://farside.co.uk/btc/` — **只有最近 15 天**,门槛 `min_rows=10`,仅作降级源;
   一旦用到它,error 账里会记一条"降级"告警(库存不会因此变短,但当天没有历史修订)。

全史页每次跑都是全量重解析 + 合并,源站的历史修订会被覆盖为最新值。

### `data/cot_btc.json`

```json
[ {"market": "BITCOIN - CHICAGO MERCANTILE EXCHANGE", "report_date": "2026-09-01",
   "open_interest": 30150, "dealer_long": 1100, "dealer_short": 4200,
   "asset_mgr_long": 9800, "asset_mgr_short": 1050,
   "lev_money_long": 6400, "lev_money_short": 18900}, ... ]
```

CFTC **Traders in Financial Futures (TFF)** 周频报告里的 CME 比特币期货持仓。
- 年度归档:`fut_fin_txt_<YYYY>.zip` → 内含 `FinFutYY.txt`(带表头的 CSV,官方定长格式)。
- **首跑回补 2018–今年**(CME BTC 期货 2017-12 上市),之后日跑只刷当年那份。
- 列按**表头名**取,不按列号(CFTC 历年插过列,写死列号必错)。映射见脚本头部注释。
- 筛选条件"市场名含 BITCOIN 且含 CHICAGO MERCANTILE"会同时命中
  `BITCOIN` / `MICRO BITCOIN` / `BITCOIN FRIDAY` 等多个合约,
  因此每条记录**保留 `market` 字段,去重键 =(report_date, market)**。
  下游做时间序列时**必须先按 market 过滤**,否则同一周会拿到好几条。

### `data/*.error.json`

抓取或解析失败时,**数据文件一个字节都不动**,失败原因追加进
`data/<name>.error.json`(append-only 数组,`{date, ts_utc, error}`),脚本退出码 1。

刻意**不**把错误对象写进数据文件本身:数据文件的契约是"只含真实观测",
往里塞 `{date, error}` 会让下游把一次抓取失败当成一条读数。宁可某天缺一行,
也不写一个猜的数——这是本项目的硬纪律。

## 消费方式(本地)

```bash
curl -s https://raw.githubusercontent.com/ryanzh668/market-relays/main/data/etf_flows_farside.json
curl -s https://raw.githubusercontent.com/ryanzh668/market-relays/main/data/cot_btc.json
```

```python
import json, urllib.request
URL = "https://raw.githubusercontent.com/ryanzh668/market-relays/main/data/etf_flows_farside.json"
rows = json.load(urllib.request.urlopen(URL, timeout=30))
```

`raw.githubusercontent.com` 有 CDN 缓存(约 5 分钟),刚提交的数据不一定立刻可见。
消费方**必须自己检查最新日期**:中继断更时文件仍然存在、仍然可读、只是不再变新——
静默陈旧比报错更危险。

## 流水线

`.github/workflows/relay.yml`:每日 **22:45 UTC** cron,另可 `workflow_dispatch` 手动跑。

- 两个源**互不连坐**:每步 `continue-on-error: true`,最后 `Gate` 步汇总——
  **两个都失败才让 run 红**;只挂一个则打 `::warning::`,run 保持绿
  (那个源就此断更,原因在 error 账里)。
- `data/` 有变化才提交;无变化不产生空提交。

## 测试

```bash
/usr/bin/python3          tests/test_parsers.py     # 3.9
/opt/homebrew/bin/python3 tests/test_parsers.py     # 3.14
```

27 个用例,全离线。夹具一律带 `_SYNTHETIC` 后缀:

- `tests/fixtures/farside_btc_SYNTHETIC.html` — 手造的 farside 式表格(含诱饵导航表、
  三行表头(Total/代码/费率)、括号负数、`-` 空值、千分位逗号、表尾 Total/Average/Maximum/Minimum 汇总行)
- `tests/fixtures/finfut_SYNTHETIC.txt` — 手造的 TFF 式 CSV(真实列名,含非 BTC 行、
  非 CME 的 BTC 行、`.` 缺失值)

**这些夹具不是真实市场数据,数值没有任何研究含义**,只用来钉住"结构一变就红"。
覆盖的失败路径:无表格(比如被 WAF 换成挑战页)、日行过少(全史页降级成短表)、残缺行占比过高、
TFF 缺必需列、只有表头、日期解析不了、zip 里没有 .txt。

依赖:**stdlib only**(`urllib` / `html.parser` / `csv` / `zipfile` / `json`)。
