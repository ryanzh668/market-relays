#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hyperliquid 永续采集器（中继脚本：Actions 与本机同一份代码）。只读公共 info 接口（POST）。

路线：api.hyperliquid.xyz/info —— 本机 000（不可达），Actions 200。
限速：info 请求权重 20、每 IP 1200/分 → 节流 1.05s/次。
资金费每小时结算（interval_h=1）；symbol 用 HL 的 coin 名（如 MON），quote=USDC。
has_spot：同所现货代币名 == coin 或 == "U"+coin（Unit 桥接币，如 UBTC）且以 USDC 计价 [推断：映射规则]。
历史 OI 无公共接口 → pool 历史行无法回填 [待验·无路]；pool 只能自前向首日起累积。

用法：python3 collect/hyperliquid.py forward | backfill [--since 2025-01-01] [--only funding,klines,klines4h,daily,spotmeta]
                                    [--coins MON,BTC] [--shard 0/4]
      python3 collect/hyperliquid.py proxy-pool [--v01-dir ~/note/market-relays/data/carry/hyperliquid/v01]
v0.1：回填宇宙 = meta 全部永续（含 isDelisted，不再按 OI/现货筛）；HL 1h K 线只给最近 5000 根，更早用 4h 补到
klines/hyperliquid_4h/；代理池行（proxy=true，oi_usd=null）由 proxy-pool 从账本费率 + 中继 v01/ 产物生成。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

EX = "hyperliquid"
URL = os.environ.get("HL_INFO") or "https://api.hyperliquid.xyz/info"
QUOTE = "USDC"
CAP = 0.04  # HL 资金费上限 4%/h（双向）


def info(body, interval=1.05):
    return C.http(URL, body=body, bucket="hl", min_interval=interval)


def load_state():
    meta, ctxs = info({"type": "metaAndAssetCtxs"})
    spot_meta, spot_ctxs = info({"type": "spotMetaAndAssetCtxs"})
    tokens = dict((t["index"], t["name"]) for t in spot_meta["tokens"])
    sctx = dict((c.get("coin"), c) for c in spot_ctxs)
    spot_by_base = {}
    for u in spot_meta["universe"]:
        b, q = u["tokens"][0], u["tokens"][1]
        if tokens.get(q) != QUOTE:
            continue
        spot_by_base[tokens.get(b)] = u["name"]  # 'PURR/USDC' 或 '@107'
    perps = {}
    for u, c in zip(meta["universe"], ctxs):
        name = u["name"]
        spot = spot_by_base.get(name) or spot_by_base.get("U" + name)
        if not spot:
            stripped, _m = C.strip_multiplier(name)
            spot = spot_by_base.get(stripped)
        mark = C.f(c.get("markPx"))
        oi = C.f(c.get("openInterest"))
        perps[name] = {
            "delisted": bool(u.get("isDelisted")), "spot": spot, "mark": mark,
            "oracle": C.f(c.get("oraclePx")), "funding": C.f(c.get("funding")),
            "oi_usd": round(oi * mark, 2) if oi is not None and mark else None,
            "vol": C.f(c.get("dayNtlVlm")),
            "spot_px": C.f(sctx.get(spot, {}).get("midPx") or sctx.get(spot, {}).get("markPx")) if spot else None,
            "spot_vol": C.f(sctx.get(spot, {}).get("dayNtlVlm")) if spot else None,
        }
    return perps


def funding_hist(coin, start_ms, end_ms=None):
    out, cur = [], start_ms
    for _ in range(200):
        body = {"type": "fundingHistory", "coin": coin, "startTime": cur}
        if end_ms:
            body["endTime"] = end_ms
        page = info(body)
        out.extend(page)
        if len(page) < 500:
            break
        cur = int(page[-1]["time"]) + 1
    return out


def funding_rows(coin, raw, source, fetched):
    rows = [C.mkrow("funding", ex=EX, symbol=coin, base=coin, quote=QUOTE, ts=int(r["time"]),
                    rate=C.f(r["fundingRate"]), interval_h=1, mark_px=None, source=source,
                    fetched_at=fetched) for r in raw]
    return rows


def meta_path(sink):
    if sink.relay:
        return os.path.join(sink.w.out, EX, "meta.json")
    return os.path.join(C.LEDGER, "_meta", "%s.json" % EX)


