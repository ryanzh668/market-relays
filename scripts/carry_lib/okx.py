#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OKX USDT/USDC 线性永续采集器（本机直连；同一份代码也可在 Actions 跑）。只读公共接口。

请求风格沿用 ~/note/fable-trader/scripts/okx.py（urllib、重试、code!='0' 视为失败）。
公共限速（官方）：funding-rate-history / history-mark-price-candles 10 次/2s，rubik 5 次/2s，
history-candles 20 次/2s，books 40 次/2s —— 下面的 min_interval 按此取保守值。

历史资金费：API 只给近 3 个月 → 更早的走官方历史数据包
  static.okx.com/cdn/okex/traderecords/swaprates/monthly/YYYYMM/{instId}-fundingrates-YYYY-MM.zip
  （月份按 UTC+8 切，账本按 ts 主键去重，边界重叠无害）。
历史 OI：rubik open-interest-history 日频（period=1D）。

用法：python3 collect/okx.py forward | backfill [--since 2025-01-01] [--only funding,pool,klines] [--symbols X-USDT-SWAP]
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

EX = "okx"
BASE = os.environ.get("OKX_BASE") or "https://www.okx.com"
ZIP = "https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/%s/%s-fundingrates-%s.zip"


class OkxError(Exception):
    pass


def req(path, params=None, rate=0.12, bucket=None):
    body = C.http(BASE + path, params, bucket=bucket or path, min_interval=rate)
    if str(body.get("code")) != "0":
        raise C.HttpError(200, path, "okx code=%s msg=%s" % (body.get("code"), body.get("msg")))
    return body.get("data") or []


def instruments():
    out = {}
    for r in req("/api/v5/public/instruments", {"instType": "SWAP"}):
        if r.get("ctType") != "linear" or r.get("settleCcy") not in ("USDT", "USDC"):
            continue
        base, quote = r["uly"].split("-")[0], r["uly"].split("-")[1]
        out[r["instId"]] = {"base": base, "quote": quote, "spot": r["uly"], "state": r.get("state"),
                            "list": int(r["listTime"]) if r.get("listTime") else None,
                            "ctVal": C.f(r.get("ctVal"))}
    return out


def spot_instruments():
    return dict((r["instId"], r) for r in req("/api/v5/public/instruments", {"instType": "SPOT"}))


def _hist_rows(inst, base, quote, raw, source, fetched, default_iv=None):
    rows = []
    for r in raw:
        rate = C.f(r.get("realizedRate")) if r.get("realizedRate") not in (None, "") else C.f(r.get("fundingRate"))
        rows.append(C.mkrow("funding", ex=EX, symbol=inst, base=base, quote=quote,
                            ts=int(r["fundingTime"]), rate=rate, interval_h=None, mark_px=None,
                            source=source, fetched_at=fetched))
    C.assign_intervals(rows, default_iv)
    if len(rows) >= 2 and rows[0]["interval_h"] == default_iv and rows[1]["interval_h"]:
        rows[0]["interval_h"] = rows[1]["interval_h"]
    return rows


def funding_history_api(inst, since_ms=None, limit_pages=20):
    out, after = [], None
    for _ in range(limit_pages):
        page = req("/api/v5/public/funding-rate-history",
                   {"instId": inst, "limit": 400, "after": after}, rate=0.22, bucket="okx_frh")
        if not page:
            break
        out.extend(page)
        after = page[-1]["fundingTime"]
        if since_ms and int(after) <= since_ms:
            break
        if len(page) < 400:
            break
    return [r for r in out if not since_ms or int(r["fundingTime"]) >= since_ms]


