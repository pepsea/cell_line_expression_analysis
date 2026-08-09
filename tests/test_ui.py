"""Browser regression tests for the interactive behaviour of the page.

These cover things the Python suite structurally cannot: whether a filter
actually redraws the chart, and whether a <select> still shows what the view is
sorted by.  Both have regressed before.

Requires Playwright and a Chromium build; skips cleanly when either is absent:

    pip install playwright && playwright install chromium
    python tests/test_ui.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment without playwright
    sync_playwright = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_chromium():
    """Return an executable_path for Chromium, or None to use the default."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root or not os.path.isdir(root):
        return None
    for entry in sorted(os.listdir(root)):
        if entry.startswith("chromium-"):
            candidate = os.path.join(root, entry, "chrome-linux", "chrome")
            if os.path.exists(candidate):
                return candidate
    return None


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class UiTests(unittest.TestCase):
    server = None
    browser = None

    @classmethod
    def setUpClass(cls):
        import uvicorn

        from hpa_cellexp.api import create_app
        from hpa_cellexp.demo import build_demo_database

        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "ui.sqlite")
        build_demo_database(db_path)

        cls.port = _free_port()
        config = uvicorn.Config(create_app(db_path), host="127.0.0.1", port=cls.port,
                                log_level="error")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        import time
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise RuntimeError("test server did not start")

        cls.play = sync_playwright().start()
        launch = {"args": ["--no-sandbox"]}
        executable = _find_chromium()
        if executable:
            launch["executable_path"] = executable
        try:
            cls.browser = cls.play.chromium.launch(**launch)
        except Exception as exc:  # pragma: no cover - no browser binary
            cls.play.stop()
            raise unittest.SkipTest("chromium unavailable: {}".format(exc))

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
            cls.play.stop()
        if cls.server:
            cls.server.should_exit = True
            cls.thread.join(timeout=10)
        cls.tmp.cleanup()

    def setUp(self):
        self.errors = []
        self.page = self.browser.new_page(viewport={"width": 1400, "height": 950})
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on(
            "console",
            lambda m: self.errors.append(m.text) if m.type == "error" else None,
        )
        self.page.goto("http://127.0.0.1:{}/".format(self.port), wait_until="networkidle")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [], "console/page errors: {}".format(self.errors))

    # -- helpers ----------------------------------------------------------

    def run_genes(self, genes: str):
        self.page.fill("#geneInput", genes)
        self.page.wait_for_timeout(300)
        self.page.click("#runButton")
        self.page.wait_for_selector("#hmViewport:not([hidden])", timeout=15000)
        self.page.wait_for_timeout(400)

    def visible_names(self, n=4):
        return self.page.evaluate(
            "(n) => state.view.slice(0, n).map(i => state.result.cellLines[i].name)", n
        )

    # -- tests -------------------------------------------------------------

    def test_reference_gene_select_keeps_its_selection(self):
        """Regression: the select snapped back to the first option after each
        sort, so the first gene could never be re-picked (no change event)."""
        self.run_genes("ALB, KLK3, PTPRC, GAPDH")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)

        for value in ["2", "0", "1", "0", "3", "0"]:
            with self.subTest(reference=value):
                self.page.select_option("#sortGene", value)
                self.page.wait_for_timeout(300)
                self.assertEqual(self.page.input_value("#sortGene"), value)
                self.assertEqual(
                    str(self.page.evaluate("() => state.sortGene")), value,
                    "the view is not sorted by the gene the control shows",
                )

    def test_first_gene_actually_reorders_after_another_gene(self):
        self.run_genes("ALB, KLK3, PTPRC")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)

        self.page.select_option("#sortGene", "0")   # ALB -> liver line on top
        self.page.wait_for_timeout(300)
        by_alb = self.visible_names(1)

        self.page.select_option("#sortGene", "2")   # PTPRC -> blood lines on top
        self.page.wait_for_timeout(300)
        by_ptprc = self.visible_names(1)
        self.assertNotEqual(by_alb, by_ptprc)

        self.page.select_option("#sortGene", "0")   # back to the first gene
        self.page.wait_for_timeout(300)
        self.assertEqual(self.visible_names(1), by_alb)

    def test_sort_by_mean_across_all_genes(self):
        self.run_genes("ALB, KLK3, PTPRC, GAPDH")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)

        options = self.page.eval_on_selector_all("#sortGene option", "e => e.map(o => o.value)")
        self.assertIn("mean", options)

        self.page.select_option("#sortGene", "mean")
        self.page.wait_for_timeout(400)
        means = self.page.evaluate(
            """() => state.view.map(i => {
                 let sum = 0, n = 0;
                 for (let r = 0; r < state.result.genes.length; r += 1) {
                   const v = state.result.values[r][i];
                   if (v !== null && v !== undefined) { sum += v; n += 1; }
                 }
                 return n ? sum / n : null;
               })"""
        )
        self.assertEqual(means, sorted(means, reverse=True))

        self.page.select_option("#sortSelect", "value-asc")
        self.page.wait_for_timeout(400)
        asc = self.page.evaluate(
            """() => state.view.map(i => {
                 let sum = 0, n = 0;
                 for (let r = 0; r < state.result.genes.length; r += 1) {
                   const v = state.result.values[r][i];
                   if (v !== null && v !== undefined) { sum += v; n += 1; }
                 }
                 return n ? sum / n : null;
               })"""
        )
        self.assertEqual(asc, sorted(asc))

    # Independently recomputed in the browser from the rendered payload, so the
    # assertions do not just re-run the implementation being tested.
    _KEYS_JS = """(basis) => {
        const d = state.result;
        const maxima = [], totals = [];
        for (let r = 0; r < d.genes.length; r += 1) {
          let m = 0, t = 0;
          for (const v of d.values[r]) {
            if (v === null || v === undefined) continue;
            if (v > m) m = v;
            t += v;
          }
          maxima.push(m); totals.push(t);
        }
        const usable = maxima.map((m, r) => (m > 0 ? r : -1)).filter((r) => r >= 0);
        const norm = (v, m) => (m > 0 ? Math.log10(1 + v) / Math.log10(1 + m) : 0);
        return state.view.map((i) => {
          let sum = 0, n = 0, lo = Infinity;
          for (const r of usable) {
            const v = d.values[r][i];
            if (v === null || v === undefined) continue;
            let x;
            if (basis === 'mean') x = v;
            else if (basis === 'share-mean') x = totals[r] > 0 ? v / totals[r] : 0;
            else x = norm(v, maxima[r]);
            sum += x; n += 1; if (x < lo) lo = x;
          }
          if (!n) return null;
          return basis === 'norm-min' ? lo : sum / n;
        });
      }"""

    def _ordered_desc(self, basis):
        keys = self.page.evaluate(self._KEYS_JS, basis)
        self.assertEqual(
            [round(k, 9) for k in keys],
            [round(k, 9) for k in sorted(keys, reverse=True)],
            "rows are not ordered by the {} basis".format(basis),
        )
        return keys

    def test_aggregate_sort_bases_are_offered(self):
        self.run_genes("GAPDH, ALB, KLK3, PTPRC")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        values = self.page.eval_on_selector_all("#sortGene option", "e => e.map(o => o.value)")
        for basis in ("norm-min", "share-mean", "mean"):
            self.assertIn(basis, values)

    def test_share_mean_is_not_dominated_by_the_largest_gene(self):
        """A plain mean ranks by GAPDH alone; the share mean must not."""
        self.run_genes("GAPDH, ALB, KLK3, PTPRC, MITF, GFAP")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)

        self.page.select_option("#sortGene", "mean")
        self.page.wait_for_timeout(400)
        self._ordered_desc("mean")
        by_mean = self.visible_names(6)

        self.page.select_option("#sortGene", "share-mean")
        self.page.wait_for_timeout(400)
        self._ordered_desc("share-mean")
        self.assertNotEqual(by_mean, self.visible_names(6))

    def test_share_mean_replaced_the_earlier_aggregate(self):
        self.run_genes("GAPDH, ALB, PTPRC")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        values = self.page.eval_on_selector_all("#sortGene option", "e => e.map(o => o.value)")
        self.assertIn("share-mean", values)
        for gone in ("log-mean", "norm-mean"):
            self.assertNotIn(gone, values)
        labels = self.page.eval_on_selector_all("#sortGene option", "e => e.map(o => o.textContent)")
        self.assertIn("発現量割合の平均", labels)

    def test_share_mean_gives_every_gene_the_same_total_weight(self):
        """Each gene's shares sum to 1 across the cell lines, so the ranking
        cannot be carried by whichever gene has the largest absolute values."""
        self.run_genes("GAPDH, ALB, KLK3, PTPRC, MITF")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        self.page.select_option("#sortGene", "share-mean")
        self.page.wait_for_timeout(400)
        keys = self._ordered_desc("share-mean")

        # Every key is a mean of shares, so 0..1, and they sum to ~1 overall.
        self.assertTrue(all(0 <= k <= 1 for k in keys if k is not None))
        self.assertAlmostEqual(sum(k for k in keys if k is not None), 1.0, places=6)

        # And it must not simply reproduce the arithmetic mean's order.
        by_share = self.visible_names(6)
        self.page.select_option("#sortGene", "mean")
        self.page.wait_for_timeout(400)
        self.assertNotEqual(by_share, self.visible_names(6))

    def test_min_basis_puts_evenly_expressed_cell_lines_first(self):
        """The bottleneck basis: a cell line only ranks high when its weakest
        gene is high, which is the "全遺伝子が満遍なく発現" question."""
        self.run_genes("GAPDH, ALB, KLK3, PTPRC, MITF, GFAP")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        self.page.select_option("#sortGene", "norm-min")
        self.page.wait_for_timeout(400)
        self._ordered_desc("norm-min")

        # Anything with a zero must rank below everything without one.
        has_zero = self.page.evaluate(
            """() => state.view.map(i =>
                 state.result.genes.some((_, r) => state.result.values[r][i] === 0))"""
        )
        first_zero = has_zero.index(True) if True in has_zero else len(has_zero)
        self.assertNotIn(False, has_zero[first_zero:],
                         "a cell line with no zero ranked below one with a zero")
        self.assertGreater(first_zero, 0, "expected at least one evenly-expressed cell line")

    def test_aggregate_basis_survives_a_rerun(self):
        self.run_genes("GAPDH, ALB, PTPRC")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        self.page.select_option("#sortGene", "norm-min")
        self.page.wait_for_timeout(400)
        self.page.click("#runButton")
        self.page.wait_for_timeout(900)
        self.assertEqual(self.page.evaluate("() => state.sortGene"), "norm-min")
        self.assertEqual(self.page.input_value("#sortGene"), "norm-min")

    def test_single_gene_hides_the_reference_control(self):
        self.run_genes("ALB")
        self.page.select_option("#sortSelect", "value-desc")
        self.page.wait_for_timeout(300)
        self.assertTrue(self.page.locator("#sortGene").is_hidden())

    def test_cell_line_search_filters_without_pressing_run(self):
        """Regression: filters only moved the match count; the chart kept
        showing the previous selection until the run button was pressed."""
        self.run_genes("GAPDH, PTPRC")
        self.assertGreater(len(self.visible_names(99)), 5)

        self.page.fill("#cellQuery", "hek293")
        self.page.wait_for_timeout(1500)
        self.assertEqual(self.visible_names(99), ["HEK 293"])

        self.page.fill("#cellQuery", "")
        self.page.wait_for_timeout(1500)
        self.assertGreater(len(self.visible_names(99)), 5)

    def test_organ_facet_is_searchable_in_japanese_and_filters_live(self):
        self.run_genes("ALB, GAPDH")
        self.page.fill("#organSearch", "肝")
        self.page.wait_for_timeout(300)
        shown = self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none').map(e => e.querySelector('input').value)",
        )
        self.assertEqual(shown, ["Liver"])

        self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none')"
            "        .forEach(e => e.querySelector('input').click())",
        )
        self.page.wait_for_timeout(1500)
        self.assertEqual(self.visible_names(99), ["HEP G2"])

    def test_searching_the_brain_also_surfaces_the_peripheral_nervous_system(self):
        """Kelly is a neuroblastoma line, so it is filed under the peripheral
        nervous system - correct, but someone looking for 脳 concluded the cell
        line had disappeared.  Each nervous-system organ finds the other."""
        self.run_genes("GAPDH")
        self.page.fill("#organSearch", "脳")
        self.page.wait_for_timeout(300)
        shown = self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none')"
            "        .map(e => e.querySelector('input').value)",
        )
        self.assertIn("Brain", shown)
        self.assertIn("Peripheral nervous system", shown)

        self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none')"
            "        .forEach(e => e.querySelector('input').click())",
        )
        self.page.wait_for_timeout(1500)
        self.assertIn("Kelly", self.visible_names(99))

    def test_species_facet_shows_one_entry_per_species(self):
        """Regression: the facet rendered "ヒト Homo sapiens", which reads as
        two separate species for what is one."""
        items = self.page.eval_on_selector_all(
            "#speciesList .facet-item .name", "els => els.map(e => e.textContent.trim())"
        )
        self.assertEqual(items, ["Homo sapiens"])
        # ヒト still finds it - the label is only hidden, not dropped.
        searchable = self.page.eval_on_selector(
            "#speciesList .facet-item", "e => e.dataset.value"
        )
        self.assertIn("ヒト", searchable)
        self.assertIn("homo sapiens", searchable)

    def test_heatmap_widens_as_genes_are_added(self):
        """More genes must widen the sheet, not squeeze the columns: a column
        never goes below the minimum unit."""
        widths = self.page.evaluate(
            "() => [1, 3, 8, 20, 60].map(n => geneColumnWidth(900, n))"
        )
        self.assertTrue(all(w >= 44 for w in widths), widths)
        self.assertEqual(widths[-1], 44, "the minimum unit is the floor")
        self.assertTrue(
            all(a >= b for a, b in zip(widths, widths[1:])),
            "columns must not grow as genes are added: {}".format(widths),
        )

        self.run_genes("ALB, KLK3")
        two = self.page.eval_on_selector("#hmSizer", "e => parseFloat(e.style.width)")
        self.run_genes("ALB, KLK3, PTPRC, GAPDH, VIM, EGFR, MKI67, CD19")
        eight = self.page.eval_on_selector("#hmSizer", "e => parseFloat(e.style.width)")
        self.assertGreater(eight, two, "eight genes must be wider than two")

    def test_dataset_chip_reveals_the_source_files(self):
        chip = self.page.locator("#datasetChip")
        panel = self.page.locator("#sourcePanel")
        self.assertTrue(panel.is_hidden())

        chip.click()
        self.page.wait_for_timeout(200)
        self.assertFalse(panel.is_hidden())
        text = panel.inner_text()
        self.assertIn("このサイトが使用しているデータ", text)
        self.assertIn("発現マトリクス", text)
        self.assertIn("細胞株メタデータ", text)
        self.assertIn("データベース", text)
        self.assertIn("ui.sqlite", text)
        # the demo database must say so here too, not only in the banner
        self.assertIn("合成", text)

        self.page.mouse.click(700, 700)
        self.page.wait_for_timeout(200)
        self.assertTrue(panel.is_hidden())

    def test_heatmap_rows_are_cell_lines(self):
        """Orientation: cell lines down the side, genes across the top."""
        self.run_genes("ALB, KLK3, PTPRC")
        geometry = self.page.evaluate(
            "() => ({rows: state.view.length, genes: state.result.genes.length})"
        )
        self.assertEqual(geometry["genes"], 3)
        total = self.page.evaluate(
            "async () => (await (await fetch('/api/meta')).json()).cellLineCount"
        )
        self.assertEqual(geometry["rows"], total)
        content = self.page.eval_on_selector(
            "#hmSizer", "e => ({w: parseFloat(e.style.width), h: parseFloat(e.style.height)})"
        )
        self.assertGreater(content["h"], content["w"], "the sheet should be taller than it is wide")


if __name__ == "__main__":
    unittest.main(verbosity=2)