def load_meta(sink):
    p = meta_path(sink)
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {"pool_ever": [], "first_seen": {}}


def save_meta(sink, meta):
    p = meta_path(sink)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(p + ".tmp", p)


def forward(sink):
    fetched = C.now_ms()
    perps = load_state()
    nxt = (fetched // C.HOUR_MS + 1) * C.HOUR_MS
    snaps = []
    for name, p in perps.items():
        if p["delisted"]:
            continue
        snaps.append(C.mkrow("snapshots", ts=fetched, ex=EX, symbol=name, rate_now=p["funding"],
                             interval_h=1, next_funding_ts=nxt, mark_px=p["mark"],
                             index_px=p["oracle"], spot_px=p["spot_px"], oi_usd=p["oi_usd"],
                             fetched_at=fetched))
    sink.write("snapshots", snaps, ex=EX)

    # 已结算费率：OI ≥ $1M 的币逐个拉近 6h（权重受限，小币只留快照里的 rate_now）
    coins = [n for n, p in perps.items() if not p["delisted"] and (p["oi_usd"] or 0) >= 1e6]
    rows = []
    for c in coins:
        try:
            rows.extend(funding_rows(c, funding_hist(c, fetched - 6 * C.HOUR_MS), "forward", fetched))
        except C.HttpError as e:
            C.write_error(EX, "forward funding %s" % c, e)
    sink.write("funding", rows, ex=EX)

    d = C.ms_to_date(fetched)
    if not sink.has_date("pool", d, EX):
        meta = load_meta(sink)
        ever = set(meta.get("pool_ever", []))
        fs = meta.setdefault("first_seen", {})
        pool = []
        for name, p in perps.items():
            fs.setdefault(name, d)
            has_spot = bool(p["spot"])
            if not (has_spot or (p["oi_usd"] or 0) >= 5e6 or name in ever):
                continue
            db = da = None
            if has_spot and (p["oi_usd"] or 0) >= 5e6:
                try:
                    book = info({"type": "l2Book", "coin": p["spot"]}, interval=0.2)
                    lv = book.get("levels", [[], []])
                    db, da = C.depth_1pct([[x["px"], x["sz"]] for x in lv[0]],
                                          [[x["px"], x["sz"]] for x in lv[1]])
                except C.HttpError as e:
                    C.write_error(EX, "l2Book %s" % p["spot"], e)
            pool.append(C.mkrow(
                "pool", date=d, ex=EX, symbol=name, spot_symbol=p["spot"], has_spot=has_spot,
                oi_usd=p["oi_usd"], perp_vol24h_usd=p["vol"], spot_vol24h_usd=p["spot_vol"],
                depth_bid_1pct_usd=db, depth_ask_1pct_usd=da, funding_cap=CAP,
                funding_floor=-CAP, listed_perp_date=None, listed_spot_date=None,
                delisted=p["delisted"], fetched_at=fetched))
            if has_spot and not p["delisted"] and (p["oi_usd"] or 0) >= 1e7:
                ever.add(name)
        meta["pool_ever"] = sorted(ever)
        sink.write("pool", pool, ex=EX)
        save_meta(sink, meta)
    C.log("hyperliquid forward", sink.stats)
    return sink.stats


def info_w(body, n_items_per_weight=None):
    """按 HL 权重节流：基础 20 + 返回条数/每权重条数；限额 1200/分/IP，留 15% 余量。"""
    r = info(body)
    if n_items_per_weight and isinstance(r, list):
        w = 20 + len(r) / float(n_items_per_weight)
        time.sleep(max(0.0, w / 1200.0 * 60.0 * 1.15 - 1.05))
    return r


def funding_hist_w(coin, start_ms):
    out, cur = [], start_ms
    for _ in range(400):
        page = info_w({"type": "fundingHistory", "coin": coin, "startTime": cur}, 20)
        out.extend(page)
        if len(page) < 500:
            break
        cur = int(page[-1]["time"]) + 1
    return out


def candles(coin, interval, start_ms, end_ms):
    """candleSnapshot 只给最近 5000 根（任何 startTime 都一样）[有据 v0.1 实测见 README]。"""
    return info_w({"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": interval, "startTime": start_ms,
                           "endTime": end_ms}}, 60)


