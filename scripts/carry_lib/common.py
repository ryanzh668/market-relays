#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carry-desk 采集层公共件（Opus-A）。Python 3 标准库，零第三方。

铁律：只读公共接口，不带任何密钥；本文件与所有采集器都不含下单/提现代码路径。

输出两种模式（同一份采集器代码，靠环境变量切换）：
  * 本机模式（默认）：直接追加进 ledger/（SPEC §2 schema），按主键去重。
  * 中继模式（设 CARRY_OUT=<dir>）：写 <dir>/{ex}/{table}/{YYYY-MM-DD}.jsonl（前向），
    或 <dir>/{ex}/backfill/{table}/{key}.jsonl.gz（回填）；由 collect/sync_relay.py 拉回入账。
"""
import gzip
import hashlib
from http.client import HTTPException
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.environ.get("CARRY_LEDGER") or os.path.join(ROOT, "ledger")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 carry-desk/0.1")

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS
BACKFILL_FROM_MS = 1735689600000  # 2025-01-01 00:00 UTC

# ---------------------------------------------------------------- schema（SPEC §2，冻结）
SCHEMA = {
    "funding": ["ex", "symbol", "base", "quote", "ts", "rate", "interval_h", "mark_px",
                "source", "fetched_at"],
    "pool": ["date", "ex", "symbol", "spot_symbol", "has_spot", "oi_usd", "perp_vol24h_usd",
             "spot_vol24h_usd", "depth_bid_1pct_usd", "depth_ask_1pct_usd", "funding_cap",
             "funding_floor", "listed_perp_date", "listed_spot_date", "delisted", "fetched_at"],
    "snapshots": ["ts", "ex", "symbol", "rate_now", "interval_h", "next_funding_ts", "mark_px",
                  "index_px", "spot_px", "oi_usd", "fetched_at"],
    "basis": ["ts", "ex", "underlying", "instrument", "expiry_ts", "fut_px", "spot_px",
              "days_to_expiry", "ann_basis", "fetched_at"],
    "klines": ["ts", "o", "h", "l", "c", "kind"],
}
KEYS = {
    "funding": ("ex", "symbol", "ts"),
    "pool": ("date", "ex", "symbol"),
    "snapshots": ("ts", "ex", "symbol"),
    "basis": ("ts", "ex", "instrument"),
    "klines": ("ts",),
}
# 比较“内容是否相同”时忽略的字段（只差这些就不重复追加）
VOLATILE = {"fetched_at", "source"}
ENUMS = {
    ("funding", "ex"): {"binance", "okx", "bybit", "hyperliquid"},
    ("funding", "source"): {"backfill", "forward"},
    ("basis", "ex"): {"deribit", "okx"},
    ("basis", "underlying"): {"BTC", "ETH"},
    ("klines", "kind"): {"mark", "last"},
}
INT_FIELDS = {"ts", "fetched_at", "next_funding_ts", "expiry_ts"}


class SchemaError(ValueError):
    pass


def mkrow(table, **kw):
    """按 SPEC §2 造一行：缺失字段写 None，不许多字段。"""
    cols = SCHEMA[table]
    extra = set(kw) - set(cols)
    if extra:
        raise SchemaError("%s 多余字段 %s" % (table, sorted(extra)))
    row = dict((c, kw.get(c)) for c in cols)
    if "fetched_at" in cols and row["fetched_at"] is None:
        row["fetched_at"] = now_ms()
    validate(table, row)
    return row


def validate(table, row):
    cols = SCHEMA[table]
    if list(row.keys()) != cols and set(row.keys()) != set(cols):
        raise SchemaError("%s 字段不符: %s" % (table, sorted(set(row) ^ set(cols))))
    for k in KEYS[table]:
        if row.get(k) is None:
            raise SchemaError("%s 主键 %s 为空" % (table, k))
    for k in INT_FIELDS:
        if k in row and row[k] is not None and not isinstance(row[k], int):
            raise SchemaError("%s.%s 必须是整数毫秒: %r" % (table, k, row[k]))
    for (t, k), allowed in ENUMS.items():
        if t == table and row.get(k) is not None and row[k] not in allowed:
            raise SchemaError("%s.%s=%r 不在 %s" % (table, k, row[k], sorted(allowed)))
    if table == "funding":
        if row["interval_h"] is not None and not (0 < row["interval_h"] <= 24):
            raise SchemaError("interval_h 越界 %r" % row["interval_h"])
        if row["rate"] is not None and abs(row["rate"]) > 0.05:
            raise SchemaError("rate 越界(>5%%/期，疑似单位错) %r" % row["rate"])
    return True


def row_key(table, row):
    return tuple(row[k] for k in KEYS[table])


def payload(row):
    return tuple((k, row[k]) for k in sorted(row) if k not in VOLATILE)


# ---------------------------------------------------------------- 时间
def now_ms():
    return int(time.time() * 1000)


def ms_to_date(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def ms_to_iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_to_ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def today():
    return ms_to_date(now_ms())


def f(x):
    """宽松转 float；空串/None/无法解析 → None（禁猜）。"""
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def interval_from_diff(ts, prev_ts, default=None):
    """由相邻两次结算时刻推 interval_h（取整到小时；结算时刻常带几毫秒抖动）。"""
    if prev_ts is None:
        return default
    h = int(round((ts - prev_ts) / float(HOUR_MS)))
    if 1 <= h <= 24:
        return h
    return default


def assign_intervals(rows, default=None):
    """rows: 同一 (ex,symbol) 的 funding 行；按 ts 升序回填 interval_h（已有值不覆盖）。"""
    rows.sort(key=lambda r: r["ts"])
    prev = None
    for r in rows:
        if r.get("interval_h") is None:
            r["interval_h"] = interval_from_diff(r["ts"], prev, default)
        prev = r["ts"]
    return rows


# ---------------------------------------------------------------- HTTP
class HttpError(Exception):
    def __init__(self, code, url, body=""):
        Exception.__init__(self, "HTTP %s %s %s" % (code, url, body[:200]))
        self.code = code
        self.url = url
        self.body = body


_rl_lock = threading.Lock()
_rl_next = {}


def throttle(bucket, min_interval):
    """简单节流：同一 bucket 两次请求至少间隔 min_interval 秒（跨线程）。"""
    if not min_interval:
        return
    while True:
        with _rl_lock:
            t = time.time()
            nxt = _rl_next.get(bucket, 0)
            if t >= nxt:
                _rl_next[bucket] = t + min_interval
                return
            wait = nxt - t
        time.sleep(wait)


def http(url, params=None, body=None, retries=None, timeout=None, bucket=None, min_interval=0,
         raw=False, headers=None):
    """GET（body=None）或 POST JSON。返回解析后的 JSON（raw=True 返回 bytes）。

    网络错误/5xx/429 退避重试；4xx（除 429）立即抛 HttpError（含 code，供路线记录）。
    """
    if params:
        clean = dict((k, str(v)) for k, v in params.items() if v is not None)
        if clean:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    if retries is None:
        retries = int(os.environ.get("CARRY_RETRIES", "5"))
    if timeout is None:
        timeout = float(os.environ.get("CARRY_TIMEOUT", "15"))
    hdrs = {"User-Agent": UA, "Accept": "application/json,*/*"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    last = None
    for i in range(retries + 1):
        throttle(bucket or url.split("/")[2], min_interval)
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                b = resp.read()
                if resp.status != 200:
                    raise HttpError(resp.status, url, b[:200].decode("utf-8", "replace"))
                return b if raw else json.loads(b.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            last = HttpError(e.code, url, txt)
            if e.code not in (418, 429, 500, 502, 503, 504):
                raise last
            time.sleep(min(60, 2 ** i * (5 if e.code in (418, 429) else 1)))
            continue
        except (urllib.error.URLError, socket.timeout, OSError, ValueError, HTTPException) as e:
            last = HttpError(0, url, "%s: %s" % (type(e).__name__, getattr(e, "reason", e)))
        time.sleep(min(30, 0.7 * (2 ** i)))
    raise last


def pmap(fn, items, workers=8):
    """线程池并发；返回 (results dict, fails dict)。"""
    import concurrent.futures
    res, fails = {}, {}
    if not items:
        return res, fails
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = dict((ex.submit(fn, k), k) for k in items)
        for fut in concurrent.futures.as_completed(futs):
            k = futs[fut]
            try:
                res[k] = fut.result()
            except Exception as exc:
                fails[k] = exc
    return res, fails


def log(*a):
    sys.stderr.write("[%s] %s\n" % (ms_to_iso(now_ms()), " ".join(str(x) for x in a)))
    sys.stderr.flush()


# ---------------------------------------------------------------- 账本 I/O
def table_path(table, ex=None, symbol=None, ledger=None):
    base = ledger or LEDGER
    if table == "klines":
        return os.path.join(base, "klines", ex, "%s.jsonl" % safe_name(symbol))
    return os.path.join(base, "%s.jsonl" % table)


def safe_name(s):
    return s.replace("/", "_").replace(" ", "_")


def iter_jsonl(path):
    if not os.path.exists(path):
        return
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_table(table, ex=None, symbol=None, ledger=None, dedup=True):
    """读账本；dedup=True 时同主键取 fetched_at 最大（SPEC §2 读取方去重规则）。"""
    path = table_path(table, ex, symbol, ledger)
    rows = list(iter_jsonl(path))
    if not dedup:
        return rows
    best = {}
    for i, r in enumerate(rows):
        k = row_key(table, r)
        fa = r.get("fetched_at") or 0
        cur = best.get(k)
        if cur is None or fa >= cur[0]:
            best[k] = (fa, i, r)
    return [v[2] for v in sorted(best.values(), key=lambda v: v[1])]


class LedgerWriter(object):
    """追加写账本：同主键且内容相同 → 跳过；内容不同 → 追加新行（修正，读取方取 fetched_at 最大）。"""

    def __init__(self, ledger=None):
        self.ledger = ledger or LEDGER
        self._idx = {}
        self._lock = threading.Lock()
        self.stats = {}

    def _index(self, path, table):
        if path not in self._idx:
            idx = {}
            for r in iter_jsonl(path):
                idx[row_key(table, r)] = payload(r)
            self._idx[path] = idx
        return self._idx[path]

    def append(self, table, rows, ex=None, symbol=None):
        if not rows:
            return 0
        groups = {}
        for r in rows:
            validate(table, r)
            if table == "klines":
                path = table_path(table, ex or r.get("_ex"), symbol, self.ledger)
            else:
                path = table_path(table, ledger=self.ledger)
            groups.setdefault(path, []).append(r)
        n = 0
        with self._lock:
            for path, rs in groups.items():
                idx = self._index(path, table)
                out = []
                for r in rs:
                    k, p = row_key(table, r), payload(r)
                    if idx.get(k) == p:
                        continue
                    idx[k] = p
                    out.append(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
                if out:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write("\n".join(out) + "\n")
                n += len(out)
            self.stats[table] = self.stats.get(table, 0) + n
        return n

    def has_date(self, table, date, ex):
        path = table_path(table, ledger=self.ledger)
        idx = self._index(path, table)
        for k in idx:
            if k[0] == date and k[1] == ex:
                return True
        return False


class RelayWriter(object):
    """中继模式写出：前向按日文件，回填 gz 分片。行格式与账本完全一致。"""

    def __init__(self, out_dir):
        self.out = out_dir
        self._lock = threading.Lock()
        self.stats = {}

    def append(self, table, rows, ex=None, symbol=None, backfill=False):
        if not rows:
            return 0
        for r in rows:
            validate(table, r)
        ex = ex or rows[0].get("ex")
        with self._lock:
            if backfill or table == "klines":
                key = safe_name(symbol or rows[0].get("symbol") or "all")
                d = os.path.join(self.out, ex, "backfill", table)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "%s.jsonl.gz" % key)
                with gzip.open(path, "at", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                groups = {}
                for r in rows:
                    groups.setdefault(today(), []).append(r)
                for day, rs in groups.items():
                    d = os.path.join(self.out, ex, table)
                    os.makedirs(d, exist_ok=True)
                    with open(os.path.join(d, "%s.jsonl" % day), "a", encoding="utf-8") as fh:
                        for r in rs:
                            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.stats[table] = self.stats.get(table, 0) + len(rows)
        return len(rows)

    def has_date(self, table, date, ex):
        return os.path.exists(os.path.join(self.out, ex, table, "%s.jsonl" % date))


class Sink(object):
    """采集器统一出口。backfill=True 的行在本机模式也直接入账（source 字段区分）。"""

    def __init__(self, out_dir=None, ledger=None):
        out_dir = out_dir if out_dir is not None else os.environ.get("CARRY_OUT")
        self.relay = bool(out_dir)
        self.w = RelayWriter(out_dir) if self.relay else LedgerWriter(ledger)

    def write(self, table, rows, ex=None, symbol=None, backfill=False):
        if self.relay:
            return self.w.append(table, rows, ex=ex, symbol=symbol, backfill=backfill)
        return self.w.append(table, rows, ex=ex, symbol=symbol)

    def has_date(self, table, date, ex):
        return self.w.has_date(table, date, ex)

    @property
    def stats(self):
        return self.w.stats


# ---------------------------------------------------------------- 错误账 / 进度
def write_error(ex, where, exc, out_dir=None):
    out_dir = out_dir or os.environ.get("CARRY_OUT") or os.path.join(LEDGER, "_errors")
    d = os.path.join(out_dir, ex)
    os.makedirs(d, exist_ok=True)
    rec = {"at": ms_to_iso(now_ms()), "where": where, "error": str(exc)[:500],
           "http": getattr(exc, "code", None)}
    with open(os.path.join(d, "error.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def progress(name, **kw):
    """写进度文件 ledger/_progress/{name}.json（回填后台跑时看这里）。"""
    d = os.path.join(os.environ.get("CARRY_OUT") or LEDGER, "_progress")
    os.makedirs(d, exist_ok=True)
    kw["at"] = ms_to_iso(now_ms())
    tmp = os.path.join(d, name + ".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(kw, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, name + ".json"))


def sha256_file(path, n_lines=None):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for line in fh:
            if n_lines is not None and n >= n_lines:
                break
            h.update(line)
            n += 1
    return h.hexdigest(), n


# ---------------------------------------------------------------- 盘口深度
def depth_1pct(bids, asks, mid=None):
    """bids/asks: [[px, qty], ...]；返回 (±1% 内买盘 USD, 卖盘 USD)。"""
    bids = [(f(p), f(q)) for p, q in ((b[0], b[1]) for b in bids)]
    asks = [(f(p), f(q)) for p, q in ((a[0], a[1]) for a in asks)]
    bids = [(p, q) for p, q in bids if p and q]
    asks = [(p, q) for p, q in asks if p and q]
    if not bids or not asks:
        return None, None
    if mid is None:
        mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2.0
    b = sum(p * q for p, q in bids if p >= mid * 0.99)
    a = sum(p * q for p, q in asks if p <= mid * 1.01)
    return round(b, 2), round(a, 2)


def strip_multiplier(base):
    """永续 base 去掉面值前缀：1000PEPE→PEPE、1000000MOG→MOG、1MBABYDOGE→BABYDOGE；返回 (base, mult)。"""
    for pre, m in (("1000000", 1000000), ("100000", 100000), ("10000", 10000), ("1000", 1000),
                   ("1M", 1000000), ("k", 1000)):
        if base.startswith(pre) and len(base) > len(pre) and not base[len(pre)].isdigit():
            return base[len(pre):], m
    return base, 1


def annualize(rates_sum, days):
    """实现年化（百分比）= 区间费率合计 / 天数 × 365 × 100。与 interval_h 无关（按实际结算逐笔加总）。"""
    if not days:
        return None
    return rates_sum / float(days) * 365.0 * 100.0