def forward(sink):
    fetched = C.now_ms()
    insts = instruments()
    fr = dict((r["instId"], r) for r in req("/api/v5/public/funding-rate", {"instId": "ANY"}))
    oi = dict((r["instId"], C.f(r.get("oiUsd"))) for r in
              req("/api/v5/public/open-interest", {"instType": "SWAP"}))
    mk = dict((r["instId"], C.f(r.get("markPx"))) for r in
              req("/api/v5/public/mark-price", {"instType": "SWAP"}))
    tk = dict((r["instId"], r) for r in req("/api/v5/market/tickers", {"instType": "SWAP"}))
    stk = dict((r["instId"], r) for r in req("/api/v5/market/tickers", {"instType": "SPOT"}))
    idx = {}
    for q in ("USDT", "USDC"):
        for r in req("/api/v5/market/index-tickers", {"quoteCcy": q}):
            idx[r["instId"]] = C.f(r.get("idxPx"))
    live = [i for i, v in insts.items() if v["state"] == "live"]

    snaps = []
    for i in live:
        v, r = insts[i], fr.get(i, {})
        ft, nft = r.get("fundingTime"), r.get("nextFundingTime")
        iv = C.interval_from_diff(int(nft), int(ft)) if ft and nft else None
        spot = stk.get(v["spot"], {})
        snaps.append(C.mkrow("snapshots", ts=fetched, ex=EX, symbol=i, rate_now=C.f(r.get("fundingRate")),
                             interval_h=iv, next_funding_ts=int(ft) if ft else None,
                             mark_px=mk.get(i), index_px=idx.get(v["spot"]),
                             spot_px=C.f(spot.get("last")), oi_usd=oi.get(i), fetched_at=fetched))
    sink.write("snapshots", snaps, ex=EX)

    # 已结算费率：每个永续拉最近 limit=6 条（覆盖 1h 间隔下 6h 窗口，4h 跑一次足够）
    def hist(i):
        page = req("/api/v5/public/funding-rate-history", {"instId": i, "limit": 8},
                   rate=0.22, bucket="okx_frh")
        r = fr.get(i, {})
        ft, nft = r.get("fundingTime"), r.get("nextFundingTime")
        iv = C.interval_from_diff(int(nft), int(ft)) if ft and nft else None
        return _hist_rows(i, insts[i]["base"], insts[i]["quote"], page, "forward", fetched, iv)
    res, fails = C.pmap(hist, live, workers=4)
    for i, e in fails.items():
        C.write_error(EX, "forward funding %s" % i, e)
    sink.write("funding", [r for rows in res.values() for r in rows], ex=EX)

    d = C.ms_to_date(fetched)
    if not sink.has_date("pool", d, EX):
        meta = load_meta(sink)
        ever = set(meta.get("pool_ever", []))
        rows = []
        need = [i for i in insts if insts[i]["spot"] in stk and (oi.get(i) or 0) >= 5e6]

        def book(i):
            b = req("/api/v5/market/books", {"instId": insts[i]["spot"], "sz": 400}, rate=0.06,
                    bucket="okx_books")[0]
            return C.depth_1pct([x[:2] for x in b["bids"]], [x[:2] for x in b["asks"]])
        deps, _ = C.pmap(book, need, workers=4)
        for i, v in insts.items():
            has_spot = v["spot"] in stk
            trading = v["state"] == "live"
            if not (has_spot or i in ever):
                continue
            r, t, s = fr.get(i, {}), tk.get(i, {}), stk.get(v["spot"], {})
            last = C.f(t.get("last"))
            pv = C.f(t.get("volCcy24h"))
            db, da = deps.get(i, (None, None))
            rows.append(C.mkrow(
                "pool", date=d, ex=EX, symbol=i, spot_symbol=v["spot"] if has_spot else None,
                has_spot=has_spot, oi_usd=oi.get(i) if trading else None,
                perp_vol24h_usd=round(pv * last, 2) if pv is not None and last else None,
                spot_vol24h_usd=C.f(s.get("volCcy24h")) if has_spot else None,
                depth_bid_1pct_usd=db, depth_ask_1pct_usd=da,
                funding_cap=C.f(r.get("maxFundingRate")), funding_floor=C.f(r.get("minFundingRate")),
                listed_perp_date=C.ms_to_date(v["list"]) if v["list"] else None,
                listed_spot_date=spot_listed(v["spot"], meta) if has_spot else None,
                delisted=not trading, fetched_at=fetched))
            if has_spot and trading and (oi.get(i) or 0) >= 1e7:
                ever.add(i)
        # 曾入池、如今已不在 instruments 列表（下架）→ delisted 行留在历史池里
        for i in sorted(ever - set(insts)):
            base, quote = i.split("-")[0], i.split("-")[1]
            rows.append(C.mkrow("pool", date=d, ex=EX, symbol=i, spot_symbol=base + "-" + quote,
                                has_spot=(base + "-" + quote) in stk, delisted=True, fetched_at=fetched))
        meta["pool_ever"] = sorted(ever)
        sink.write("pool", rows, ex=EX)
        save_meta(sink, meta)
    C.log("okx forward", sink.stats)
    return sink.stats