def load_universe():
    """meta 全部永续（含 isDelisted）+ spotMeta 当前列表（USDC 计价对）。"""
    meta = info({"type": "meta"})
    spot = info({"type": "spotMeta"})
    tokens = dict((t["index"], t) for t in spot["tokens"])
    spot_by_base = {}
    for u in spot["universe"]:
        b, q = u["tokens"][0], u["tokens"][1]
        if tokens.get(q, {}).get("name") != QUOTE:
            continue
        spot_by_base[tokens.get(b, {}).get("name")] = (u["name"], b)
    perps = []
    for u in meta["universe"]:
        name = u["name"]
        sp = spot_by_base.get(name) or spot_by_base.get("U" + name)
        if not sp:
            stripped, _m = C.strip_multiplier(name)
            sp = spot_by_base.get(stripped)
        perps.append({"coin": name, "delisted": bool(u.get("isDelisted")),
                      "spot": sp[0] if sp else None, "spot_token": sp[1] if sp else None,
                      "max_lev": u.get("maxLeverage")})
    return perps, spot


def backfill(sink, since, only, coins=None, shard=None):
    """v0.1 全量回填：meta 全部永续（不再按 OI/现货筛）。shard="i/n" 按序号取模分片（Actions 并行）。

    only ⊂ {funding, klines, klines4h, daily, spotmeta}：
      funding  fundingHistory 自 since 起按 500 条翻页，interval_h=1
      klines   1h candleSnapshot（HL 只给最近 5000 根 ≈ 208 天）kind=last（HL 无标记价 K 线）
      klines4h 4h candleSnapshot 补 1h 窗口之前的部分 → klines/hyperliquid_4h/
      daily    1d 永续 + 1d 现货 K 线（日成交额代理、现货首日 = listed_spot_date 代理）+ tokenDetails
      spotmeta 当前 spotMeta 原样 + 映射表
    """
    since_ms = C.date_to_ms(since)
    fetched = C.now_ms()
    perps, spot = load_universe()
    if coins:
        perps = [p for p in perps if p["coin"] in set(coins)]
    if shard:
        i, n = [int(x) for x in shard.split("/")]
        perps = [p for k, p in enumerate(perps) if k % n == i]
    out_dir = os.path.join(sink.w.out, EX, "v01") if sink.relay else os.path.join(C.LEDGER, "_meta", "hl_v01")
    os.makedirs(out_dir, exist_ok=True)
    tag = (shard or "all").replace("/", "of")
    if "spotmeta" in only:
        with open(os.path.join(out_dir, "spot_meta_raw.json"), "w") as fh:
            json.dump({"fetched_at": fetched, "spotMeta": spot}, fh, ensure_ascii=False)
    C.log("hyperliquid backfill v0.1 coins=%d shard=%s only=%s" % (len(perps), shard, only))
    daily = {}
    report = {}
    for k, p in enumerate(perps):
        c = p["coin"]
        rep = report.setdefault(c, {"delisted": p["delisted"], "spot": p["spot"]})
        first_1h = last_1h = None
        if "klines" in only or ("funding" in only and p["delisted"]):
            try:
                k1 = candles(c, "1h", since_ms, fetched)
                rows = [C.mkrow("klines", ts=int(r["t"]), o=C.f(r["o"]), h=C.f(r["h"]),
                                l=C.f(r["l"]), c=C.f(r["c"]), kind="last") for r in k1]
                sink.write("klines", rows, ex=EX, symbol=c, backfill=True)
                first_1h = rows[0]["ts"] if rows else None
                last_1h = rows[-1]["ts"] if rows else None
                rep["k1h_n"] = len(rows)
                rep["k1h_last"] = last_1h
                rep["k1h_first"] = first_1h
            except C.HttpError as e:
                rep["k1h_err"] = str(e)[:200]
                C.write_error(EX, "backfill klines %s" % c, e)
        if "funding" in only:
            try:
                raw = funding_hist_w(c, since_ms)
                rows = funding_rows(c, raw, "backfill", fetched)
                if p["delisted"]:
                    # 已下架币：HL 下架后仍逐小时返回 rate=0.0 的“空结算”直到今天 [有据 FTM 实测]
                    # → 截到最后一根 1h K 线收盘；无 K 线（窗口内未交易）则只留非零行
                    cut = last_1h + C.HOUR_MS if last_1h else None
                    n0 = len(rows)
                    rows = [r for r in rows if (r["ts"] <= cut if cut else r["rate"] != 0)]
                    rep["funding_dropped_after_delist"] = n0 - len(rows)
                sink.write("funding", rows, ex=EX, symbol=c, backfill=True)
                rep["funding_n"] = len(rows)
                rep["funding_first"] = rows[0]["ts"] if rows else None
                rep["funding_last"] = rows[-1]["ts"] if rows else None
            except C.HttpError as e:
                rep["funding_err"] = str(e)[:200]
                C.write_error(EX, "backfill funding %s" % c, e)
        if "klines4h" in only and (first_1h is None or first_1h > since_ms):
            try:
                end = (first_1h or fetched)
                k4 = candles(c, "4h", since_ms, end)
                rows = [C.mkrow("klines", ts=int(r["t"]), o=C.f(r["o"]), h=C.f(r["h"]),
                                l=C.f(r["l"]), c=C.f(r["c"]), kind="last")
                        for r in k4 if int(r["t"]) + 4 * C.HOUR_MS <= end]
                sink.write("klines4h", rows, ex=EX, symbol=c, backfill=True)
                rep["k4h_n"] = len(rows)
                rep["k4h_first"] = rows[0]["ts"] if rows else None
            except C.HttpError as e:
                rep["k4h_err"] = str(e)[:200]
                C.write_error(EX, "backfill klines4h %s" % c, e)
        if "daily" in only:
            d = {"spot": p["spot"], "delisted": p["delisted"]}
            try:
                d["perp_1d"] = [[int(r["t"]), C.f(r["c"]), C.f(r["v"])]
                                for r in candles(c, "1d", 0, fetched)]
            except C.HttpError as e:
                d["perp_1d_err"] = str(e)[:200]
            if p["spot"]:
                try:
                    d["spot_1d"] = [[int(r["t"]), C.f(r["c"]), C.f(r["v"])]
                                    for r in candles(p["spot"], "1d", 0, fetched)]
                except C.HttpError as e:
                    d["spot_1d_err"] = str(e)[:200]
                try:
                    td = info({"type": "tokenDetails",
                               "tokenId": spot["tokens"][p["spot_token"]]["tokenId"]})
                    d["token_deploy_time"] = td.get("deployTime")
                except (C.HttpError, KeyError, IndexError, TypeError) as e:
                    d["token_err"] = str(e)[:200]
            daily[c] = d
        C.progress("hyperliquid_backfill_%s" % tag, done=k + 1, total=len(perps), last=c)
    if daily:
        with open(os.path.join(out_dir, "daily_%s.json" % tag), "w") as fh:
            json.dump(daily, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "report_%s.json" % tag), "w") as fh:
        json.dump({"fetched_at": fetched, "since": since, "coins": report}, fh, ensure_ascii=False,
                  indent=0, sort_keys=True)
    C.log("hyperliquid backfill done", sink.stats)


