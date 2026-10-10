#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carry-desk 中继入口：binance。真正的代码在 scripts/carry_lib/binance.py，
它是 ~/note/套利策略/collect/binance.py 的逐字拷贝（由 collect/publish_relay.sh 同步，勿在此处改）。

MODE=collect（默认）→ forward，写 data/carry/binance/{table}/{date}.jsonl，随后 commit；
MODE=backfill        → backfill，写 backfill_out/（由 workflow 上传为 artifact，不进仓库）。
只读公共接口，无密钥。
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "carry_lib"))
MODE = os.environ.get("MODE", "collect")
DATA = os.path.join(ROOT, "data", "carry")
if MODE == "backfill":
    os.environ["CARRY_OUT"] = os.path.join(ROOT, "backfill_out")
else:
    os.environ["CARRY_OUT"] = DATA

import binance as mod  # noqa: E402

args = ["backfill", "--since", os.environ.get("SINCE", "2025-01-01")] if MODE == "backfill" else ["forward"]
if MODE == "backfill" and os.environ.get("ONLY"):
    args += ["--only", os.environ["ONLY"]]
try:
    mod.main(args)
finally:
    if MODE == "backfill":  # 回填得到的元数据（pool_ever、现货上市日）并入前向目录
        src = os.path.join(ROOT, "backfill_out", "binance", "meta.json")
        dst = os.path.join(DATA, "binance", "meta.json")
        if os.path.exists(src):
            new = json.load(open(src))
            old = json.load(open(dst)) if os.path.exists(dst) else {}
            for k, v in old.items():
                if isinstance(v, list):
                    new[k] = sorted(set(new.get(k, [])) | set(v))
                elif isinstance(v, dict):
                    merged = dict(v)
                    merged.update(new.get(k, {}))
                    new[k] = merged
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            json.dump(new, open(dst, "w"), ensure_ascii=False, indent=0, sort_keys=True)