def meta_path(sink):
    if sink.relay:
        return os.path.join(sink.w.out, EX, "meta.json")
    return os.path.join(C.LEDGER, "_meta", "%s.json" % EX)


def load_meta(sink):
    p = meta_path(sink)
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {"pool_ever": [], "spot_listed": {}}


def save_meta(sink, meta):
    p = meta_path(sink)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(p + ".tmp", p)


_spot_inst = None


def spot_listed(spot, meta):
    """现货上市日：SPOT instruments 的 listTime（OKX 有此字段；为空时记 None）。"""
    global _spot_inst
    sl = meta.setdefault("spot_listed", {})
    if spot in sl:
        return sl[spot]
    if _spot_inst is None:
        _spot_inst = spot_instruments()
    lt = _spot_inst.get(spot, {}).get("listTime")
    sl[spot] = C.ms_to_date(int(lt)) if lt else None
    return sl[spot]


# ---------------------------------------------------------------- 回填
def _months(since_ms, until_ms):
    y, m = int(C.ms_to_date(since_ms)[:4]), int(C.ms_to_date(since_ms)[5:7])
    out = []
    while True:
        tag = "%04d-%02d" % (y, m)
        if C.date_to_ms(tag + "-01") > until_ms:
            break
        out.append(tag)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def funding_zip(inst, tag):
    url = ZIP % (tag.replace("-", ""), inst, tag)
    try:
        b = C.http(url, raw=True, retries=3, bucket="okx_static", min_interval=0.05)
    except C.HttpError as e:
        if e.code in (403, 404):
            return []
        raise
    z = zipfile.ZipFile(io.BytesIO(b))
    txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
    return [{"fundingTime": r["funding_time"], "fundingRate": r["funding_rate"]}
            for r in csv.DictReader(io.StringIO(txt)) if r.get("funding_time")]


def backfill_funding(sink, insts, since_ms):
    fetched = C.now_ms()
    api_from = fetched - 85 * C.DAY_MS
    months = [t for t in _months(since_ms, api_from)]
    for n, i in enumerate(sorted(insts)):
        v = insts[i]
        start = max(since_ms, (v["list"] or since_ms) - 31 * C.DAY_MS)
        raw = []
        ms = [t for t in months if C.date_to_ms(t + "-01") + 32 * C.DAY_MS >= start]
        zres, zf = C.pmap(lambda t: funding_zip(i, t), ms, workers=6)
        for t in ms:
            raw.extend(zres.get(t, []))
        for t, e in zf.items():
            C.write_error(EX, "zip %s %s" % (i, t), e)
        try:
            raw.extend(funding_history_api(i, since_ms))
        except C.HttpError as e:
            C.write_error(EX, "frh %s" % i, e)
        dedup = {}
        for r in raw:
            if int(r["fundingTime"]) >= since_ms:
                dedup[int(r["fundingTime"])] = r
        rows = _hist_rows(i, v["base"], v["quote"], [dedup[k] for k in sorted(dedup)], "backfill",
                          fetched, 8)
        sink.write("funding", rows, ex=EX, symbol=i, backfill=True)
        C.progress("okx_funding", done=n + 1, total=len(insts), last=i, rows=len(rows))


