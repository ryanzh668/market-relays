#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线解析器测试(**不联网**,只吃 tests/fixtures/ 里的合成夹具)。

两个解释器都必须绿:
    /usr/bin/python3          tests/test_parsers.py     (3.9)
    /opt/homebrew/bin/python3 tests/test_parsers.py     (3.14)

夹具全部标 _SYNTHETIC:它们是手造的结构样本,**不是真实市场数据**,
只用来钉住"结构一变就红"这件事;数值本身没有任何研究含义。
网络分支(farside/cftc 的 http_get)在本机够不着,故 [未经实测],不在这里测。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import relay_common as R          # noqa: E402
import fetch_etf_farside as FS    # noqa: E402
import fetch_cftc_cot as CT       # noqa: E402


def fx(name):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- farside
class TestFarside(unittest.TestCase):
    def setUp(self):
        self.html = fx("farside_btc_SYNTHETIC.html")

    def test_parses_all_date_rows_and_skips_summary(self):
        rows = FS.parse_farside(self.html)
        self.assertEqual(len(rows), 40)
        # Total / Average / Maximum / Minimum 汇总行首列不是日期 → 必须被跳掉
        self.assertEqual(len(set(r["date"] for r in rows)), 40)
        # 升序
        self.assertEqual([r["date"] for r in rows],
                         sorted(r["date"] for r in rows))

    def test_values_match_expected(self):
        rows = {r["date"]: r["total_net_flow_usd_m"] for r in FS.parse_farside(self.html)}
        exp = {}
        with open(os.path.join(FIX, "farside_btc_SYNTHETIC.expected.csv"), encoding="utf-8") as f:
            next(f)
            for line in f:
                if line.strip():
                    d, v = line.strip().split(",")
                    exp[d] = float(v)
        self.assertEqual(set(rows), set(exp))
        for d in exp:
            self.assertAlmostEqual(rows[d], exp[d], delta=0.06, msg=d)

    def test_negatives_present(self):
        vals = [r["total_net_flow_usd_m"] for r in FS.parse_farside(self.html)]
        self.assertTrue(any(v < 0 for v in vals), "夹具里应有括号负数被正确解析为负")

    def test_parse_amount(self):
        cases = [("1,234.5", 1234.5), ("(123.4)", -123.4), ("-12.0", -12.0),
                 ("$88.8", 88.8), ("-", 0.0), ("", 0.0), ("0.0", 0.0),
                 ("−5.0", -5.0)]
        for s, want in cases:
            self.assertAlmostEqual(FS.parse_amount(s), want, msg=repr(s))
        for bad in ("abc", "1.2.3", "N.A"):
            self.assertRaises(ValueError, FS.parse_amount, bad)

    def test_parse_date_cell(self):
        self.assertIsNotNone(FS.parse_date_cell("02 Jan 2026"))
        self.assertIsNotNone(FS.parse_date_cell("2026-01-02"))
        for s in ("Total", "Average", "Maximum", "Minimum", "IBIT", ""):
            self.assertIsNone(FS.parse_date_cell(s), s)

    # ---- fail-loudly ----
    def test_no_table_raises(self):
        self.assertRaises(RuntimeError, FS.parse_farside,
                          "<html><body><p>Just a Cloudflare challenge page</p></body></html>")

    def test_too_few_rows_raises(self):
        small = ("<table><tr><th>Date</th><th>IBIT</th><th>Total</th></tr>"
                 "<tr><td>01 Jan 2026</td><td>1.0</td><td>1.0</td></tr>"
                 "<tr><td>02 Jan 2026</td><td>2.0</td><td>2.0</td></tr></table>")
        with self.assertRaises(RuntimeError) as cm:
            FS.parse_farside(small)
        self.assertIn("下限", str(cm.exception))

    def test_total_column_fallback_when_header_missing(self):
        """表头没有 'Total' 字样时回退到众数列宽的最后一列,而不是静默取错列。"""
        html = self.html.replace("<th>Total</th>", "<th>Sum</th>")
        rows = FS.parse_farside(html)
        self.assertEqual(len(rows), 40)

    def test_total_col_is_header_not_tail_summary(self):
        """表尾 'Total' 汇总行在第 0 列;总计列必须取表头段那个(末列),不能被它带偏。"""
        p = FS._TableParser()
        p.feed(self.html)
        table, _ = FS.pick_table(p.tables)
        self.assertEqual(FS.find_total_col(table), 13)

    def test_min_rows_is_a_parameter(self):
        """落地页只有最近十几天,全史页有几百行 —— 门槛按源给,不是一个全局常量。"""
        small = ("<table><tr><td></td><td>IBIT</td><td>Total</td></tr>"
                 + "".join("<tr><td>%02d Jan 2026</td><td>1.0</td><td>2.0</td></tr>" % (i + 1)
                           for i in range(15))
                 + "</table>")
        self.assertEqual(len(FS.parse_farside(small, min_rows=10)), 15)
        self.assertRaises(RuntimeError, FS.parse_farside, small, 300)

    def test_sources_ordered_full_history_first(self):
        """全史页必须排在降级源前面,且门槛更高。"""
        self.assertEqual(FS.SOURCES[0][0],
                         "https://farside.co.uk/bitcoin-etf-flow-all-data/")
        self.assertGreater(FS.SOURCES[0][1], FS.SOURCES[1][1])

    def test_bad_rows_over_threshold_raises(self):
        """残缺行(拿不到 Total 列)占比过高 → 判 Total 列定位错了,不许当噪声跳过。"""
        good = "".join(
            "<tr><td>%02d Jan 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>" % (i + 1)
            for i in range(31))
        bad = "".join(
            "<tr><td>%02d Mar 2026</td><td>1.0</td></tr>" % (i + 1) for i in range(20))
        html = ("<table><tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>Total</th></tr>"
                + good + bad + "</table>")
        with self.assertRaises(RuntimeError) as cm:
            FS.parse_farside(html)
        self.assertIn("残缺行", str(cm.exception))


