#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market-relays 共用工具。stdlib only,无第三方依赖。

纪律:
- **绝不伪造**:抓不到 / 解析不出结构 → 抛异常,由调用方写错误账并非零退出;
  数据文件保持上一次的真实内容不动,宁可缺一天也不写猜的数。
- **append-only 错误账**:错误写进 data/<name>.error.json(数组,只追加),
  数据文件本身永远只含真实观测记录,不混入 error 对象(混进去会毒化下游消费者)。
"""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# 数据中心 IP(Actions runner)访问公共站点时,不带 UA 的 urllib 默认 UA
# ("Python-urllib/3.x")会被大量 WAF 直接挡。带一个普通浏览器 UA 是常规做法。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def today_iso():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def http_get(url, timeout=60, headers=None):
    """GET → bytes。失败(含非 200)一律抛异常,不返回空内容。"""
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        if code != 200:
            raise RuntimeError("HTTP %s from %s" % (code, url))
        return resp.read()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return default
    return json.loads(txt)


def write_json(path, obj):
    """原子写:先写 .tmp 再 rename,避免半截文件被 commit 出去。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def merge_by_key(old_rows, new_rows, key):
    """按 key 去重合并(新记录覆盖同 key 旧记录),按 key 升序返回。

    key 可以是字段名(str)或取键函数。返回 (merged, n_added, n_updated)。
    """
    if isinstance(key, str):
        kf = lambda r: r[key]      # noqa: E731
    else:
        kf = key
    idx = {}
    for r in old_rows:
        idx[kf(r)] = r
    added = updated = 0
    for r in new_rows:
        k = kf(r)
        if k not in idx:
            added += 1
        elif idx[k] != r:
            updated += 1
        idx[k] = r
    merged = [idx[k] for k in sorted(idx)]
    return merged, added, updated


def record_error(name, msg):
    """把一条错误追加进 data/<name>.error.json(append-only)。

    刻意**不**写进数据文件:数据文件的契约是"只含真实观测",
    往里塞 {date, error} 会让下游把错误当成一条读数。
    """
    path = os.path.join(DATA, name + ".error.json")
    rows = load_json(path, [])
    if not isinstance(rows, list):
        rows = []
    rows.append({"date": today_iso(),
                 "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "error": str(msg)[:2000]})
    write_json(path, rows)
    return path
