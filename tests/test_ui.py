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

    def test_a_neuroblastoma_line_is_under_the_brain(self):
        """The report: Kelly shows up when sorting by name or by expression,
        but vanishes from the 由来臓器 grouping.  It was in its own
        "peripheral nervous system" bucket, not the 脳 everyone looks in."""
        self.run_genes("GAPDH")
        self.page.fill("#organSearch", "脳")
        self.page.wait_for_timeout(300)
        shown = self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none')"
            "        .map(e => e.querySelector('input').value)",
        )
        self.assertEqual(shown, ["Brain"], "the nervous system is one bucket")

        self.page.eval_on_selector_all(
            "#organList .facet-item",
            "els => els.filter(e => e.style.display !== 'none')"
            "        .forEach(e => e.querySelector('input').click())",
        )
        self.page.wait_for_timeout(1500)
        names = self.visible_names(99)
        for expected in ("Kelly", "SH-SY5Y", "SK-N-SH", "U-251 MG"):
            self.assertIn(expected, names)

    def test_the_page_shows_which_build_it_is_running(self):
        """A stale image or a cached script looks exactly like a bug that was
        never fixed.  The badge is what tells those two apart."""
        from hpa_cellexp import __version__

        badge = self.page.locator("#versionBadge")
        self.assertFalse(badge.is_hidden(), "the version badge must be visible")
        self.assertEqual(badge.inner_text().strip(), "v{}".format(__version__))
        self.assertNotIn("stale", badge.get_attribute("class") or "")
        self.assertTrue(self.page.locator("#staleBanner").is_hidden())

    def test_a_mismatched_build_is_reported_loudly(self):
        version = self.page.evaluate("() => APP_VERSION")
        self.page.evaluate("() => showVersion('0.0.1-other')")
        self.page.wait_for_timeout(100)
        badge = self.page.locator("#versionBadge")
        self.assertIn("stale", badge.get_attribute("class") or "")
        self.assertIn("0.0.1-other", badge.inner_text())
        banner = self.page.locator("#staleBanner")
        self.assertFalse(banner.is_hidden())
        self.assertIn("Cmd+Shift+R", banner.inner_text())
        # Back to agreement: no banner, no warning styling.
        self.page.evaluate("(v) => showVersion(v)", version)
        self.page.wait_for_timeout(100)
        self.assertNotIn("stale", badge.get_attribute("class") or "")

    def test_an_organ_label_never_sits_on_another_organs_rows(self):
        """Reported as "LNCAP は前立腺ではなく乳腺に表示される".

        The label was clamped to the viewport edges, so the group starting
        just below the fold got its name painted beside the last row of the
        group above it.  A label must stay inside its own rows at every
        scroll position.
        """
        self.run_genes("GAPDH, EGFR")
        self.page.select_option("#sortSelect", "organ")
        self.page.wait_for_timeout(600)

        offending = self.page.evaluate(
            """() => {
              const vh = document.getElementById('hmViewport').clientHeight;
              const bad = [];
              const groups = organGroups().filter(g => g.organ);
              for (let sy = 0; sy < 400; sy += 7) {
                for (const g of groups) {
                  const top = HM.band + g.start * HM.ch - sy;
                  const bottom = HM.band + (g.end + 1) * HM.ch - sy;
                  const y = groupLabelY(top, bottom, HM.band, vh);
                  if (y === null) continue;
                  if (y < top || y > bottom) {
                    bad.push({organ: g.organ, scroll: sy, y, top, bottom});
                  }
                }
              }
              return bad;
            }"""
        )
        self.assertEqual(offending, [], "label drawn outside its own group")

    def test_every_organ_change_gets_a_boundary_rule(self):
        """The boundaries were invisible, so the sheet read as one continuous
        list and the labels looked attached to whatever rows were nearest.
        One rule per organ change, and none inside an organ."""
        self.run_genes("GAPDH, EGFR")
        self.page.select_option("#sortSelect", "organ")
        self.page.wait_for_timeout(600)

        result = self.page.evaluate(
            """() => ({
              rules: groupBoundaryRows(),
              starts: organGroups().filter(g => g.start > 0).map(g => g.start),
              width: GROUP_RULE_WIDTH,
              organs: organGroups().length,
            })"""
        )
        self.assertGreater(result["organs"], 3, "the demo data must span several organs")
        self.assertEqual(result["rules"], result["starts"])
        self.assertGreater(result["width"], 0)

    def test_rows_keep_a_constant_pitch(self):
        """Rules divide the list without moving anything, so hit testing stays
        a plain division - a row must still map back to its own cell line."""
        self.run_genes("GAPDH, EGFR")
        self.page.select_option("#sortSelect", "organ")
        self.page.wait_for_timeout(600)
        geometry = self.page.evaluate(
            """() => {
              const v = document.getElementById('hmViewport');
              const rows = state.view.length;
              return {
                sheet: parseFloat(document.getElementById('hmSizer').style.height),
                expected: HM.band + rows * HM.ch,
              };
            }"""
        )
        self.assertEqual(geometry["sheet"], geometry["expected"])

    def test_ungrouped_sorts_have_no_boundary_rules(self):
        self.run_genes("GAPDH, EGFR")
        self.page.select_option("#sortSelect", "name")
        self.page.wait_for_timeout(600)
        self.assertEqual(self.page.evaluate("() => groupBoundaryRows()"), [])

    def test_clicking_a_gene_opens_it_in_hpa(self):
        """The heatmap's gene band links out to the Human Protein Atlas, the
        way the cell line names link out to Cellosaurus."""
        self.run_genes("ALB, KLK3, PTPRC")
        opened = []
        self.page.expose_function("recordOpen", lambda url: opened.append(url))
        self.page.evaluate("() => { window.open = (u) => { recordOpen(u); return null; }; }")

        geometry = self.page.evaluate(
            """() => {
              const v = document.getElementById('hmViewport').getBoundingClientRect();
              return {x: v.left + HM.gutter + HM.cw / 2, y: v.top + HM.band / 2,
                      url: state.result.genes[0].hpaUrl, symbol: state.result.genes[0].symbol};
            }"""
        )
        self.page.mouse.click(geometry["x"], geometry["y"])
        self.page.wait_for_timeout(300)
        self.assertEqual(opened, [geometry["url"]])
        self.assertIn("proteinatlas.org", geometry["url"])
        self.assertIn(geometry["symbol"], geometry["url"])

    def test_the_table_header_links_out_without_losing_its_sort(self):
        """The header itself sorts, so the link out has to be a separate
        target - otherwise sorting by a gene would navigate away."""
        self.run_genes("ALB, KLK3, PTPRC")
        self.page.click("#tabTable")
        self.page.wait_for_function(
            "() => document.getElementById('tableNotice').hidden", timeout=15000
        )
        links = self.page.eval_on_selector_all(
            "#matrixTable thead th .th-link", "els => els.map(e => e.href)"
        )
        self.assertEqual(len(links), 3)
        self.assertTrue(all("proteinatlas.org" in href for href in links), links)

        before = self.page.evaluate("() => state.sort")
        # 5 meta columns now precede the genes (the last is 類似がん種).
        self.page.click("#matrixTable thead th:nth-child(6)")
        self.page.wait_for_timeout(400)
        self.assertNotEqual(self.page.evaluate("() => state.sort"), before)
        self.assertTrue(self.page.url.endswith("/") or "127.0.0.1" in self.page.url)

    def test_the_tcga_similarity_is_actually_shown(self):
        """It was computed and sent with every response, then dropped on the
        floor - nothing in the UI ever read it."""
        self.run_genes("GAPDH, ALB")
        payload = self.page.evaluate(
            """() => {
              const byName = {};
              state.result.cellLines.forEach(c => { byName[c.name] = c.tcga; });
              return byName;
            }"""
        )
        self.assertTrue(payload["HEP G2"], "no TCGA hits reached the client")
        self.assertLessEqual(len(payload["HEP G2"]), 3)
        self.assertEqual(payload["HEP G2"][0]["cancer"], "LIHC")
        self.assertEqual(payload["HEP G2"][0]["nameJa"], "肝細胞がん")

        # ...in the heatmap tooltip
        row = self.page.evaluate(
            """() => {
              const i = state.view.findIndex(
                v => state.result.cellLines[v].name === 'HEP G2');
              const v = document.getElementById('hmViewport').getBoundingClientRect();
              return {x: v.left + HM.gutter / 2, y: v.top + HM.band + i * HM.ch + HM.ch / 2};
            }"""
        )
        self.page.mouse.move(row["x"], row["y"])
        self.page.wait_for_timeout(300)
        tooltip = self.page.inner_text("#hmTooltip")
        self.assertIn("類似がん種", tooltip)
        self.assertIn("LIHC", tooltip)
        self.assertIn("次点", tooltip)

        # ...and in the table
        self.page.click("#tabTable")
        self.page.wait_for_function(
            "() => document.getElementById('tableNotice').hidden", timeout=15000
        )
        headers = self.page.eval_on_selector_all(
            "#matrixTable thead th", "els => els.map(e => e.textContent.trim())"
        )
        self.assertIn("類似がん種 (TCGA)", headers[4])
        cell = self.page.eval_on_selector(
            "#matrixTable tbody tr:has(td.name a:text-is('HEP G2')) td:nth-child(5)",
            "e => ({text: e.textContent, title: e.title})",
        )
        self.assertIn("LIHC", cell["text"])
        self.assertIn("肝細胞がん", cell["text"])
        # The runners-up live in the title so the column stays readable.
        self.assertEqual(cell["title"].count("\n"), 2)

    def test_the_tsv_export_carries_the_similarity(self):
        tsv = self.page.evaluate(
            """async () => {
              const r = await fetch('/api/expression.tsv', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({genes: 'GAPDH', metric: 'ntpm'}),
              });
              return r.text();
            }"""
        )
        lines = tsv.strip().split("\n")
        self.assertIn("Similar TCGA cohorts (top 3)", lines[0])
        hepg2 = [l for l in lines if l.startswith("HEP G2\t")]
        self.assertEqual(len(hepg2), 1)
        self.assertIn("LIHC (rho=", hepg2[0])

    def test_the_filter_panel_collapses_and_gives_the_room_to_the_sheet(self):
        self.run_genes("ALB, EGFR, GFAP")
        wide_before = self.page.eval_on_selector(
            "#hmSizer", "e => parseFloat(e.style.width)"
        )
        self.assertFalse(self.page.eval_on_selector("#controls", "e => e.hidden"))

        self.page.click("#sidebarToggle")
        self.page.wait_for_timeout(500)
        self.assertTrue(self.page.eval_on_selector("#controls", "e => e.hidden"))
        self.assertEqual(
            self.page.get_attribute("#sidebarToggle", "aria-expanded"), "false"
        )
        wide_after = self.page.eval_on_selector(
            "#hmSizer", "e => parseFloat(e.style.width)"
        )
        self.assertGreater(wide_after, wide_before, "the sheet must reclaim the space")

        self.page.click("#sidebarToggle")
        self.page.wait_for_timeout(500)
        self.assertFalse(self.page.eval_on_selector("#controls", "e => e.hidden"))
        self.assertEqual(
            self.page.eval_on_selector("#hmSizer", "e => parseFloat(e.style.width)"),
            wide_before,
        )

    def test_the_collapsed_panel_still_reports_the_match_count(self):
        """The count lives inside the panel, so hiding it would hide the one
        number that says what the filters are doing."""
        self.run_genes("GAPDH")
        self.page.wait_for_timeout(500)
        self.assertEqual(self.page.inner_text("#sidebarCount").strip(), "")
        self.page.click("#sidebarToggle")
        self.page.wait_for_timeout(500)
        total = self.page.evaluate("() => state.meta.cellLineCount.toLocaleString()")
        self.assertEqual(self.page.inner_text("#sidebarCount").strip(), total)

    def test_the_collapsed_state_survives_a_reload(self):
        self.page.click("#sidebarToggle")
        self.page.wait_for_timeout(300)
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_timeout(400)
        self.assertTrue(self.page.eval_on_selector("#controls", "e => e.hidden"))
        self.page.click("#sidebarToggle")
        self.page.wait_for_timeout(300)
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_timeout(400)
        self.assertFalse(self.page.eval_on_selector("#controls", "e => e.hidden"))

    def test_the_title_bar_scrolls_away(self):
        """It is read once; the vertical space matters on every scroll after."""
        self.assertEqual(
            self.page.eval_on_selector(".topbar", "e => getComputedStyle(e).position"),
            "relative",
        )
        # The source panel hangs off the header, so it must still be anchored.
        self.page.click("#datasetChip")
        self.page.wait_for_timeout(200)
        box = self.page.eval_on_selector("#sourcePanel", "e => e.getBoundingClientRect().top")
        self.assertLess(box, 200, "the source panel must stay under the chip")

    def test_the_table_shows_every_matching_cell_line(self):
        """It used to stop at 500 rows, so cell lines went missing from the
        table depending on the sort order."""
        self.run_genes("GAPDH, EGFR, ALB")
        self.page.click("#tabTable")
        self.page.wait_for_function(
            "() => document.getElementById('tableNotice').hidden", timeout=15000
        )
        counts = self.page.evaluate(
            """() => ({
              rendered: document.querySelectorAll('#matrixTable tbody tr').length,
              matching: state.view.length,
            })"""
        )
        self.assertEqual(counts["rendered"], counts["matching"])
        self.assertGreater(counts["matching"], 40)
        names = self.page.eval_on_selector_all(
            "#matrixTable tbody tr td.name", "els => els.map(e => e.textContent.trim())"
        )
        self.assertIn("Kelly", names)
        self.assertIn("LNCAP", names)

    def test_a_resort_mid_render_does_not_leave_a_mixed_table(self):
        """The body is filled a chunk at a time, so a render that a re-sort
        superseded has to stop rather than interleave its rows."""
        self.run_genes("GAPDH, EGFR, ALB")
        self.page.click("#tabTable")
        self.page.wait_for_timeout(200)
        self.page.select_option("#sortSelect", "name")
        self.page.select_option("#sortSelect", "organ")
        self.page.select_option("#sortSelect", "name")
        self.page.wait_for_function(
            "() => document.getElementById('tableNotice').hidden", timeout=15000
        )
        rendered = self.page.eval_on_selector_all(
            "#matrixTable tbody tr td.name", "els => els.map(e => e.textContent.trim())"
        )
        self.assertEqual(rendered, self.visible_names(999))

    def test_no_cell_line_is_lost_when_grouping_by_organ(self):
        """The heart of "由来臓器で並べると細胞が消える": every cell line the
        name ordering shows must still be on screen in the organ ordering."""
        self.run_genes("GAPDH")
        self.page.select_option("#sortSelect", "name")
        self.page.wait_for_timeout(600)
        by_name = sorted(self.visible_names(999))
        self.page.select_option("#sortSelect", "organ")
        self.page.wait_for_timeout(600)
        by_organ = sorted(self.visible_names(999))
        self.assertEqual(by_name, by_organ)
        self.assertIn("Kelly", by_organ)

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

    def test_the_sheet_never_grows_past_the_screen(self):
        """More genes narrow the columns; they must not push the heatmap off
        to the right, so a laptop never scrolls sideways to reach the last
        gene."""
        widths = self.page.evaluate(
            "() => [1, 3, 8, 20, 60, 200].map(n => geneColumnWidth(900, n))"
        )
        self.assertTrue(
            all(w * n <= 900 for w, n in zip(widths, [1, 3, 8, 20, 60, 200])),
            "columns must fit the room available: {}".format(widths),
        )
        self.assertTrue(
            all(a >= b for a, b in zip(widths, widths[1:])),
            "columns must not grow as genes are added: {}".format(widths),
        )
        self.assertTrue(all(w > 0 for w in widths), widths)

        for genes in ("ALB, KLK3",
                      "ALB, KLK3, PTPRC, GAPDH, VIM, EGFR, MKI67, CD19",
                      "ALB, KLK3, PTPRC, GAPDH, VIM, EGFR, MKI67, CD19, TP53, MYC, "
                      "ESR1, AR, GFAP, SOX2, MITF, PMEL, TYR, CDH1, CDH2, MUC1"):
            with self.subTest(genes=genes.count(",") + 1):
                self.run_genes(genes)
                fits = self.page.evaluate(
                    """() => {
                      const v = document.getElementById('hmViewport');
                      const sheet = parseFloat(
                        document.getElementById('hmSizer').style.width);
                      return {sheet, room: v.clientWidth, scroll: v.scrollWidth};
                    }"""
                )
                self.assertLessEqual(fits["sheet"], fits["room"])
                self.assertLessEqual(fits["scroll"], fits["room"])

    def test_narrow_columns_thin_the_gene_labels_out(self):
        """Rotated symbols need about 14px of pitch; past that they overprint
        into an unreadable smear, so only every Nth column is labelled."""
        steps = self.page.evaluate(
            "() => [132, 44, 14, 7, 4].map(w => geneLabelStep(w))"
        )
        self.assertEqual(steps[:3], [1, 1, 1])
        self.assertEqual(steps[3], 2)
        self.assertEqual(steps[4], 4)

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