def load_v01_dir(v01_dir):
    """读中继产物 v01/：spot_meta_raw.json + daily_*.json（各分片合并）。"""
    import glob
    spot = json.load(open(os.path.join(v01_dir, "spot_meta_raw.json")))
    daily = {}
    for fn in sorted(glob.glob(os.path.join(v01_dir, "daily_*.json"))):
        daily.update(json.load(open(fn)))
    return spot, daily


def write_spot_meta(spot, daily, ledger=None):
    """ledger/hl_spot_meta.json：当前现货清单（含 fetched_at）+ 永续↔现货映射 + 现货首日代理。"""
    pairs = []
    for coin, d in sorted(daily.items()):
        if not d.get("spot"):
            continue
        s1 = d.get("spot_1d") or []
        tdt = d.get("token_deploy_time")
        pairs.append({"perp": coin, "spot_symbol": d["spot"],
                      "listed_spot_date": C.ms_to_date(s1[0][0]) if s1 else None,
                      "listed_spot_date_src": "first_spot_1d_candle" if s1 else None,
                      "token_deploy_time": tdt, "perp_delisted": d.get("delisted")})
    out = {"fetched_at": spot["fetched_at"], "fetched_at_iso": C.ms_to_iso(spot["fetched_at"]),
           "note": "spotMeta 只有当前列表；listed_spot_date 用现货 1d K 线首根日期代理 [推断]；"
                   "v0.1 回放按“当前有现货即全程有现货”处理（预注册 §4 已登记偏向）",
           "n_spot_pairs_usdc_mapped_to_perp": len(pairs), "pairs": pairs,
           "spotMeta": spot["spotMeta"]}
    p = os.path.join(ledger or C.LEDGER, "hl_spot_meta.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)
    return out


def proxy_pool_rows(funding_by_sym, daily, forward_first, today, fetched):
    """HL 代理池行（v0.1）：每币每日一行（该日有资金费结算才出行），oi_usd=null，proxy=true。

    funding_by_sym: {coin: sorted list of ts}；forward_first: {coin: 首个前向池日期}（之后不写，免得盖掉真行）。
    has_spot 按 spotMeta 当前列表（全程）；listed_perp_date = 首条费率日期；
    perp/spot_vol24h_usd = 前一日 1d K 线 v×c（近似，无前视）；delisted 只在已下架币的最后一日为 true。
    """
    rows = []
    for coin, tss in sorted(funding_by_sym.items()):
        if not tss:
            continue
        d = daily.get(coin, {})
        spot_sym = d.get("spot")
        pv = dict((C.ms_to_date(t), (c or 0) * (v or 0)) for t, c, v in d.get("perp_1d") or [])
        sv = dict((C.ms_to_date(t), (c or 0) * (v or 0)) for t, c, v in d.get("spot_1d") or [])
        s1 = d.get("spot_1d") or []
        lsd = C.ms_to_date(s1[0][0]) if s1 else None
        lpd = C.ms_to_date(tss[0])
        dates = sorted(set(C.ms_to_date(t) for t in tss))
        last = dates[-1]
        stop = min(forward_first.get(coin, today), today)
        for day in dates:
            if day >= stop:
                continue
            prev = C.ms_to_date(C.date_to_ms(day) - C.DAY_MS)
            rows.append(C.mkrow(
                "pool", date=day, ex=EX, symbol=coin, spot_symbol=spot_sym,
                has_spot=bool(spot_sym), oi_usd=None,
                perp_vol24h_usd=round(pv[prev], 2) if prev in pv else None,
                spot_vol24h_usd=round(sv[prev], 2) if prev in sv else None,
                depth_bid_1pct_usd=None, depth_ask_1pct_usd=None,
                funding_cap=CAP, funding_floor=-CAP, listed_perp_date=lpd, listed_spot_date=lsd,
                delisted=bool(d.get("delisted")) and day == last, fetched_at=fetched, proxy=True))
    return rows


def proxy_pool(v01_dir, ledger=None):
    ledger = ledger or C.LEDGER
    spot, daily = load_v01_dir(v01_dir)
    write_spot_meta(spot, daily, ledger)
    fb = {}
    for r in C.iter_jsonl(C.table_path("funding", ledger=ledger)):
        if r["ex"] == EX:
            fb.setdefault(r["symbol"], set()).add(r["ts"])
    fb = dict((k, sorted(v)) for k, v in fb.items())
    ff = {}
    for r in C.iter_jsonl(C.table_path("pool", ledger=ledger)):
        if r["ex"] == EX and not r.get("proxy"):
            ff[r["symbol"]] = min(ff.get(r["symbol"], "9999"), r["date"])
    rows = proxy_pool_rows(fb, daily, ff, C.today(), C.now_ms())
    n = C.LedgerWriter(ledger).append("pool", rows)
    C.log("hyperliquid proxy-pool 生成 %d 行，新追加 %d 行，币 %d" % (len(rows), n, len(fb)))
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forward", "backfill", "proxy-pool"])
    ap.add_argument("--v01-dir", default=os.path.expanduser("~/note/market-relays/data/carry/hyperliquid/v01"))
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--only", default="funding,klines,klines4h,daily,spotmeta")
    ap.add_argument("--coins", default="")
    ap.add_argument("--shard", default="", help="i/n：按 meta 序号取模分片")
    a = ap.parse_args(argv)
    sink = C.Sink()
    try:
        if a.mode == "forward":
            forward(sink)
        elif a.mode == "proxy-pool":
            proxy_pool(a.v01_dir)
        else:
            backfill(sink, a.since, a.only.split(","), [c for c in a.coins.split(",") if c],
                     a.shard or None)
    except Exception as e:
        C.write_error(EX, a.mode, e)
        raise


if __name__ == "__main__":
    main()
