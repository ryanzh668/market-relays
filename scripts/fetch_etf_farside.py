#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Farside BTC 现货 ETF 每日净流(全表历史)中继抓取。

源:https://farside.co.uk/btc/ (页面上直接列出**全部历史**日行,单位 US$m)
本机状态:403 / Cloudflare 挡死 → 只能在 GitHub Actions 里跑。
        因此本脚本的**网络分支在本机无法实测**,标 [未经实测];
        解析器分支由 tests/ 的合成 fixture 覆盖(结构假设写在下面)。

页面表结构假设(2026-09 公开页面口径,写死在这里以便结构一变就红):
  * 一张主表,表头若干行,末列表头文字为 "Total";
    中间是各 ETF 代码列(IBIT/FBTC/BITB/ARKB/BTCO/EZBC/BRRR/HODL/BTCW/GBTC/BTC…),
    列数会随新产品上市而增加 —— 所以**按表头文字定位 Total 列,不写死列号**。
  * 数据行首列是日期,形如 "02 Jan 2026";
    表尾另有 "Total"/"Average"/"Maximum"/"Minimum" 汇总行,首列不是日期 → 自动跳过。
  * 数值单位 US$ 百万;负数用 "(123.4)" 括号表示;空/"-" 视作 0.0。

产出:data/etf_flows_farside.json = [{"date": "YYYY-MM-DD",
                                     "total_net_flow_usd_m": float}, ...] 按日期升序,按 date 去重。
失败:data/etf_flows_farside.error.json 追加一条 {date, ts_utc, error},退出码 1,
     **数据文件一个字节都不动**(绝不写猜的数)。
"""
import datetime as dt
import os
import re
import sys
from html.parser import HTMLParser

import relay_common as R

URL = "https://farside.co.uk/btc/"
NAME = "etf_flows_farside"
OUT = os.path.join(R.DATA, NAME + ".json")

# 解析出的日行少于这个数 = 页面结构八成变了(farside 全表历史 2024-01 起,数百行)
MIN_ROWS = 30
# 行残缺(列数不够 / 数值解析不了)占比上限,超了就判结构变化
MAX_BAD_FRAC = 0.20

DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%Y-%m-%d", "%b %d, %Y")


class _TableParser(HTMLParser):
    """把 HTML 里所有 <table> 抽成 list[list[list[str]]](表→行→单元格文本)。"""

    def __init__(self):
        HTMLParser.__init__(self)
        self.tables = []
        self._tstack = []      # 支持嵌套表
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tstack.append([])
        elif tag == "tr" and self._tstack:
            self._row = []
        elif tag in ("td", "th") and self._tstack:
            if self._row is None:      # 有些页面 <tr> 缺失,容错
                self._row = []
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr":
            if self._row is not None and self._tstack:
                self._tstack[-1].append(self._row)
            self._row = None
        elif tag == "table":
            if self._tstack:
                t = self._tstack.pop()
                if self._row:          # 未闭合的尾行
                    t.append(self._row)
                    self._row = None
                self.tables.append(t)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_entityref(self, name):
        if self._cell is not None:
            self._cell.append({"nbsp": " ", "amp": "&", "minus": "-"}.get(name, ""))

    def handle_charref(self, name):
        if self._cell is not None:
            try:
                self._cell.append(chr(int(name[1:], 16) if name[:1].lower() == "x" else int(name)))
            except ValueError:
                pass


def _clean(s):
    s = s.replace("\xa0", " ").replace("−", "-")
    return re.sub(r"\s+", " ", s).strip()


def parse_date_cell(s):
    """首列 → date;不是日期(汇总行/表头)返回 None。"""
    s = _clean(s)
    if not s or len(s) > 24:
        return None
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(s):
    """farside 金额单元格 → float(US$m)。解析不了抛 ValueError。"""
    s = _clean(s)
    if s in ("", "-", "–", "—", "n/a", "N/A"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace("US", "").strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    if s in ("", "-"):
        return 0.0
    v = float(s)          # 非数字 → ValueError,向上冒泡
    return -v if neg else v


def find_total_col(rows):
    """在表头行里找文字为 "Total" 的列;取最靠右的一个(farside 末列即总计)。

    找不到则返回 None,调用方回退到"该表众数列宽的最后一列"。
    """
    best = None
    for row in rows[:8]:
        for i, c in enumerate(row):
            if _clean(c).lower() == "total":
                if best is None or i > best:
                    best = i
    return best


def pick_table(tables):
    """选日期行最多的那张表。"""
    best, best_n = None, 0
    for t in tables:
        n = sum(1 for row in t if row and parse_date_cell(row[0]) is not None)
        if n > best_n:
            best, best_n = t, n
    return best, best_n


def parse_farside(html_text):
    """HTML → [{"date","total_net_flow_usd_m"}, ...] 升序。结构不符抛 RuntimeError。"""
    p = _TableParser()
    p.feed(html_text)
    table, n_date_rows = pick_table(p.tables)
    if table is None or n_date_rows == 0:
        raise RuntimeError("页面里找不到任何含日期行的表格(共 %d 张表)——结构已变或被 WAF 挡返回了非表格页"
                           % len(p.tables))

    col = find_total_col(table)
    if col is None:
        widths = {}
        for row in table:
            widths[len(row)] = widths.get(len(row), 0) + 1
        modal = max(widths, key=lambda k: widths[k])
        if modal < 2:
            raise RuntimeError("表头没有 'Total' 列,且众数列宽=%d 无法回退" % modal)
        col = modal - 1

    out, bad = {}, 0
    for row in table:
        if not row:
            continue
        d = parse_date_cell(row[0])
        if d is None:
            continue                     # 表头 / Total / Average / Maximum / Minimum 行
        if col >= len(row):
            bad += 1
            continue
        try:
            v = parse_amount(row[col])
        except ValueError:
            bad += 1
            continue
        out[d.isoformat()] = v

    if len(out) < MIN_ROWS:
        raise RuntimeError("只解析出 %d 条日行(< 下限 %d),判为页面结构变化或内容被截断"
                           % (len(out), MIN_ROWS))
    if bad > MAX_BAD_FRAC * (len(out) + bad):
        raise RuntimeError("残缺行 %d / 共 %d(> %.0f%%),Total 列(idx=%d)定位可能错了"
                           % (bad, len(out) + bad, MAX_BAD_FRAC * 100, col))

    return [{"date": k, "total_net_flow_usd_m": out[k]} for k in sorted(out)]


def main():
    try:
        raw = R.http_get(URL, timeout=90)
        html_text = raw.decode("utf-8", "replace")
        rows = parse_farside(html_text)
    except Exception as e:                      # noqa: BLE001 —— 任何失败都记账+红
        path = R.record_error(NAME, "%s: %s" % (type(e).__name__, e))
        sys.stderr.write("[FAIL] farside 抓取/解析失败,已记 %s\n       %s: %s\n"
                         % (path, type(e).__name__, e))
        return 1

    old = R.load_json(OUT, [])
    if not isinstance(old, list):
        sys.stderr.write("[FAIL] %s 不是数组,拒绝覆盖\n" % OUT)
        return 1
    merged, added, updated = R.merge_by_key(old, rows, "date")
    R.write_json(OUT, merged)
    sys.stdout.write("[OK] farside: 本次解析 %d 行,新增 %d,修订 %d,库存 %d(%s → %s)\n"
                     % (len(rows), added, updated, len(merged),
                        merged[0]["date"], merged[-1]["date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
