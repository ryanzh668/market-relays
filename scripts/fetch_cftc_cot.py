#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CFTC COT — Traders in Financial Futures (TFF) — CME 比特币期货持仓中继抓取。

源:https://www.cftc.gov/files/dea/history/fut_fin_txt_<YYYY>.zip
   (年度归档 zip,内含一个 FinFutYY.txt,带表头的定长 CSV,官方文档格式)
本机状态:cftc.gov 与 publicreporting.cftc.gov **两个域名 SSL 全封锁**
        (curl: SSL_ERROR_SYSCALL)→ 只能在 GitHub Actions 里跑。
        网络分支 [未经实测];解析器分支由 tests/ 合成 fixture 覆盖。

列映射(按**表头名**取,不按列号 —— CFTC 历年会插列,写死列号必错;
表头名缺失即抛异常,宁可红也不猜):
  Market_and_Exchange_Names                → market
  Report_Date_as_MM_DD_YYYY /
  Report_Date_as_YYYY-MM-DD                → report_date  (统一归一到 YYYY-MM-DD)
  Open_Interest_All                        → open_interest
  Dealer_Positions_Long_All  / _Short_All  → dealer_long / dealer_short
  Asset_Mgr_Positions_Long_All / _Short_All→ asset_mgr_long / asset_mgr_short
  Lev_Money_Positions_Long_All / _Short_All→ lev_money_long / lev_money_short

行筛选:市场名同时含 "BITCOIN" 与 "CHICAGO MERCANTILE"
      —— 注意这会同时命中 "BITCOIN"、"MICRO BITCOIN"、"BITCOIN FRIDAY" 等多个 CME 合约,
      因此每条记录**保留 market 字段,去重键 = (report_date, market)**。
      (只按 report_date 去重会让同一周的多个合约互相覆盖,静默丢数据。)

产出:data/cot_btc.json = [{market, report_date, open_interest, dealer_long, ...}, ...]
     按 (report_date, market) 升序。