def _paged(path, params, ts_from, rate, bucket, col=0):
    out, after = [], None
    for _ in range(60):
        p = dict(params)
        if after:
            p["after"] = after
        page = req(path, p, rate=rate, bucket=bucket)
        if not page:
            break
        out.extend(page)
        after = page[-1][col]
        if int(after) <= ts_from:
            break
    return [r for r in out if int(r[col]) >= ts_from]


def backfill_pool(sink, insts, since_ms, meta):
    fetched = C.now_ms()
    ever = set(meta.get("pool_ever", []))
    spot_all = spot_instruments()
    for n, i in enumerate(sorted(insts)):
        v = insts[i]
        if v["spot"] not in spot_all:
            continue
        try:
            ois = _paged("/api/v5/rubik/stat/contracts/open-interest-history",
                         {"instId": i, "period": "1D", "limit": 100}, since_ms, 0.45, "okx_rubik")
            pk = _paged("/api/v5/market/history-candles", {"instId": i, "bar": "1Dutc", "limit": 100},
                        since_ms - 2 * C.DAY_MS, 0.12, "okx_hc")
            sk = _paged("/api/v5/market/history-candles",
                        {"instId": v["spot"], "bar": "1Dutc", "limit": 100}, since_ms - 2 * C.DAY_MS,
                        0.12, "okx_hc")
        except C.HttpError as e:
            C.write_error(EX, "backfill_pool %s" % i, e)
            continue
        pvol = dict((C.ms_to_date(int(r[0])), C.f(r[7])) for r in pk)
        svol = dict((C.ms_to_date(int(r[0])), C.f(r[7])) for r in sk)
        rows = []
        for r in ois:
            d = C.ms_to_date(int(r[0]))
            pd = C.ms_to_date(int(r[0]) - C.DAY_MS)
            oi_usd = C.f(r[3])
            has_spot = pd in svol or d in svol
            rows.append(C.mkrow(
                "pool", date=d, ex=EX, symbol=i, spot_symbol=v["spot"], has_spot=has_spot,
                oi_usd=oi_usd, perp_vol24h_usd=pvol.get(pd), spot_vol24h_usd=svol.get(pd),
                listed_perp_date=C.ms_to_date(v["list"]) if v["list"] else None,
                listed_spot_date=spot_listed(v["spot"], meta), delisted=False, fetched_at=fetched))
            if has_spot and (oi_usd or 0) >= 1e7:
                ever.add(i)
        sink.write("pool", rows, ex=EX, symbol=i, backfill=True)
        C.progress("okx_pool", done=n + 1, total=len(insts), last=i, rows=len(rows))
    meta["pool_ever"] = sorted(ever)
    save_meta(sink, meta)
    return ever


def backfill_klines(sink, insts, since_ms):
    for n, i in enumerate(sorted(insts)):
        try:
            k = _paged("/api/v5/market/history-mark-price-candles", {"instId": i, "bar": "1H",
                                                                     "limit": 100},
                       since_ms, 0.22, "okx_mark", col=0)
        except C.HttpError as e:
            C.write_error(EX, "backfill_klines %s" % i, e)
            k = []
        rows = [C.mkrow("klines", ts=int(r[0]), o=C.f(r[1]), h=C.f(r[2]), l=C.f(r[3]),
                        c=C.f(r[4]), kind="mark")
                for r in sorted(k, key=lambda r: int(r[0])) if len(r) < 6 or r[5] == "1"]
        sink.write("klines", rows, ex=EX, symbol=i, backfill=True)
        C.progress("okx_klines", done=n + 1, total=len(insts), last=i, rows=len(rows))


