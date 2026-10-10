#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bybit 线性永续采集器（中继脚本）。只读公共 v5 接口。

现状 [待验·无路]（10-10 实测，见 docs/A_README.md）：本机 403；Actions 上 api.bybit.com /
api.bytick.com / api.bybit.nl / .eu / -tr.com / byhkbit.com 全部 CloudFront 403（对美 IP 地理封锁），
api.bybit.kz 403，www.bybit.com/x-api 403。本文件保留完整实现：一旦某域名可达（例如换非美出口），
设 BYBIT_BASE=<域名> 即可运行；否则每次运行只记一条错误账并以非零退出（workflow 中 continue-on-error）。

用法：python3 collect/bybit.py forward | backfill [--since 2025-01-01]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

EX = "bybit"
BASES = [b for b in [os.environ.get("BYBIT_BASE"), "https://api.bybit.com", "https://api.bytick.com"] if b]


def _get(path, params=None):
    last = None
    for b in BASES:
        try:
            r = C.http(b + path, params, retries=1, bucket="bybit", min_interval=0.1)
            if r.get("retCode") != 0:
                raise C.HttpError(200, b + path, str(r.get("retMsg")))
            return r["result"]
        except C.HttpError as e:
            last = e
    raise last


def forward(sink):
    fetched = C.now_ms()
    tick = _get("/v5/market/tickers", {"category": "linear"})["list"]
    instr = dict((i["symbol"], i) for i in
                 _get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})["list"])
    snaps, fund = [], []
    for t in tick:
        s = t["symbol"]
        ins = instr.get(s, {})
        if ins.get("contractType") != "LinearPerpetual":
            continue
        ivh = int(ins.get("fundingInterval") or 480) // 60
        mark = C.f(t.get("markPrice"))
        snaps.append(C.mkrow("snapshots", ts=fetched, ex=EX, symbol=s, rate_now=C.f(t.get("fundingRate")),
                             interval_h=ivh, next_funding_ts=int(t["nextFundingTime"]) if t.get("nextFundingTime") else None,
                             mark_px=mark, index_px=C.f(t.get("indexPrice")),
                             oi_usd=C.f(t.get("openInterestValue")), fetched_at=fetched))
    sink.write("snapshots", snaps, ex=EX)
    sink.write("funding", fund, ex=EX)
    return sink.stats


def backfill(sink, since):
    fetched = C.now_ms()
    since_ms = C.date_to_ms(since)
    instr = _get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})["list"]
    for ins in instr:
        if ins.get("contractType") != "LinearPerpetual":
            continue
        s, raw, end = ins["symbol"], [], fetched
        for _ in range(50):
            page = _get("/v5/market/funding/history", {"category": "linear", "symbol": s,
                                                       "endTime": end, "limit": 200})["list"]
            raw.extend(page)
            if len(page) < 200:
                break
            end = int(page[-1]["fundingRateTimestamp"]) - 1
            if end < since_ms:
                break
        rows = [C.mkrow("funding", ex=EX, symbol=s, base=ins.get("baseCoin"), quote=ins.get("quoteCoin"),
                        ts=int(r["fundingRateTimestamp"]), rate=C.f(r["fundingRate"]), interval_h=None,
                        source="backfill", fetched_at=fetched)
                for r in raw if int(r["fundingRateTimestamp"]) >= since_ms]
        C.assign_intervals(rows, int(ins.get("fundingInterval") or 480) // 60)
        sink.write("funding", rows, ex=EX, symbol=s, backfill=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forward", "backfill"])
    ap.add_argument("--since", default="2025-01-01")
    a = ap.parse_args(argv)
    sink = C.Sink()
    try:
        forward(sink) if a.mode == "forward" else backfill(sink, a.since)
    except Exception as e:
        C.write_error(EX, a.mode, e)
        C.log("bybit 失败(预期中: 无路):", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