失败:data/cot_btc.error.json 追加一条,退出码 1,数据文件不动。
"""
import datetime as dt
import io
import os
import sys
import zipfile

import relay_common as R

URL_TPL = "https://www.cftc.gov/files/dea/history/fut_fin_txt_%d.zip"
NAME = "cot_btc"
OUT = os.path.join(R.DATA, NAME + ".json")

BACKFILL_FROM = 2018      # 首跑回补起始年(CME BTC 期货 2017-12 上市)

MARKET_MUST_CONTAIN = ("BITCOIN", "CHICAGO MERCANTILE")

# 字段名 → 该字段在 TFF 表头里可能用过的名字(按优先级)
COLMAP = [
    ("market",          ["Market_and_Exchange_Names"]),
    ("report_date",     ["Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY"]),
    ("open_interest",   ["Open_Interest_All"]),
    ("dealer_long",     ["Dealer_Positions_Long_All"]),
    ("dealer_short",    ["Dealer_Positions_Short_All"]),
    ("asset_mgr_long",  ["Asset_Mgr_Positions_Long_All"]),
    ("asset_mgr_short", ["Asset_Mgr_Positions_Short_All"]),
    ("lev_money_long",  ["Lev_Money_Positions_Long_All"]),
    ("lev_money_short", ["Lev_Money_Positions_Short_All"]),
]
INT_FIELDS = [f for f, _ in COLMAP if f not in ("market", "report_date")]

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m/%d/%y")


def norm_header(h):
    return h.replace("﻿", "").strip().strip('"').strip()


def build_index(header_row):
    """表头行 → {字段名: 列号}。任一必需字段找不到 → RuntimeError。"""
    names = [norm_header(h) for h in header_row]
    lower = {n.lower(): i for i, n in enumerate(names)}
    idx = {}
    missing = []
    for field, candidates in COLMAP:
        for cand in candidates:
            if cand.lower() in lower:
                idx[field] = lower[cand.lower()]
                break
        else:
            missing.append("%s(候选 %s)" % (field, "/".join(candidates)))
    if missing:
        raise RuntimeError("TFF 表头缺列:%s;实际表头前 20 列=%s"
                           % ("; ".join(missing), names[:20]))
    return idx


def parse_date(s):
    s = s.strip().strip('"').strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError("无法解析报告日期:%r" % s)


def parse_int(s):
    """TFF 数值:可能带引号/逗号;"." 与空 = 缺失(CFTC 的缺失记号)→ None。"""
    s = s.strip().strip('"').strip().replace(",", "")
    if s in ("", "."):
        return None
    return int(float(s))


def parse_tff_text(text):
    """FinFutYY.txt 文本 → 命中的比特币行列表(未去重,原始顺序)。"""
    import csv
    rdr = csv.reader(io.StringIO(text))
    try:
        header = next(rdr)
    except StopIteration:
        raise RuntimeError("TFF 文件为空")
    idx = build_index(header)

    rows = []
    n_data = 0
    for raw in rdr:
        if not raw or len(raw) <= max(idx.values()):
            continue
        n_data += 1
        market = raw[idx["market"]].strip().strip('"').strip()
        up = market.upper()
        if not all(tok in up for tok in MARKET_MUST_CONTAIN):
            continue
        rec = {"market": market,
               "report_date": parse_date(raw[idx["report_date"]]).isoformat()}
        for f in INT_FIELDS:
            rec[f] = parse_int(raw[idx[f]])
        rows.append(rec)
    if n_data == 0:
        raise RuntimeError("TFF 文件只有表头没有数据行")
    return rows


def read_zip_txt(blob):
    """年度 zip → 内含 .txt 的文本。找不到 .txt 抛异常。"""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    txts = [n for n in zf.namelist() if n.lower().endswith(".txt")]
    if not txts:
        raise RuntimeError("zip 里没有 .txt(内容=%s)" % zf.namelist())
    data = zf.read(sorted(txts)[0])
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def rec_key(r):
    return (r["report_date"], r["market"])


def main():
    old = R.load_json(OUT, [])
    if not isinstance(old, list):
        sys.stderr.write("[FAIL] %s 不是数组,拒绝覆盖\n" % OUT)
        return 1

    this_year = dt.datetime.now(dt.timezone.utc).year
    if old:
        years = [this_year]                                   # 常规日跑:只刷当年
    else:
        years = list(range(BACKFILL_FROM, this_year + 1))      # 首跑:回补全史
        sys.stdout.write("[cot] 首跑,回补 %d–%d\n" % (years[0], years[-1]))

    fetched, errors = [], []
    for y in years:
        url = URL_TPL % y
        try:
            blob = R.http_get(url, timeout=120)
            rows = parse_tff_text(read_zip_txt(blob))
            sys.stdout.write("[cot] %d: %d 条比特币行\n" % (y, len(rows)))
            fetched.extend(rows)
        except Exception as e:                                 # noqa: BLE001
            errors.append("%d %s: %s" % (y, type(e).__name__, e))
            sys.stderr.write("[cot] %d 失败:%s: %s\n" % (y, type(e).__name__, e))

    # 当年那一份必须成功;回补年份失败只警告(旧年份 zip 偶有 404/改名)
    cur_failed = any(e.startswith("%d " % this_year) for e in errors)
    if cur_failed or not fetched:
        msg = "; ".join(errors) or "无任何比特币行(市场名筛选 %s 落空)" % (MARKET_MUST_CONTAIN,)
        path = R.record_error(NAME, msg)
        sys.stderr.write("[FAIL] CFTC 抓取/解析失败,已记 %s\n       %s\n" % (path, msg))
        return 1

    merged, added, updated = R.merge_by_key(old, fetched, rec_key)
    R.write_json(OUT, merged)
    if errors:
        R.record_error(NAME, "部分回补年份失败(当年成功,已入库):" + "; ".join(errors))
    sys.stdout.write("[OK] cot: 本次 %d 行,新增 %d,修订 %d,库存 %d(%s → %s)\n"
                     % (len(fetched), added, updated, len(merged),
                        merged[0]["report_date"], merged[-1]["report_date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
