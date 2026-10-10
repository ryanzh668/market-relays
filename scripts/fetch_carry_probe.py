#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carry-desk 可达性探针（只读公共接口，不带任何密钥）。

在 Actions 出口 IP 上把各源/各备选路线的 HTTP 码记下来，写 data/carry/probe.json
（覆盖写当次结果）并追加 data/carry/probe_log.jsonl。任何一条失败都不抛，探针本身永远 0 退出。
"""
import datetime as dt
import json
import os
import socket
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("CARRY_OUT") or os.path.join(ROOT, "data", "carry")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

ZIP = "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-09.zip"
ROUTES = [
    # (源, 路线名, method, url, body)
    ("binance", "fapi.binance.com premiumIndex", "GET", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("binance", "fapi.binance.com fundingRate", "GET", "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=5", None),
    ("binance", "fapi1.binance.com premiumIndex", "GET", "https://fapi1.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("binance", "fapi2.binance.com premiumIndex", "GET", "https://fapi2.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("binance", "fapi3.binance.com premiumIndex", "GET", "https://fapi3.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("binance", "www.binance.com/fapi premiumIndex", "GET", "https://www.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("binance", "www.binance.com bapi fundingRate", "GET", "https://www.binance.com/bapi/futures/v1/public/future/common/get-funding-rate-history?symbol=BTCUSDT", None),
    ("binance", "api-gcp.binance.com spot ping", "GET", "https://api-gcp.binance.com/api/v3/ping", None),
    ("binance", "data-api.binance.vision spot ticker", "GET", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", None),
    ("binance", "data.binance.vision fundingRate zip", "GET", "https://data.binance.vision" + ZIP, None),
    ("binance", "data.binance.vision markPriceKlines zip", "GET", "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/BTCUSDT/1h/BTCUSDT-1h-2025-09.zip", None),
    ("binance", "fapi.binance.us (US 站无合约)", "GET", "https://api.binance.us/api/v3/ping", None),
    ("bybit", "api.bybit.com tickers linear", "GET", "https://api.bybit.com/v5/market/tickers?category=linear", None),
    ("bybit", "api.bytick.com tickers linear", "GET", "https://api.bytick.com/v5/market/tickers?category=linear", None),
    ("bybit", "api.bybit.com funding history", "GET", "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=5", None),
    ("hyperliquid", "api.hyperliquid.xyz info meta", "POST", "https://api.hyperliquid.xyz/info", {"type": "meta"}),
    ("okx", "www.okx.com funding-rate", "GET", "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", None),
    ("deribit", "www.deribit.com index", "GET", "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd", None),
]


def probe(method, url, body, timeout=20):
    t0 = time.time()
    data = None
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    code, err, nbytes, head = None, None, None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            raw = r.read()
            nbytes = len(raw)
            head = raw[:160].decode("utf-8", "replace") if not url.endswith(".zip") else "zip"
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            head = e.read()[:160].decode("utf-8", "replace")
        except Exception:
            pass
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        code = 0
        err = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
    return {"http": code, "err": err, "bytes": nbytes, "head": head,
            "ms": int((time.time() - t0) * 1000)}


def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    where = os.environ.get("CARRY_WHERE") or ("actions" if os.environ.get("GITHUB_ACTIONS") else "local")
    res = []
    for src, name, m, url, body in ROUTES:
        r = probe(m, url, body)
        r.update({"src": src, "route": name, "url": url})
        res.append(r)
        print("%-12s %-42s %s %s" % (src, name, r["http"], r["err"] or ""))
    os.makedirs(OUT, exist_ok=True)
    doc = {"probed_at": now, "where": where, "results": res}
    with open(os.path.join(OUT, "probe_%s.json" % where), "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "probe_log.jsonl"), "a") as f:
        f.write(json.dumps({"probed_at": now, "where": where,
                            "codes": {r["route"]: r["http"] for r in res}}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