# ---------------------------------------------------------------- CFTC TFF
class TestCot(unittest.TestCase):
    def setUp(self):
        self.txt = fx("finfut_SYNTHETIC.txt")

    def test_filters_to_cme_bitcoin_only(self):
        rows = CT.parse_tff_text(self.txt)
        names = set(r["market"] for r in rows)
        self.assertIn("BITCOIN - CHICAGO MERCANTILE EXCHANGE", names)
        self.assertIn("MICRO BITCOIN - CHICAGO MERCANTILE EXCHANGE", names)
        self.assertIn("BITCOIN FRIDAY - CHICAGO MERCANTILE EXCHANGE", names)
        self.assertNotIn("S&P 500 Consolidated - CHICAGO MERCANTILE EXCHANGE", names)
        self.assertNotIn("BITCOIN - SOME OTHER EXCHANGE", names)
        self.assertEqual(len(rows), 4)

    def test_field_mapping_by_header_name(self):
        rows = [r for r in CT.parse_tff_text(self.txt)
                if r["market"] == "BITCOIN - CHICAGO MERCANTILE EXCHANGE"
                and r["report_date"] == "2026-09-01"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["open_interest"], 30150)
        self.assertEqual(r["dealer_long"], 1100)
        self.assertEqual(r["dealer_short"], 4200)
        self.assertEqual(r["asset_mgr_long"], 9800)
        self.assertEqual(r["asset_mgr_short"], 1050)
        self.assertEqual(r["lev_money_long"], 6400)
        self.assertEqual(r["lev_money_short"], 18900)

    def test_missing_value_dot_becomes_none(self):
        r = [x for x in CT.parse_tff_text(self.txt)
             if x["market"].startswith("BITCOIN FRIDAY")][0]
        self.assertIsNone(r["dealer_long"])
        self.assertIsNone(r["dealer_short"])
        self.assertEqual(r["lev_money_long"], 200)

    def test_alternate_legacy_header_and_date_format(self):
        """旧年份用 Report_Date_as_MM_DD_YYYY + MM/DD/YYYY;映射必须照样成立。"""
        txt = self.txt.replace("Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY")
        txt = txt.replace("2026-09-01", "09/01/2026").replace("2026-08-25", "08/25/2026")
        rows = CT.parse_tff_text(txt)
        self.assertEqual(len(rows), 4)
        self.assertIn("2026-09-01", set(r["report_date"] for r in rows))

    def test_column_order_change_is_survived(self):
        """列顺序变了(按名取列的意义所在)。"""
        lines = self.txt.strip().split("\n")
        import csv
        rdr = list(csv.reader(io.StringIO(self.txt)))
        order = list(range(len(rdr[0])))
        order.reverse()
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for row in rdr:
            w.writerow([row[i] for i in order])
        rows = CT.parse_tff_text(buf.getvalue())
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(lines), 7)

    # ---- fail-loudly ----
    def test_missing_required_column_raises(self):
        txt = self.txt.replace("Lev_Money_Positions_Long_All", "Something_Else")
        with self.assertRaises(RuntimeError) as cm:
            CT.parse_tff_text(txt)
        self.assertIn("lev_money_long", str(cm.exception))

    def test_header_only_raises(self):
        head = self.txt.split("\n")[0] + "\n"
        self.assertRaises(RuntimeError, CT.parse_tff_text, head)

    def test_empty_file_raises(self):
        self.assertRaises(RuntimeError, CT.parse_tff_text, "")

    def test_bad_date_raises(self):
        txt = self.txt.replace('"2026-09-01"', '"not-a-date"')
        self.assertRaises(ValueError, CT.parse_tff_text, txt)

    def test_read_zip_txt(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("FinFutYY.txt", self.txt)
        self.assertEqual(CT.read_zip_txt(buf.getvalue()), self.txt)

    def test_read_zip_without_txt_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.pdf", "x")
        self.assertRaises(RuntimeError, CT.read_zip_txt, buf.getvalue())


# ---------------------------------------------------------------- 合并 / 记账
class TestMergeAndErrorLedger(unittest.TestCase):
    def test_merge_dedup_and_sort(self):
        old = [{"date": "2026-01-02", "total_net_flow_usd_m": 1.0},
               {"date": "2026-01-01", "total_net_flow_usd_m": 2.0}]
        new = [{"date": "2026-01-02", "total_net_flow_usd_m": 9.0},   # 修订
               {"date": "2026-01-03", "total_net_flow_usd_m": 3.0}]   # 新增
        merged, added, updated = R.merge_by_key(old, new, "date")
        self.assertEqual([r["date"] for r in merged],
                         ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual((added, updated), (1, 1))
        self.assertEqual(merged[1]["total_net_flow_usd_m"], 9.0)

    def test_merge_idempotent(self):
        rows = [{"date": "2026-01-01", "total_net_flow_usd_m": 1.0}]
        merged, added, updated = R.merge_by_key(rows, rows, "date")
        self.assertEqual((added, updated, len(merged)), (0, 0, 1))

    def test_merge_composite_key(self):
        a = {"report_date": "2026-09-01", "market": "BITCOIN - CHICAGO MERCANTILE EXCHANGE"}
        b = {"report_date": "2026-09-01", "market": "MICRO BITCOIN - CHICAGO MERCANTILE EXCHANGE"}
        merged, added, _ = R.merge_by_key([], [a, b], CT.rec_key)
        self.assertEqual(len(merged), 2, "同日多合约不得互相覆盖")
        self.assertEqual(added, 2)

    def test_error_ledger_is_append_only(self):
        tmp = tempfile.mkdtemp()
        old_data = R.DATA
        try:
            R.DATA = tmp
            R.record_error("thing", "first")
            R.record_error("thing", "second")
            with open(os.path.join(tmp, "thing.error.json"), encoding="utf-8") as f:
                rows = json.load(f)
            self.assertEqual([r["error"] for r in rows], ["first", "second"])
        finally:
            R.DATA = old_data
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
