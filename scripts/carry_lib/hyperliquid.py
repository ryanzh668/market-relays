#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hyperliquid 永续采集器（中继脚本：Actions 与本机同一份代码）。只读公共 info 接口（POST）。

路线：api.hyperliquid.xyz/info —— 本机 000（不可达），Actions 200。
限速：info 请求权重 20、每 IP 1200/分 → 节流 1.05s/次。
资金费每小时结算（interval_h=1）；symbol 用 HL 的 coin 名（如 MON），quote=USDC。
has_spot：同所现货代币名 == coin 或 == "U"+coin（Unit 桥接币，如 UBTC）且以 USDC 计价 [推断：映射规则]。
历史 OI 无公共接口 → pool 历史行无法回填 [待验·无路]；pool 只能自前向首日起累积。

用法：python3 collect/hyperliquid.py forward | backfill [--since 2025-01-01] [--only funding,klines] [--coins MON,BTC]
"""
import argparse
import json
import os
import sys

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


def backfill(sink, since, only, coins=None):
    since_ms = C.date_to_ms(since)
    fetched = C.now_ms()
    perps = load_state()
    if not coins:
        # 回填范围：有同所现货 ∪ 当前 OI ≥ $5M（无历史 OI，无法按历史池筛 → 幸存者偏差 [待验]）
        coins = sorted(n for n, p in perps.items()
                       if p["spot"] or (p["oi_usd"] or 0) >= 5e6)
    C.log("hyperliquid backfill coins=%d" % len(coins))
    for i, c in enumerate(coins):
        if "funding" in only:
            try:
                raw = funding_hist(c, since_ms)
                sink.write("funding", funding_rows(c, raw, "backfill", fetched), ex=EX, symbol=c,
                           backfill=True)
            except C.HttpError as e:
                C.write_error(EX, "backfill funding %s" % c, e)
        if "klines" in only:
            rows, cur = [], since_ms
            try:
                for _ in range(10):
                    k = info({"type": "candleSnapshot",
                              "req": {"coin": c, "interval": "1h", "startTime": cur,
                                      "endTime": fetched}})
                    for r in k:
                        rows.append(C.mkrow("klines", ts=int(r["t"]), o=C.f(r["o"]), h=C.f(r["h"]),
                                            l=C.f(r["l"]), c=C.f(r["c"]), kind="last"))
                    if len(k) < 5000:
                        break
                    cur = int(k[-1]["t"]) + 1
            except C.HttpError as e:
                C.write_error(EX, "backfill klines %s" % c, e)
            sink.write("klines", rows, ex=EX, symbol=c, backfill=True)
        C.progress("hyperliquid_backfill", done=i + 1, total=len(coins), last=c)
    C.log("hyperliquid backfill done", sink.stats)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forward", "backfill"])
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--only", default="funding,klines")
    ap.add_argument("--coins", default="")
    a = ap.parse_args(argv)
    sink = C.Sink()
    try:
        if a.mode == "forward":
            forward(sink)
        else:
            backfill(sink, a.since, a.only.split(","), [c for c in a.coins.split(",") if c])
    except Exception as e:
        C.write_error(EX, a.mode, e)
        raise


if __name__ == "__main__":
    main()
