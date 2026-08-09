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

    def test_heatmap_rows_are_cell_lines(self):
        """Orientation: cell lines down the side, genes across the top."""
        self.run_genes("ALB, KLK3, PTPRC")
        geometry = self.page.evaluate(
            "() => ({rows: state.view.length, genes: state.result.genes.length})"
        )
        self.assertEqual(geometry["genes"], 3)
        self.assertEqual(geometry["rows"], 50)
        content = self.page.eval_on_selector(
            "#hmSizer", "e => ({w: parseFloat(e.style.width), h: parseFloat(e.style.height)})"
        )
        self.assertGreater(content["h"], content["w"], "the sheet should be taller than it is wide")


if __name__ == "__main__":
    unittest.main(verbosity=2)
