#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance USDⓈ-M 永续采集器（中继脚本：Actions 与本机同一份代码）。只读公共接口。

路线（docs/A_README.md 有 HTTP 码）：
  * 合约：www.binance.com/fapi/v1/*（Actions 200；fapi.binance.com 对美 IP 451）
  * 现货：data-api.binance.vision/api/v3/*（官方行情镜像，Actions 200），备 www.binance.com/api/v3
  * 历史：data.binance.vision（S3 列表取全部曾上市符号含退市者；daily metrics zip 取历史 OI）

用法：
  python3 collect/binance.py forward                 # 快照 + 近 26h 已结算费率 + 当日池（00 点那一跑）
  python3 collect/binance.py backfill [--since 2025-01-01] [--only funding,pool,klines] [--symbols A,B]
环境变量 CARRY_OUT=<dir> → 中继模式（写文件），否则直接入 ledger/。
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

EX = "binance"
FAPI_BASES = [os.environ.get("BINANCE_FAPI") or "https://www.binance.com", "https://fapi.binance.com"]
SPOT_BASES = [os.environ.get("BINANCE_SPOT") or "https://data-api.binance.vision",
              "https://www.binance.com", "https://api.binance.com"]
VISION = "https://data.binance.vision"
S3_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
QUOTES = ("USDT", "USDC")
_base_cache = {}


def _get(kind, path, params=None, min_interval=0.05, bucket=None):
    """在候选域名上依次尝试；记住第一个成功的。"""
    bases = FAPI_BASES if kind == "fapi" else SPOT_BASES
    if kind in _base_cache:
        bases = [_base_cache[kind]] + [b for b in bases if b != _base_cache[kind]]
    last = None
    for b in bases:
        try:
            r = C.http(b + path, params, bucket=bucket or (kind + b), min_interval=min_interval)
            _base_cache[kind] = b
            return r
        except C.HttpError as e:
            last = e
            if e.code == 400:  # 参数错误，换域名也没用
                raise
    raise last


# ---------------------------------------------------------------- 元数据
def meta_path(sink):
    if sink.relay:
        return os.path.join(sink.w.out, EX, "meta.json")
    return os.path.join(C.LEDGER, "_meta", "%s.json" % EX)


def load_meta(sink):
    p = meta_path(sink)
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {"spot_listed": {}, "pool_ever": []}


def save_meta(sink, meta):
    p = meta_path(sink)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(p + ".tmp", p)


def perp_universe():
    ei = _get("fapi", "/fapi/v1/exchangeInfo")
    out = {}
    for s in ei.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") not in QUOTES:
            continue
        out[s["symbol"]] = {
            "base": s.get("baseAsset"), "quote": s.get("quoteAsset"), "status": s.get("status"),
            "onboard": s.get("onboardDate"), "delivery": s.get("deliveryDate"),
        }
    return out


def spot_universe():
    ei = _get("spot", "/api/v3/exchangeInfo", min_interval=0.2)
    return dict((s["symbol"], {"base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                               "status": s.get("status")}) for s in ei.get("symbols", []))


def spot_for(base, quote, spots):
    """永续 → 同所现货对。返回 (spot_symbol, mult) 或 (None, 1)。"""
    for b, m in ((base, 1), C.strip_multiplier(base)):
        sym = b + quote
        if sym in spots:
            return sym, m
    return None, 1


def split_symbol(sym):
    for q in QUOTES:
        if sym.endswith(q):
            return sym[:-len(q)], q
    return sym, None


def vision_symbols():
    """data.binance.vision S3 列表：所有曾有 fundingRate 月度包的 UM 符号（含退市者）。失败返回空集。"""
    out, marker = set(), ""
    try:
        for _ in range(20):
            b = C.http(S3_LIST, {"delimiter": "/", "prefix": "data/futures/um/monthly/fundingRate/",
                                 "marker": marker or None}, raw=True)
            txt = b.decode("utf-8")
            syms = re.findall(r"<Prefix>data/futures/um/monthly/fundingRate/([^/<]+)/</Prefix>", txt)
            out.update(syms)
            if "<IsTruncated>true</IsTruncated>" not in txt or not syms:
                break
            marker = "data/futures/um/monthly/fundingRate/%s/" % syms[-1]
    except C.HttpError as e:
        C.log("vision 列表失败", e)
    return set(s for s in out if split_symbol(s)[1])


def funding_info():
    """{symbol: {cap, floor, interval}}；不在表里的符号用默认 8h、上下限未知(None)。"""
    out = {}
    for r in _get("fapi", "/fapi/v1/fundingInfo", min_interval=0.7, bucket="bn_funding"):
        out[r["symbol"]] = {"cap": C.f(r.get("adjustedFundingRateCap")),
                            "floor": C.f(r.get("adjustedFundingRateFloor")),
                            "interval": int(r.get("fundingIntervalHours") or 8)}
    return out


def spot_first_date(spot_sym, meta):
    if spot_sym in meta["spot_listed"]:
        return meta["spot_listed"][spot_sym]
    try:
        k = _get("spot", "/api/v3/klines", {"symbol": spot_sym, "interval": "1d", "startTime": 0,
                                           "limit": 1}, min_interval=0.1)
        d = C.ms_to_date(int(k[0][0])) if k else None
    except C.HttpError:
        d = None
    meta["spot_listed"][spot_sym] = d
    return d


# ---------------------------------------------------------------- 前向
def forward(sink):
    fetched = C.now_ms()
    perps = perp_universe()
    spots = spot_universe()
    finfo = funding_info()
    prem = dict((r["symbol"], r) for r in _get("fapi", "/fapi/v1/premiumIndex"))
    ptick = dict((r["symbol"], r) for r in _get("fapi", "/fapi/v1/ticker/24hr", min_interval=0.5))
    stick = dict((r["symbol"], r) for r in _get("spot", "/api/v3/ticker/24hr", min_interval=1.0))
    live = [s for s, v in perps.items() if v["status"] == "TRADING"]

    def oi(sym):
        r = _get("fapi", "/fapi/v1/openInterest", {"symbol": sym}, min_interval=0.04, bucket="bn_w")
        return C.f(r.get("openInterest"))
    ois, fails = C.pmap(oi, live, workers=6)
    if fails:
        C.log("openInterest 失败 %d 个" % len(fails))

    snaps = []
    for s in live:
        p = prem.get(s, {})
        v = perps[s]
        mark = C.f(p.get("markPrice"))
        spot_sym, mult = spot_for(v["base"], v["quote"], spots)
        spx = C.f(stick.get(spot_sym, {}).get("lastPrice")) if spot_sym else None
        snaps.append(C.mkrow(
            "snapshots", ts=fetched, ex=EX, symbol=s, rate_now=C.f(p.get("lastFundingRate")),
            interval_h=finfo.get(s, {}).get("interval", 8),
            next_funding_ts=int(p["nextFundingTime"]) if p.get("nextFundingTime") else None,
            mark_px=mark, index_px=C.f(p.get("indexPrice")),
            spot_px=spx * mult if spx is not None else None,
            oi_usd=round(ois[s] * mark, 2) if ois.get(s) is not None and mark else None,
            fetched_at=fetched))
    sink.write("snapshots", snaps, ex=EX)

    # 已结算费率：近 26h 全市场（不带 symbol 分页）
    start = fetched - 26 * C.HOUR_MS
    raw, seen = [], set()
    cur = start
    for _ in range(40):
        page = _get("fapi", "/fapi/v1/fundingRate", {"startTime": cur, "limit": 1000},
                    min_interval=0.7, bucket="bn_funding")
        new = [r for r in page if (r["symbol"], r["fundingTime"]) not in seen]
        for r in new:
            seen.add((r["symbol"], r["fundingTime"]))
        raw.extend(new)
        if len(page) < 1000 or not new:
            break
        cur = max(int(r["fundingTime"]) for r in page)
    rows = funding_rows(raw, perps, finfo, "forward", fetched)
    sink.write("funding", rows, ex=EX)

    # 池：每 UTC 日一次
    d = C.ms_to_date(fetched)
    if not sink.has_date("pool", d, EX):
        meta = load_meta(sink)
        pool_rows = build_pool(d, perps, spots, finfo, prem, ptick, stick, ois, meta, fetched)
        sink.write("pool", pool_rows, ex=EX)
        save_meta(sink, meta)
    C.log("binance forward", sink.stats)
    return sink.stats


def funding_rows(raw, perps, finfo, source, fetched):
    by = {}
    for r in raw:
        by.setdefault(r["symbol"], []).append(r)
    out = []
    for s, rs in by.items():
        base, quote = (perps[s]["base"], perps[s]["quote"]) if s in perps else split_symbol(s)
        default = finfo.get(s, {}).get("interval", 8)
        rows = [C.mkrow("funding", ex=EX, symbol=s, base=base, quote=quote,
                        ts=int(r["fundingTime"]), rate=C.f(r["fundingRate"]),
                        interval_h=None, mark_px=C.f(r.get("markPrice")), source=source,
                        fetched_at=fetched) for r in rs]
        C.assign_intervals(rows, default)
        # 首行无前值：用“下一期间隔”代理（同币间隔很少在相邻两期切换），否则用当前 fundingInfo
        if len(rows) >= 2 and rows[0]["interval_h"] == default and rows[1]["interval_h"]:
            rows[0]["interval_h"] = rows[1]["interval_h"]
        out.extend(rows)
    return out


def build_pool(d, perps, spots, finfo, prem, ptick, stick, ois, meta, fetched):
    rows = []
    ever = set(meta.get("pool_ever", []))
    cands = []
    for s, v in perps.items():
        spot_sym, mult = spot_for(v["base"], v["quote"], spots)
        has_spot = bool(spot_sym and spots[spot_sym]["status"] == "TRADING")
        trading = v["status"] == "TRADING"
        if not trading and s not in ever:
            continue
        if not has_spot and s not in ever:
            continue
        mark = C.f(prem.get(s, {}).get("markPrice"))
        oi_usd = round(ois[s] * mark, 2) if ois.get(s) is not None and mark else None
        cands.append((s, v, spot_sym, has_spot, trading, oi_usd))

    def depth(spot_sym):
        b = _get("spot", "/api/v3/depth", {"symbol": spot_sym, "limit": 500}, min_interval=0.6,
                 bucket="bn_spot_w")
        return C.depth_1pct(b.get("bids", []), b.get("asks", []))
    need = [c[2] for c in cands if c[3] and (c[5] or 0) >= 5e6]
    deps, _ = C.pmap(depth, need, workers=2)
    for s, v, spot_sym, has_spot, trading, oi_usd in cands:
        db, da = deps.get(spot_sym, (None, None))
        fi = finfo.get(s, {})
        rows.append(C.mkrow(
            "pool", date=d, ex=EX, symbol=s, spot_symbol=spot_sym, has_spot=has_spot,
            oi_usd=oi_usd if trading else None,
            perp_vol24h_usd=C.f(ptick.get(s, {}).get("quoteVolume")) if trading else None,
            spot_vol24h_usd=C.f(stick.get(spot_sym, {}).get("quoteVolume")) if spot_sym else None,
            depth_bid_1pct_usd=db, depth_ask_1pct_usd=da,
            funding_cap=fi.get("cap"), funding_floor=fi.get("floor"),
            listed_perp_date=C.ms_to_date(int(v["onboard"])) if v.get("onboard") else None,
            listed_spot_date=spot_first_date(spot_sym, meta) if spot_sym else None,
            delisted=not trading, fetched_at=fetched))
        if has_spot and trading and (oi_usd or 0) >= 1e7:
            ever.add(s)
    meta["pool_ever"] = sorted(ever)
    return rows


# ---------------------------------------------------------------- 回填
def backfill_funding(sink, symbols, since_ms, perps, finfo):
    fetched = C.now_ms()
    done = 0
    for s in symbols:
        raw, cur = [], since_ms
        try:
            for _ in range(50):
                page = _get("fapi", "/fapi/v1/fundingRate", {"symbol": s, "startTime": cur,
                                                            "limit": 1000},
                            min_interval=0.7, bucket="bn_funding")
                raw.extend(page)
                if len(page) < 1000:
                    break
                cur = int(page[-1]["fundingTime"]) + 1
        except C.HttpError as e:
            C.write_error(EX, "backfill_funding %s" % s, e)
        rows = funding_rows(raw, perps, finfo, "backfill", fetched)
        sink.write("funding", rows, ex=EX, symbol=s, backfill=True)
        done += 1
        C.progress("binance_funding", done=done, total=len(symbols), last=s, rows=len(rows))


def _daily_klines(kind, sym, since_ms):
    path = "/fapi/v1/klines" if kind == "fapi" else "/api/v3/klines"
    out, cur = {}, since_ms
    for _ in range(5):
        k = _get(kind, path, {"symbol": sym, "interval": "1d", "startTime": cur, "limit": 1000},
                 min_interval=0.15, bucket="bn_k_" + kind)
        for r in k:
            out[C.ms_to_date(int(r[0]))] = C.f(r[7])
        if len(k) < 1000:
            break
        cur = int(k[-1][0]) + 1
    return out


def _metrics_oi(sym, d):
    """data.binance.vision daily metrics：取当日 00:00 的 sum_open_interest_value（USD）。"""
    url = "%s/data/futures/um/daily/metrics/%s/%s-metrics-%s.zip" % (VISION, sym, sym, d)
    try:
        b = C.http(url, raw=True, retries=3, bucket="vision", min_interval=0.02)
    except C.HttpError as e:
        if e.code == 404:
            return None
        raise
    z = zipfile.ZipFile(io.BytesIO(b))
    txt = z.read(z.namelist()[0]).decode("utf-8")
    rd = csv.DictReader(io.StringIO(txt))
    for r in rd:
        return C.f(r.get("sum_open_interest_value"))
    return None


def backfill_pool(sink, symbols, since_ms, perps, spots, meta):
    """历史池：每周一一行（换仓日），OI 来自 vision metrics，成交额来自日 K。深度/上下限历史不可得 → null。"""
    fetched = C.now_ms()
    mondays = []
    t = since_ms
    while t < fetched:
        if (t // C.DAY_MS + 3) % 7 == 0:  # 1970-01-01 是周四
            mondays.append(C.ms_to_date(t))
        t += C.DAY_MS
    ever = set(meta.get("pool_ever", []))
    done = 0
    for s in symbols:
        base, quote = (perps[s]["base"], perps[s]["quote"]) if s in perps else split_symbol(s)
        spot_sym, mult = spot_for(base, quote, spots)
        if not spot_sym:
            continue
        try:
            pk = _daily_klines("fapi", s, since_ms - 2 * C.DAY_MS)
            sk = _daily_klines("spot", spot_sym, since_ms - 2 * C.DAY_MS)
        except C.HttpError as e:
            C.write_error(EX, "backfill_pool klines %s" % s, e)
            continue
        if not pk:
            continue
        listed_spot = spot_first_date(spot_sym, meta)
        onboard = perps.get(s, {}).get("onboard")
        listed_perp = C.ms_to_date(int(onboard)) if onboard else min(pk)
        last_day = max(pk)
        trading = perps.get(s, {}).get("status") == "TRADING"
        days = [d for d in mondays if min(pk) <= d <= last_day]
        prev = lambda d: C.ms_to_date(C.date_to_ms(d) - C.DAY_MS)  # noqa: E731
        ois, fails = C.pmap(lambda d: _metrics_oi(s, d), days, workers=12)
        rows = []
        for d in days:
            pd = prev(d)
            has_spot = pd in sk or d in sk
            rows.append(C.mkrow(
                "pool", date=d, ex=EX, symbol=s, spot_symbol=spot_sym, has_spot=has_spot,
                oi_usd=ois.get(d), perp_vol24h_usd=pk.get(pd), spot_vol24h_usd=sk.get(pd),
                listed_perp_date=listed_perp, listed_spot_date=listed_spot, delisted=False,
                fetched_at=fetched))
            if has_spot and (ois.get(d) or 0) >= 1e7:
                ever.add(s)
        if not trading and last_day < C.ms_to_date(fetched - 2 * C.DAY_MS):
            # 退市：最后一个交易日次日记一行 delisted=true，留在历史池里
            dd = C.ms_to_date(C.date_to_ms(last_day) + C.DAY_MS)
            rows.append(C.mkrow("pool", date=dd, ex=EX, symbol=s, spot_symbol=spot_sym,
                                has_spot=dd in sk, listed_perp_date=listed_perp,
                                listed_spot_date=listed_spot, delisted=True, fetched_at=fetched))
        sink.write("pool", rows, ex=EX, symbol=s, backfill=True)
        done += 1
        C.progress("binance_pool", done=done, total=len(symbols), last=s, rows=len(rows),
                   metrics_fail=len(fails))
    meta["pool_ever"] = sorted(ever)
    save_meta(sink, meta)
    return ever


def backfill_klines(sink, symbols, since_ms):
    done = 0
    for s in symbols:
        rows, cur = [], since_ms
        try:
            for _ in range(40):
                k = _get("fapi", "/fapi/v1/markPriceKlines", {"symbol": s, "interval": "1h",
                                                             "startTime": cur, "limit": 1000},
                         min_interval=0.15, bucket="bn_k_fapi")
                for r in k:
                    rows.append(C.mkrow("klines", ts=int(r[0]), o=C.f(r[1]), h=C.f(r[2]),
                                        l=C.f(r[3]), c=C.f(r[4]), kind="mark"))
                if len(k) < 1000:
                    break
                cur = int(k[-1][0]) + 1
        except C.HttpError as e:
            C.write_error(EX, "backfill_klines %s" % s, e)
        sink.write("klines", rows, ex=EX, symbol=s, backfill=True)
        done += 1
        C.progress("binance_klines", done=done, total=len(symbols), last=s, rows=len(rows))


def backfill(sink, since, only, symbols=None):
    since_ms = C.date_to_ms(since)
    perps = perp_universe()
    spots = spot_universe()
    finfo = funding_info()
    meta = load_meta(sink)
    if symbols:
        universe = list(symbols)
    else:
        allsyms = set(perps) | vision_symbols()
        universe = []
        for s in sorted(allsyms):
            base, quote = (perps[s]["base"], perps[s]["quote"]) if s in perps else split_symbol(s)
            if quote and spot_for(base, quote, spots)[0]:  # 池要求 has_spot：无现货对的永续不进池
                universe.append(s)
    C.log("binance backfill universe=%d" % len(universe))
    if "funding" in only:
        backfill_funding(sink, universe, since_ms, perps, finfo)
    ever = set(meta.get("pool_ever", []))
    if "pool" in only:
        ever = backfill_pool(sink, universe, since_ms, perps, spots, meta)
    if "klines" in only:
        targets = [s for s in universe if s in ever]
        backfill_klines(sink, targets, since_ms)
    C.log("binance backfill done", sink.stats)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forward", "backfill"])
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--only", default="funding,pool,klines")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args(argv)
    sink = C.Sink()
    try:
        if a.mode == "forward":
            forward(sink)
        else:
            backfill(sink, a.since, a.only.split(","), [s for s in a.symbols.split(",") if s])
    except Exception as e:
        C.write_error(EX, a.mode, e)
        raise


if __name__ == "__main__":
    main()