def backfill(sink, since, only, symbols=None):
    since_ms = C.date_to_ms(since)
    insts = instruments()
    if symbols:
        insts = dict((k, v) for k, v in insts.items() if k in symbols)
    spot_all = spot_instruments()
    # 池要求 has_spot → 资金费回填也只取有同所现货对的永续（无现货的永不进池）
    insts = dict((k, v) for k, v in insts.items() if v["spot"] in spot_all)
    meta = load_meta(sink)
    C.log("okx backfill insts=%d" % len(insts))
    if "funding" in only:
        backfill_funding(sink, insts, since_ms)
    ever = set(meta.get("pool_ever", []))
    if "pool" in only:
        ever = backfill_pool(sink, insts, since_ms, meta)
    if "klines" in only:
        backfill_klines(sink, dict((k, v) for k, v in insts.items() if k in ever), since_ms)
    C.log("okx backfill done", sink.stats)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forward", "backfill", "basis", "basis-backfill"])
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--only", default="funding,pool,klines")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args(argv)
    sink = C.Sink()
    try:
        if a.mode == "forward":
            forward(sink)
        elif a.mode == "basis":
            forward_basis(sink)
        elif a.mode == "basis-backfill":
            backfill_basis(sink, a.since)
        else:
            backfill(sink, a.since, a.only.split(","), [s for s in a.symbols.split(",") if s])
    except Exception as e:
        C.write_error(EX, a.mode, e)
        raise


# ---------------------------------------------------------------- 交割基差（OKX BTC/ETH 全部交割合约）
def _futures():
    out = []
    for fam in ("BTC-USD", "BTC-USDT", "ETH-USD", "ETH-USDT"):
        for r in req("/api/v5/public/instruments", {"instType": "FUTURES", "instFamily": fam}):
            out.append({"inst": r["instId"], "und": fam.split("-")[0], "uly": fam,
                        "exp": int(r["expTime"]), "list": int(r["listTime"]) if r.get("listTime") else None})
    return out


def forward_basis(sink):
    import deribit as D
    fetched = C.now_ms()
    futs = _futures()
    mk = dict((r["instId"], C.f(r.get("markPx"))) for r in
              req("/api/v5/public/mark-price", {"instType": "FUTURES"}))
    idx = {}
    for q in ("USD", "USDT"):
        for r in req("/api/v5/market/index-tickers", {"quoteCcy": q}):
            idx[r["instId"]] = C.f(r.get("idxPx"))
    rows = [D.basis_row(fetched, EX, f_["und"], f_["inst"], f_["exp"], mk.get(f_["inst"]),
                        idx.get(f_["uly"]), fetched) for f_ in futs]
    sink.write("basis", rows, ex=EX)
    return sink.stats


def backfill_basis(sink, since):
    """只能回填**仍在上市**的交割合约（OKX 已交割合约的 K 线接口返回 50047）[待验·无路：已到期 OKX 合约]。"""
    import deribit as D
    since_ms = C.date_to_ms(since)
    fetched = C.now_ms()
    spot = {}
    for fam in ("BTC-USD", "BTC-USDT", "ETH-USD", "ETH-USDT"):
        spot[fam] = D.okx_index_4h(fam, since_ms)
    for f_ in _futures():
        start = max(since_ms, f_["list"] or since_ms)
        try:
            k = _paged("/api/v5/market/history-mark-price-candles", {"instId": f_["inst"], "bar": "4H",
                                                                     "limit": 100},
                       start, 0.22, "okx_mark")
        except C.HttpError as e:
            C.write_error(EX, "basis %s" % f_["inst"], e)
            continue
        rows = []
        for r in k:
            t = int(r[0])
            s = spot[f_["uly"]].get(t)
            if s and t % (4 * C.HOUR_MS) == 0:
                rows.append(D.basis_row(t, EX, f_["und"], f_["inst"], f_["exp"], C.f(r[1]), s, fetched))
        sink.write("basis", rows, ex=EX, symbol=f_["inst"], backfill=True)


if __name__ == "__main__":
    main()
