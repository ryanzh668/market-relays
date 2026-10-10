#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deribit BTC/ETH 交割合约基差（本机直连）。只读公共 JSON-RPC over GET。

basis 行（SPEC §2）：fut_px = 合约标记价（前向）/ 该时刻 1h K 线开盘价（回填）；
spot_px = Deribit 指数 btc_usd / eth_usd（前向）/ OKX 指数 BTC-USD 4H K 线开盘价（回填，Deribit 无指数历史接口）；
ann_basis = (fut/spot − 1) × 365 / days_to_expiry，**小数**（0.0638 = 6.38%）。

用法：python3 collect/deribit.py forward | backfill [--since 2025-01-01]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

EX = "deribit"
BASE = os.environ.get("DERIBIT_BASE") or "https://www.deribit.com/api/v2"
UNDERLYINGS = ("BTC", "ETH")
FOUR_H = 4 * C.HOUR_MS


def rpc(method, params=None):
    body = C.http(BASE + "/public/" + method, params, bucket="deribit", min_interval=0.06)
    if "error" in body:
        raise C.HttpError(200, method, str(body["error"]))
    return body["result"]


def basis_row(ts, ex, und, inst, expiry, fut, spot, fetched):
    days = (expiry - ts) / float(C.DAY_MS)
    ann = None
    if fut and spot and days > 0:
        ann = (fut / spot - 1.0) * 365.0 / days
    return C.mkrow("basis", ts=ts, ex=ex, underlying=und, instrument=inst, expiry_ts=expiry,
                   fut_px=fut, spot_px=spot, days_to_expiry=round(days, 4) if days else days,
                   ann_basis=round(ann, 6) if ann is not None else None, fetched_at=fetched)


def forward(sink):
    fetched = C.now_ms()
    ts = fetched // FOUR_H * FOUR_H if os.environ.get("CARRY_ALIGN_4H") else fetched
    rows = []
    for und in UNDERLYINGS:
        idx = rpc("get_index_price", {"index_name": und.lower() + "_usd"})["index_price"]
        exp = dict((i["instrument_name"], int(i["expiration_timestamp"])) for i in
                   rpc("get_instruments", {"currency": und, "kind": "future", "expired": "false"})
                   if i.get("settlement_period") != "perpetual")
        for b in rpc("get_book_summary_by_currency", {"currency": und, "kind": "future"}):
            inst = b["instrument_name"]
            if inst not in exp:
                continue
            rows.append(basis_row(ts, EX, und, inst, exp[inst], C.f(b.get("mark_price")),
                                  C.f(idx), fetched))
    sink.write("basis", rows, ex=EX)
    C.log("deribit forward", sink.stats)
    return sink.stats


def okx_index_4h(inst_id, since_ms):
    """OKX 指数 4H K 线（OKX 4H 按 UTC+8 切，8 是 4 的倍数 → 与 UTC 4h 网格对齐）。返回 {ts: open}。"""
    import okx as O
    out, after = {}, None
    for _ in range(80):
        p = {"instId": inst_id, "bar": "4H", "limit": 100}
        if after:
            p["after"] = after
        page = O.req("/api/v5/market/history-index-candles", p, rate=0.12, bucket="okx_idx")
        if not page:
            break
        for r in page:
            out[int(r[0])] = C.f(r[1])
        after = page[-1][0]
        if int(after) <= since_ms:
            break
    return out


def backfill(sink, since):
    since_ms = C.date_to_ms(since)
    fetched = C.now_ms()
    for und in UNDERLYINGS:
        spot = okx_index_4h(und + "-USD", since_ms)
        insts = []
        for expired in ("true", "false"):
            for i in rpc("get_instruments", {"currency": und, "kind": "future", "expired": expired}):
                if i.get("settlement_period") == "perpetual":
                    continue
                if int(i["expiration_timestamp"]) < since_ms:
                    continue
                insts.append(i)
        for n, i in enumerate(insts):
            inst, expiry = i["instrument_name"], int(i["expiration_timestamp"])
            start = max(since_ms, int(i.get("creation_timestamp") or since_ms))
            end = min(expiry, fetched)
            rows, cur = [], start
            while cur < end:
                stop = min(end, cur + 60 * C.DAY_MS)
                try:
                    r = rpc("get_tradingview_chart_data", {"instrument_name": inst, "resolution": 60,
                                                           "start_timestamp": cur,
                                                           "end_timestamp": stop})
                except C.HttpError as e:
                    C.write_error(EX, "chart %s" % inst, e)
                    break
                for t, o in zip(r.get("ticks", []), r.get("open", [])):
                    t = int(t)
                    if t % FOUR_H == 0 and t < expiry and t in spot:
                        rows.append(basis_row(t, EX, und, inst, expiry, C.f(o), spot[t], fetched))
                cur = stop
            sink.write("basis", rows, ex=EX, symbol=inst, backfill=True)
            C.progress("deribit_basis_%s" % und, done=n + 1, total=len(insts), last=inst,
                       rows=len(rows))
    C.log("deribit backfill done", sink.stats)


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
        raise


if __name__ == "__main__":
    main()
