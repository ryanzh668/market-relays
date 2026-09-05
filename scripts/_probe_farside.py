#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断 2:找 farside 全史页。用完即删。"""
import re
import relay_common as R
import fetch_etf_farside as FS

raw = R.http_get("https://farside.co.uk/btc/", timeout=90)
html = raw.decode("utf-8", "replace")
hrefs = sorted(set(re.findall(r'href="([^"]+)"', html)))
print("ANCHORS", len(hrefs))
for h in hrefs:
    if h.startswith("http") and "farside" not in h:
        continue
    print("  A", h[:120])

for u in ["https://farside.co.uk/bitcoin-etf-flow-all-data/",
          "https://farside.co.uk/btc/all-data/",
          "https://farside.co.uk/bitcoin-etf-flow/",
          "https://farside.co.uk/btc-all-data/"]:
    try:
        b = R.http_get(u, timeout=90)
        t = b.decode("utf-8", "replace")
        p = FS._TableParser()
        p.feed(t)
        best, n = FS.pick_table(p.tables)
        print("URL %s -> len=%d tables=%d best_date_rows=%d" % (u, len(t), len(p.tables), n))
    except Exception as e:
        print("URL %s -> %s: %s" % (u, type(e).__name__, e))
