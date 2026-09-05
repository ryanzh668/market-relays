#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断:把 farside 页面真实表格结构打进 Actions 日志。用完即删。"""
import sys
import relay_common as R
import fetch_etf_farside as FS

raw = R.http_get(FS.URL, timeout=90)
html = raw.decode("utf-8", "replace")
print("LEN", len(html))
p = FS._TableParser()
p.feed(html)
print("TABLES", len(p.tables))
for i, t in enumerate(p.tables):
    nd = sum(1 for r in t if r and FS.parse_date_cell(r[0]) is not None)
    widths = sorted(set(len(r) for r in t))
    print("== table %d: rows=%d date_rows=%d widths=%s" % (i, len(t), nd, widths[:8]))
    for r in t[:4]:
        print("   HEAD", [c[:18] for c in r[:16]])
    for r in t[-3:]:
        print("   TAIL", [c[:18] for c in r[:16]])
