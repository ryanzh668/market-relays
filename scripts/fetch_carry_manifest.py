#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 data/carry/manifest.json：每个产物文件的 sha256 与行数，供本机 sync_relay.py 只拉变化的文件。"""
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data", "carry")
files = {}
for dp, _dn, fns in os.walk(D):
    for fn in fns:
        if not fn.endswith(".jsonl") or "_progress" in dp:
            continue
        p = os.path.join(dp, fn)
        rel = os.path.relpath(p, D)
        b = open(p, "rb").read()
        files[rel] = {"sha256": hashlib.sha256(b).hexdigest(), "lines": b.count(b"\n")}
json.dump({"files": files}, open(os.path.join(D, "manifest.json"), "w"), indent=0, sort_keys=True)
print("manifest: %d files" % len(files))
