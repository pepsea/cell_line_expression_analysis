"""End-to-end tests for ingestion, querying and the HTTP API.

Run with:  python -m pytest tests -q      (or: python tests/test_pipeline.py)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hpa_cellexp import columns as C  # noqa: E402
from hpa_cellexp import reference as R  # noqa: E402
from hpa_cellexp.api import create_app, split_gene_tokens  # noqa: E402
from hpa_cellexp.ingest import build_database  # noqa: E402
from hpa_cellexp.queries import Database  # noqa: E402

EXPRESSION = """Gene\tGene name\tCell line\tTPM\tpTPM\tnTPM
ENSG00000163631\tALB\tHEP G2\t910.4\t880.1\t945.2
ENSG00000163631\tALB\tA-549\t0.0\t0.0\t0.0
ENSG00000163631\tALB\tK-562\t0.1\t0.1\t0.1
ENSG00000146648\tEGFR\tHEP G2\t12.5\t12.0\t11.8
ENSG00000146648\tEGFR\tA-549\t88.3\t85.0\t90.1
ENSG00000146648\tEGFR\tK-562\t0.4\t0.4\t0.5
ENSG00000081237\tPTPRC\tHEP G2\t0.2\t0.2\t0.2
ENSG00000081237\tPTPRC\tA-549\t0.3\t0.3\t0.3
ENSG00000081237\tPTPRC\tK-562\t210.0\t205.0\t221.7
"""

# Deliberately uses different header spellings from the expression file to
# exercise the alias resolution.
METADATA = """Cell line\tCellosaurus ID\tPrimary tissue\tDisease\tSpecies
HEP G2\tCVCL_0027\tliver\tHepatocellular carcinoma\tHuman
A-549\tCVCL_0023\tlung\tLung carcinoma\tHuman
K-562\tCVCL_0004\tbone marrow\tChronic myeloid leukemia\tHuman
"""

TCGA = """TCGA cancer\tCell line\tRank\tNormalized enrichment score\tSpearman correlation
LIHC\tHEP G2\t1\t2.41\t0.83
LUAD\tA-549\t1\t2.20\t0.79
LAML\tK-562\t1\t2.05\t0.75
"""


def write(directory: str, name: str, text: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def write_zip(directory: str, name: str, member: str, text: str) -> str:
    path = os.path.join(directory, name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return path


class ColumnTests(unittest.TestCase):
    def test_header_normalisation(self):
        self.assertEqual(C.normalise_header("  Cell line "), "cell_line")
        self.assertEqual(C.normalise_header("Cell_Line"), "cell_line")
        self.assertEqual(C.normalise_header("﻿Gene name"), "gene_name")
        self.assertEqual(C.normalise_header("Normalized enrichment score"),
                         "normalized_enrichment_score")

    def test_resolve_aliases_and_missing(self):
        header = ["Gene", "Gene name", "Cell_line", "nTPM"]
        self.assertEqual(C.resolve(header, C.GENE_ID), 0)
        self.assertEqual(C.resolve(header, C.GENE_SYMBOL), 1)
        self.assertEqual(C.resolve(header, C.CELL_LINE), 2)
        self.assertEqual(C.resolve(header, C.NTPM), 3)
        self.assertIsNone(C.resolve(header, C.TPM))
        with self.assertRaises(C.MissingColumn):
            C.resolve(header, C.TPM, required=True)


class ReferenceTests(unittest.TestCase):
    def test_tcga_organ_map(self):
        self.assertEqual(R.tcga_organ_map()["LUAD"], "Lung")
        self.assertEqual(R.tcga_organ_map()["LAML"], "Bone marrow")

    def test_organ_from_text_prefers_specific_patterns(self):
        self.assertEqual(R.organ_from_text("Cholangiocarcinoma"), "Bile duct")
        self.assertEqual(R.organ_from_text("Acute myeloid leukemia"), "Bone marrow")
        self.assertEqual(R.organ_from_text("Small intestine"), "Small intestine")
        self.assertEqual(R.organ_from_text(None, "cutaneous melanoma"), "Skin")
        self.assertIsNone(R.organ_from_text("", None))

    def test_name_key_strips_punctuation(self):
        self.assertEqual(R.name_key("HEP G2"), "HEPG2")
        self.assertEqual(R.name_key("MDA-MB-231"), "MDAMB231")
        self.assertEqual(R.name_key("U-266/70"), "U26670")
        self.assertEqual(R.name_key(None), "")

    def test_japanese_labels(self):
        self.assertEqual(R.label_ja("Lung"), "肺")
        self.assertEqual(R.label_ja("Bone marrow"), "骨髄")
        self.assertEqual(R.label_ja("Homo sapiens"), "ヒト")
        self.assertIsNone(R.label_ja("Nonexistent organ"))

    def test_every_reference_organ_has_a_japanese_label(self):
        """A new organ in the keyword table without a label breaks JA search."""
        organs = set(R.tcga_organ_map().values())
        organs.update(organ for _, organ in R._organ_keywords())
        missing = sorted(o for o in organs if not R.label_ja(o))
        self.assertEqual(missing, [], "organs missing from labels_ja.tsv: {}".format(missing))

    def test_species_and_cvcl(self):
        self.assertEqual(R.normalise_species("Human"), "Homo sapiens")
        self.assertEqual(R.normalise_species(None), "Homo sapiens")
        self.assertEqual(R.extract_cvcl("RRID:CVCL_0027"), "CVCL_0027")
        self.assertIsNone(R.extract_cvcl("n/a"))

    def test_cellosaurus_url_falls_back_to_search(self):
        self.assertEqual(R.cellosaurus_url("HeLa", "CVCL_0030"),
                         "https://www.cellosaurus.org/CVCL_0030")
        self.assertIn("search?input=HEP%20G2", R.cellosaurus_url("HEP G2", None))


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.db_path = os.path.join(d, "test.sqlite")
        cls.report = build_database(
            db_path=cls.db_path,
            # zipped input exercises the streaming zip reader
            expression_path=write_zip(d, "rna_celline.tsv.zip", "rna_celline.tsv", EXPRESSION),
            metadata_paths=[write(d, "cell_line_analysis_data.tsv", METADATA)],
            tcga_path=write(d, "tcga.tsv", TCGA),
            release="TEST",
            progress=False,
        )
        cls.db = Database(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_report_counts(self):
        self.assertEqual(self.report.genes, 3)
        self.assertEqual(self.report.cell_lines, 3)
        self.assertEqual(self.report.expression_rows, 9)
        self.assertEqual(self.report.metrics, ["tpm", "ptpm", "ntpm"])
        self.assertEqual(self.report.tcga_rows, 3)

    def test_metadata_is_attached(self):
        rows = {c["name"]: c for c in self.db.cell_lines()}
        self.assertEqual(rows["HEP G2"]["organ"], "Liver")
        self.assertEqual(rows["K-562"]["organ"], "Bone marrow")
        self.assertEqual(rows["A-549"]["species"], "Homo sapiens")
        self.assertEqual(rows["A-549"]["cellosaurusId"], "CVCL_0023")
        self.assertEqual(rows["A-549"]["databaseUrl"], "https://www.cellosaurus.org/CVCL_0023")

    def test_gene_resolution_by_symbol_and_ensembl(self):
        found, missing = self.db.resolve_genes(["alb", "ENSG00000146648", "NOPE", "ALB"])
        self.assertEqual([g["symbol"] for g in found], ["ALB", "EGFR"])
        self.assertEqual(missing, ["NOPE"])

    def test_matrix_shape_and_values(self):
        result = self.db.expression_matrix(["ALB", "EGFR"], metric="ntpm")
        self.assertEqual([g["symbol"] for g in result["genes"]], ["ALB", "EGFR"])
        names = [c["name"] for c in result["cellLines"]]
        self.assertEqual(len(result["values"]), 2)
        self.assertEqual(len(result["values"][0]), len(names))
        self.assertAlmostEqual(result["values"][0][names.index("HEP G2")], 945.2)
        self.assertAlmostEqual(result["values"][1][names.index("A-549")], 90.1)
        self.assertEqual(result["values"][0][names.index("A-549")], 0.0)

    def test_organ_filter_restricts_columns(self):
        result = self.db.expression_matrix(["ALB"], metric="ntpm", organs=["Liver"])
        self.assertEqual([c["name"] for c in result["cellLines"]], ["HEP G2"])
        self.assertEqual(len(result["values"][0]), 1)

    def test_unknown_metric_rejected(self):
        with self.assertRaises(ValueError):
            self.db.expression_matrix(["ALB"], metric="rpkm")

    def test_facets(self):
        facets = self.db.facets()
        self.assertEqual({f["value"] for f in facets["organs"]}, {"Liver", "Lung", "Bone marrow"})
        self.assertEqual([f["value"] for f in facets["species"]], ["Homo sapiens"])
        self.assertEqual(facets["organUnassigned"], 0)

    def test_facets_carry_japanese_labels(self):
        """The facet list has to be searchable in Japanese - HPA values are English."""
        facets = self.db.facets()
        organs = {f["value"]: f["labelJa"] for f in facets["organs"]}
        self.assertEqual(organs["Lung"], "肺")
        self.assertEqual(organs["Liver"], "肝臓")
        self.assertEqual(organs["Bone marrow"], "骨髄")
        self.assertEqual(facets["species"][0]["labelJa"], "ヒト")

    def test_cell_line_search_ignores_punctuation(self):
        """"hepg2" must find "HEP G2"; "a549" must find "A-549"."""
        for query, expected in [
            ("hep g2", "HEP G2"),
            ("hepg2", "HEP G2"),
            ("HEPG2", "HEP G2"),
            ("a549", "A-549"),
            ("a-549", "A-549"),
            ("k562", "K-562"),
        ]:
            with self.subTest(query=query):
                rows = self.db.cell_lines(name_query=query)
                self.assertEqual([r["name"] for r in rows], [expected])

    def test_cell_line_search_still_does_substrings(self):
        self.assertEqual(
            sorted(r["name"] for r in self.db.cell_lines(name_query="5")),
            ["A-549", "K-562"],
        )

    def test_cell_line_search_combines_with_organ_filter(self):
        self.assertEqual(self.db.cell_lines(name_query="hepg2", organs=["Lung"]), [])
        self.assertEqual(
            [r["name"] for r in self.db.cell_lines(name_query="hepg2", organs=["Liver"])],
            ["HEP G2"],
        )


class TcgaFallbackTests(unittest.TestCase):
    """With no metadata file the organ must still come from the TCGA table."""

    def test_organ_inferred_from_tcga(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "t.sqlite")
            build_database(
                db_path=db_path,
                expression_path=write(d, "e.tsv", EXPRESSION),
                tcga_path=write(d, "t.tsv", TCGA),
                progress=False,
            )
            rows = {c["name"]: c for c in Database(db_path).cell_lines()}
            self.assertEqual(rows["HEP G2"]["organ"], "Liver")
            self.assertEqual(rows["A-549"]["organ"], "Lung")
            self.assertEqual(rows["K-562"]["organ"], "Bone marrow")
            # No CVCL available, so the link must degrade to a name search.
            self.assertIn("search?input=", rows["K-562"]["databaseUrl"])


class LegacySchemaTests(unittest.TestCase):
    """An older release with TPM only and no Ensembl column must still load."""

    def test_tpm_only_and_symbol_only(self):
        text = "Gene name\tCell line\tTPM\nALB\tHEP G2\t900\nALB\tA-549\t0\n"
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "t.sqlite")
            report = build_database(
                db_path=db_path, expression_path=write(d, "e.tsv", text), progress=False
            )
            self.assertEqual(report.metrics, ["tpm"])
            db = Database(db_path)
            result = db.expression_matrix(["ALB"], metric="tpm")
            self.assertEqual(len(result["cellLines"]), 2)
            self.assertIsNone(db.expression_matrix(["ALB"], metric="ntpm")["values"][0][0])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        db_path = os.path.join(d, "api.sqlite")
        build_database(
            db_path=db_path,
            expression_path=write(d, "e.tsv", EXPRESSION),
            metadata_paths=[write(d, "m.tsv", METADATA)],
            tcga_path=write(d, "t.tsv", TCGA),
            release="TEST",
            progress=False,
        )
        cls.client = TestClient(create_app(db_path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_meta(self):
        body = self.client.get("/api/meta").json()
        self.assertEqual(body["release"], "TEST")
        self.assertFalse(body["isDemo"])
        self.assertEqual(body["geneCount"], 3)
        self.assertEqual({f["value"] for f in body["facets"]["organs"]},
                         {"Liver", "Lung", "Bone marrow"})

    def test_index_and_assets_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/styles.css").status_code, 200)

    def test_expression_accepts_pasted_string(self):
        body = self.client.post(
            "/api/expression",
            json={"genes": "ALB EGFR; PTPRC\nMISSING", "metric": "ntpm"},
        ).json()
        self.assertEqual([g["symbol"] for g in body["genes"]], ["ALB", "EGFR", "PTPRC"])
        self.assertEqual(body["unmatchedGenes"], ["MISSING"])
        self.assertTrue(all("databaseUrl" in c for c in body["cellLines"]))
        self.assertEqual({c["tcga"] for c in body["cellLines"]}, {"LIHC", "LUAD", "LAML"})

    def test_expression_requires_genes(self):
        self.assertEqual(self.client.post("/api/expression", json={"genes": ""}).status_code, 400)

    def test_expression_rejects_bad_metric(self):
        response = self.client.post("/api/expression", json={"genes": "ALB", "metric": "xx"})
        self.assertEqual(response.status_code, 400)

    def test_gene_limit_enforced(self):
        many = ",".join("G{}".format(i) for i in range(300))
        self.assertEqual(
            self.client.post("/api/expression", json={"genes": many}).status_code, 400
        )

    def test_cell_line_filtering(self):
        body = self.client.get("/api/cell-lines", params={"organ": "Liver"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["results"][0]["name"], "HEP G2")

    def test_tsv_export(self):
        response = self.client.post(
            "/api/expression.tsv", json={"genes": "ALB,EGFR", "metric": "ntpm"}
        )
        self.assertEqual(response.status_code, 200)
        lines = response.text.strip().split("\n")
        self.assertEqual(len(lines), 4)  # header + 3 cell lines
        self.assertIn("ALB (ntpm)", lines[0])
        self.assertIn("cellosaurus.org", response.text)

    def test_gene_autocomplete(self):
        body = self.client.get("/api/genes", params={"q": "eg"}).json()
        self.assertEqual([g["symbol"] for g in body["results"]], ["EGFR"])


class CliTests(unittest.TestCase):
    def test_serve_uses_the_database_flag(self):
        """Regression: `serve --database X` must not fall back to the default.

        `hpa_cellexp.api` builds a module-level app from the default path, so
        the CLI has to hand uvicorn an app built from --database rather than an
        import string that resolves to the already-imported module.
        """
        import inspect as _inspect

        from hpa_cellexp import __main__ as cli

        source = _inspect.getsource(cli)
        self.assertNotIn("from .api import DEFAULT_DB_PATH", source)
        self.assertIn("create_app(db_path)", source)

    def test_default_db_path_is_importable_without_api(self):
        from hpa_cellexp.config import DEFAULT_DB_PATH

        self.assertTrue(DEFAULT_DB_PATH.endswith(".sqlite"))

    def test_serve_reports_missing_database(self):
        with tempfile.TemporaryDirectory() as d:
            from hpa_cellexp.__main__ import main

            code = main(["--database", os.path.join(d, "nope.sqlite"), "serve"])
            self.assertEqual(code, 1)


class TokenTests(unittest.TestCase):
    def test_split_gene_tokens(self):
        self.assertEqual(split_gene_tokens("A, B;C\nD|E  F"), ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(split_gene_tokens(["A", "B C"]), ["A", "B", "C"])
        self.assertEqual(split_gene_tokens(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
