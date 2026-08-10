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

    def test_camel_case_headers(self):
        """"CellLineName" has to reduce to the same key as "Cell line name"."""
        self.assertEqual(C.normalise_header("CellLineName"), "cell_line_name")
        self.assertEqual(C.normalise_header("SiteOfOrigin"), "site_of_origin")
        self.assertEqual(C.normalise_header("OncotreeLineage"), "oncotree_lineage")
        self.assertEqual(C.normalise_header("nTPM"), "n_tpm")
        self.assertEqual(C.normalise_header("pTPM"), "p_tpm")

    def test_metric_headers_still_resolve(self):
        header = ["Gene", "Gene name", "Cell line", "TPM", "pTPM", "nTPM"]
        self.assertEqual(C.resolve(header, C.TPM), 3)
        self.assertEqual(C.resolve(header, C.PTPM), 4)
        self.assertEqual(C.resolve(header, C.NTPM), 5)

    def test_unconventional_metadata_headers(self):
        header = ["CellLineName", "SiteOfOrigin", "Histology", "OncotreeLineage"]
        self.assertEqual(C.resolve(header, C.CELL_LINE), 0)
        self.assertEqual(C.resolve(header, C.TISSUE), 1)
        self.assertEqual(C.resolve(header, C.DISEASE), 2)
        self.assertEqual(C.resolve(header, C.ORGAN), 3)

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

    def test_substring_hazards_resolve_to_the_right_organ(self):
        """First-substring-wins makes ordering load-bearing.

        Each of these pairs a short pattern with a longer term that contains
        it; every one of them was resolving to the wrong organ before.
        """
        cases = {
            # "renal" sits inside "adrenal"
            "Adrenal gland": "Adrenal gland",
            "Adrenocortical carcinoma": "Adrenal gland",
            "Pheochromocytoma": "Adrenal gland",
            "Renal cell carcinoma": "Kidney",
            # "thyroid" sits inside "parathyroid"
            "Parathyroid adenoma": "Parathyroid gland",
            "Thyroid carcinoma": "Thyroid gland",
            # ovarian germ cell tumours are not testicular
            "Ovarian germ cell tumor": "Ovary",
            "Testicular germ cell tumor": "Testis",
            # "bladder" sits inside "gallbladder"
            "Gallbladder carcinoma": "Gallbladder",
            "Urinary bladder carcinoma": "Urinary bladder",
            # "rectal" sits inside "colorectal"
            "Colorectal adenocarcinoma": "Colon",
            "Rectal adenocarcinoma": "Rectum",
            # "bone" sits inside "bone marrow"
            "Bone marrow": "Bone marrow",
            "Osteosarcoma": "Bone",
            # "oral" used to be a bare pattern and swallowed "temporal"
            "Temporal lobe glioma": "Brain",
            "Oral cavity squamous cell carcinoma": "Head and neck",
            # cervical lymph node is lymphoid, not cervix
            "Cervical lymph node": "Lymphoid tissue",
            "Cervical adenocarcinoma": "Cervix",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(R.organ_from_text(text), expected)

    def test_intestinal_terms_resolve(self):
        """Regression: "Large intestine" matched nothing, so colorectal lines
        could end up with no organ at all."""
        self.assertEqual(R.organ_from_text("Large intestine"), "Colon")
        self.assertEqual(R.organ_from_text("Sigmoid colon"), "Colon")
        self.assertEqual(R.organ_from_text("Caecum"), "Colon")
        self.assertEqual(R.organ_from_text("Small intestine"), "Small intestine")
        self.assertEqual(R.organ_from_text("Duodenum"), "Small intestine")
        # Only the unqualified term falls through to the broad category.
        self.assertEqual(R.organ_from_text("Intestine"), "Intestine")

    def test_the_whole_nervous_system_is_one_organ(self):
        """However a source words it, neural lines have to land in the same
        bucket - otherwise Kelly is missing from 脳 and nowhere else obvious."""
        for value in ("Nervous system", "Central nervous system", "CNS",
                      "Peripheral nervous system", "Sympathetic nervous system"):
            with self.subTest(value=value):
                self.assertEqual(R.normalise_organ(value), "Brain")
        self.assertEqual(R.organ_from_text("Neuroblastoma"), "Brain")

    def test_nervous_system_is_not_split_by_the_disease_text(self):
        """It used to be a "broad" organ, so "Nervous system" + neuroblastoma
        was refined into a second organ the reader never thinks to open."""
        self.assertFalse(R.is_broad_organ("Nervous system"))
        # The refinement itself still works where it was actually wanted.
        self.assertTrue(R.is_broad_organ("Intestine"))

    def test_broad_organs(self):
        self.assertTrue(R.is_broad_organ("Intestine"))
        self.assertTrue(R.is_broad_organ("  gastrointestinal tract "))
        self.assertFalse(R.is_broad_organ("Colon"))
        self.assertFalse(R.is_broad_organ(None))

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


class OrganAssignmentTests(unittest.TestCase):
    """Where 由来臓器 comes from, and that it is traceable."""

    EXPR = (
        "Gene name\tCell line\tnTPM\n"
        "GAPDH\tCACO-2\t900\n"
        "GAPDH\tHT-29\t800\n"
        "GAPDH\tA-549\t700\n"
        "GAPDH\tMYSTERY-1\t600\n"
    )

    def _build(self, metadata_text, tcga_text=None):
        d = self.tmp.name
        db_path = os.path.join(d, "organ-{}.sqlite".format(abs(hash(metadata_text)) % 10 ** 8))
        build_database(
            db_path=db_path,
            expression_path=write(d, "e-{}.tsv".format(abs(hash(metadata_text)) % 10 ** 8), self.EXPR),
            metadata_paths=[write(d, "m-{}.tsv".format(abs(hash(metadata_text)) % 10 ** 8), metadata_text)]
            if metadata_text
            else [],
            tcga_path=write(d, "t-{}.tsv".format(abs(hash(str(tcga_text))) % 10 ** 8), tcga_text)
            if tcga_text
            else None,
            progress=False,
        )
        return {c["name"]: c for c in Database(db_path).cell_lines()}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_broad_organ_column_is_refined_by_disease(self):
        """A source that files colorectal lines under the whole tract must
        still let them be found under 結腸 / Colon."""
        rows = self._build(
            "Cell line\tOrgan\tDisease\n"
            "CACO-2\tIntestine\tColorectal adenocarcinoma\n"
            "HT-29\tIntestine\tColon adenocarcinoma\n"
            "A-549\tRespiratory system\tLung adenocarcinoma\n"
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertEqual(rows["HT-29"]["organ"], "Colon")
        self.assertEqual(rows["A-549"]["organ"], "Lung")
        self.assertIn("Intestine", rows["CACO-2"]["organSource"])

    def test_specific_organ_column_is_kept_verbatim(self):
        rows = self._build(
            "Cell line\tOrgan\tDisease\n"
            "CACO-2\tColon\tColorectal adenocarcinoma\n"
            "A-549\tLung\tLung adenocarcinoma\n"
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertEqual(rows["CACO-2"]["organSource"], "メタデータの organ 列")

    def test_organ_inferred_from_disease_when_no_organ_column(self):
        rows = self._build(
            "Cell line\tDisease\n"
            "CACO-2\tColorectal adenocarcinoma\n"
            "A-549\tLung adenocarcinoma\n"
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertEqual(rows["CACO-2"]["organSource"], "疾患/組織名からの推定")

    def test_tcga_derived_organ_is_flagged_as_approximate(self):
        """TCGA similarity is not provenance; it must be visibly weaker."""
        rows = self._build(
            None,
            "TCGA cancer\tCell line\tRank\nCOAD\tCACO-2\t1\nLUAD\tA-549\t1\n",
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertIn("TCGA", rows["CACO-2"]["organSource"])
        self.assertIn("参考値", rows["CACO-2"]["organSource"])

    def test_unassignable_cell_line_gets_no_organ(self):
        rows = self._build("Cell line\tDisease\nCACO-2\tColorectal adenocarcinoma\n")
        self.assertIsNone(rows["MYSTERY-1"]["organ"])
        self.assertIsNone(rows["MYSTERY-1"]["organSource"])

    def test_organ_facet_finds_the_refined_lines(self):
        d = self.tmp.name
        db_path = os.path.join(d, "facet.sqlite")
        build_database(
            db_path=db_path,
            expression_path=write(d, "ef.tsv", self.EXPR),
            metadata_paths=[
                write(
                    d,
                    "mf.tsv",
                    "Cell line\tOrgan\tDisease\n"
                    "CACO-2\tIntestine\tColorectal adenocarcinoma\n"
                    "HT-29\tIntestine\tColon adenocarcinoma\n"
                    "A-549\tIntestine\tLung adenocarcinoma\n",
                )
            ],
            progress=False,
        )
        db = Database(db_path)
        colon = sorted(r["name"] for r in db.cell_lines(organs=["Colon"]))
        self.assertEqual(colon, ["CACO-2", "HT-29"])


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
        # tcga is a ranked list now, not a single code.
        top = {c["tcga"][0]["cancer"] for c in body["cellLines"] if c["tcga"]}
        self.assertEqual(top, {"LIHC", "LUAD", "LAML"})
        hepg2 = next(c for c in body["cellLines"] if c["name"] == "HEP G2")
        self.assertEqual(hepg2["tcga"][0]["nameJa"], "肝細胞がん")
        self.assertEqual(hepg2["tcga"][0]["rank"], 1)

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

    def test_tsv_filename_carries_a_timestamp(self):
        import re as _re

        response = self.client.post("/api/expression.tsv", json={"genes": "ALB"})
        disposition = response.headers["content-disposition"]
        self.assertRegex(
            disposition,
            r'filename="hpa_cell_line_expression_\d{8}_\d{6}\.tsv"',
            "export filename should be stamped with the date and time",
        )

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


class OrderingTests(unittest.TestCase):
    """The order on screen has to match the labels on screen."""

    def test_natural_name_order(self):
        names = ["NCI-H1650", "NCI-H2", "NCI-H23", "NCI-H460", "U-2 OS", "U-138 MG"]
        self.assertEqual(
            sorted(names, key=R.natural_key),
            ["NCI-H2", "NCI-H23", "NCI-H460", "NCI-H1650", "U-2 OS", "U-138 MG"],
        )

    def test_organs_use_the_canonical_anatomical_order(self):
        """Sorting by the English value while showing Japanese labels produced
        a sequence that read as random; the order comes from labels_ja.tsv."""
        organs = ["Colon", "Bone marrow", "Lung", "Cervix", "Brain", "Breast"]
        self.assertEqual(
            sorted(organs, key=lambda o: (R.display_rank(o), o)),
            ["Bone marrow", "Brain", "Lung", "Colon", "Breast", "Cervix"],
        )

    def test_unlisted_organs_sort_last(self):
        self.assertGreater(R.display_rank("Something Unlisted"), R.display_rank("Vasculature"))
        self.assertGreater(R.display_rank(None), R.display_rank("Something Unlisted"))


class CuratedOrganTests(unittest.TestCase):
    """The built-in cell line list, used when the dataset says nothing."""

    def test_exact_and_prefix_lookup(self):
        self.assertEqual(R.organ_from_cell_line_name("CACO-2")[0], "Colon")
        self.assertEqual(R.organ_from_cell_line_name("caco2")[0], "Colon")
        self.assertEqual(R.organ_from_cell_line_name("HEP G2")[0], "Liver")
        self.assertEqual(R.organ_from_cell_line_name("MDA-MB-468")[0], "Breast")
        self.assertEqual(R.organ_from_cell_line_name("KYSE-150")[0], "Esophagus")
        self.assertIsNone(R.organ_from_cell_line_name("NOT-A-CELL-LINE"))

    def test_exact_entry_beats_its_own_series_prefix(self):
        """SK-N-MC is an Ewing sarcoma line, not a neuroblastoma."""
        self.assertEqual(R.organ_from_cell_line_name("SK-N-SH")[0], "Brain")
        self.assertEqual(R.organ_from_cell_line_name("SK-N-MC")[0], "Bone")

    def test_nci_h_series_is_not_blanket_lung(self):
        self.assertEqual(R.organ_from_cell_line_name("NCI-H1650")[0], "Lung")
        self.assertEqual(R.organ_from_cell_line_name("NCI-H929")[0], "Bone marrow")

    def test_every_curated_organ_has_a_label(self):
        exact, prefixes = R._cell_line_organs()
        organs = set(exact.values()) | {organ for _, organ in prefixes}
        missing = sorted(o for o in organs if not R.label_ja(o))
        self.assertEqual(missing, [], "organs missing from labels_ja.tsv: {}".format(missing))

    def test_results_are_labelled_as_estimates(self):
        for name in ("CACO-2", "MDA-MB-468"):
            self.assertIn("参考値", R.organ_from_cell_line_name(name)[1])

    def test_the_common_glioma_and_neuroblastoma_lines_are_covered(self):
        """These had no 由来臓器 at all, so they vanished from every organ
        grouping - the report was "細胞がなくなる"."""
        for name in ("U-87 MG", "U-138 MG", "U-251 MG", "u87mg", "SF-268", "KNS-42"):
            with self.subTest(cell_line=name):
                self.assertEqual(R.organ_from_cell_line_name(name)[0], "Brain")
        for name in ("Kelly", "SK-N-BE(2)", "SK-N-DZ", "GI-ME-N"):
            with self.subTest(cell_line=name):
                self.assertEqual(R.organ_from_cell_line_name(name)[0], "Brain")


class GeneLinkTests(unittest.TestCase):
    """Clicking a gene has to reach that gene's page in HPA - the resource the
    expression values come from."""

    def test_canonical_url_uses_the_ensembl_id_and_symbol(self):
        self.assertEqual(
            R.proteinatlas_url("EGFR", "ENSG00000146648"),
            "https://www.proteinatlas.org/ENSG00000146648-EGFR",
        )

    def test_a_missing_half_falls_back_to_something_that_still_resolves(self):
        self.assertEqual(
            R.proteinatlas_url("EGFR", None), "https://www.proteinatlas.org/search/EGFR"
        )
        self.assertEqual(
            R.proteinatlas_url(None, "ENSG00000146648"),
            "https://www.proteinatlas.org/ENSG00000146648",
        )

    def test_symbols_needing_escaping_are_quoted(self):
        self.assertNotIn(" ", R.proteinatlas_url("A B", None))

    def test_resolved_genes_carry_the_link(self):
        with tempfile.TemporaryDirectory() as d:
            from hpa_cellexp.demo import build_demo_database

            path = os.path.join(d, "demo.sqlite")
            build_demo_database(path)
            found, _ = Database(path).resolve_genes(["EGFR", "GAPDH"])
            self.assertEqual(len(found), 2)
            for gene in found:
                with self.subTest(gene=gene["symbol"]):
                    self.assertIn("proteinatlas.org", gene["hpaUrl"])
                    self.assertIn(gene["symbol"], gene["hpaUrl"])


class ReferenceFolderTests(unittest.TestCase):
    """The curated tables and cellosaurus.txt live wherever the deployment
    keeps them; config decides, not the package layout."""

    def setUp(self):
        import importlib

        from hpa_cellexp import config

        self.importlib = importlib
        self.config = config
        self.saved = {
            k: os.environ.get(k)
            for k in ("HPA_CELLEXP_REFERENCE_DIR", "HPA_CELLEXP_CELLOSAURUS")
        }
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.importlib.reload(self.config)
        self.importlib.reload(R)
        R.reload()
        self.tmp.cleanup()

    def _point_at(self, directory):
        os.environ["HPA_CELLEXP_REFERENCE_DIR"] = directory
        self.importlib.reload(self.config)
        self.importlib.reload(R)
        R.reload()

    def test_a_file_in_the_folder_wins_over_the_packaged_copy(self):
        write(self.tmp.name, "tcga_organ.tsv",
              "tcga_code\torgan\tdescription\tja\nLUAD\tPleura\tTest\tテスト\n")
        self._point_at(self.tmp.name)
        self.assertEqual(self.config.reference_origin("tcga_organ.tsv"), "external")
        self.assertEqual(R.tcga_organ_map()["LUAD"], "Pleura")
        self.assertEqual(R.tcga_name("LUAD"), ("Test", "テスト"))

    def test_the_fallback_is_per_file(self):
        """Overriding one table must not mean copying all four."""
        write(self.tmp.name, "tcga_organ.tsv",
              "tcga_code\torgan\tdescription\tja\nLUAD\tPleura\tTest\tテスト\n")
        self._point_at(self.tmp.name)
        self.assertEqual(self.config.reference_origin("labels_ja.tsv"), "packaged")
        self.assertEqual(R.label_ja("Lung"), "肺")
        self.assertEqual(R.organ_from_cell_line_name("CACO-2")[0], "Colon")

    def test_an_empty_or_missing_folder_changes_nothing(self):
        self._point_at(os.path.join(self.tmp.name, "does-not-exist"))
        self.assertEqual(self.config.reference_origin("tcga_organ.tsv"), "packaged")
        self.assertEqual(R.tcga_organ_map()["LUAD"], "Lung")

    def test_the_external_folder_reaches_the_ingest(self):
        """Not just the readers: a build has to classify by the override."""
        write(self.tmp.name, "tcga_organ.tsv",
              "tcga_code\torgan\tdescription\tja\nLUAD\tPleura\tTest\tテスト\n")
        self._point_at(self.tmp.name)
        expression = write(self.tmp.name, "e.tsv",
                           "Gene name\tCell line\tnTPM\nGAPDH\tMYSTERY-1\t100\n")
        tcga = write(self.tmp.name, "t.tsv",
                     "TCGA cancer\tCell line\tRank\tSpearman correlation\n"
                     "LUAD\tMYSTERY-1\t1\t0.8\n")
        db_path = os.path.join(self.tmp.name, "x.sqlite")
        build_database(db_path=db_path, expression_path=expression, tcga_path=tcga,
                       progress=False)
        row = Database(db_path).cell_lines()[0]
        self.assertEqual(row["organ"], "Pleura")

    def test_the_cellosaurus_path_is_configuration(self):
        """So `build` does not need --cellosaurus spelled out every time."""
        os.environ["HPA_CELLEXP_CELLOSAURUS"] = "/srv/data/cellosaurus.txt"
        self.importlib.reload(self.config)
        from hpa_cellexp import __main__ as cli

        self.importlib.reload(cli)
        args = cli.apply_config_defaults(
            cli.build_parser().parse_args(["build", "--expression", "e.tsv"])
        )
        self.assertEqual(args.cellosaurus, "/srv/data/cellosaurus.txt")
        self.assertFalse(args.cellosaurus_explicit)
        # An explicit flag still wins, and counts as explicit.
        args = cli.apply_config_defaults(
            cli.build_parser().parse_args(
                ["build", "--expression", "e.tsv", "--cellosaurus", "/other.txt"]
            )
        )
        self.assertEqual(args.cellosaurus, "/other.txt")
        self.assertTrue(args.cellosaurus_explicit)

    def test_reference_dir_is_accepted_on_either_side_of_the_subcommand(self):
        from hpa_cellexp import __main__ as cli

        parser = cli.build_parser()
        self.assertEqual(
            parser.parse_args(["--reference-dir", "/r", "organs"]).reference_dir, "/r"
        )
        self.assertEqual(
            parser.parse_args(["organs", "--reference-dir", "/r"]).reference_dir, "/r"
        )


class InputCheckTests(unittest.TestCase):
    """A missing input must be reported before the load, not after it.

    Reported for real: --cellosaurus pointed at a host path that does not
    exist inside the container, and the traceback arrived AFTER streaming
    24 million expression rows.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.expression = write(self.tmp.name, "e.tsv",
                                "Gene name\tCell line\tnTPM\nGAPDH\tA-549\t10\n")
        self.db = os.path.join(self.tmp.name, "x.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        import contextlib
        import io

        from hpa_cellexp.__main__ import main

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(argv)
        return code, err.getvalue()

    def test_a_missing_expression_file_fails_immediately(self):
        code, err = self._run(
            ["build", "--database", self.db, "--expression", "/nope/rna.tsv.zip"]
        )
        self.assertEqual(code, 1)
        self.assertIn("--expression", err)
        self.assertIn("/nope/rna.tsv.zip", err)
        self.assertFalse(os.path.exists(self.db), "nothing should have been written")

    def test_an_explicit_missing_cellosaurus_is_an_error(self):
        code, err = self._run([
            "build", "--database", self.db, "--expression", self.expression,
            "--cellosaurus", "/nope/cellosaurus.txt", "--quiet",
        ])
        self.assertEqual(code, 1)
        self.assertIn("--cellosaurus", err)
        self.assertFalse(os.path.exists(self.db))

    def test_a_configured_missing_cellosaurus_only_warns(self):
        """It is a convenience default; a machine without the download should
        still be able to build."""
        import importlib

        from hpa_cellexp import config

        saved = os.environ.get("HPA_CELLEXP_CELLOSAURUS")
        os.environ["HPA_CELLEXP_CELLOSAURUS"] = "/nope/cellosaurus.txt"
        try:
            importlib.reload(config)
            from hpa_cellexp import __main__ as cli

            importlib.reload(cli)
            import contextlib
            import io

            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["build", "--database", self.db,
                                 "--expression", self.expression, "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("continuing without it", err.getvalue())
            self.assertTrue(os.path.exists(self.db))
        finally:
            if saved is None:
                os.environ.pop("HPA_CELLEXP_CELLOSAURUS", None)
            else:
                os.environ["HPA_CELLEXP_CELLOSAURUS"] = saved
            importlib.reload(config)
            from hpa_cellexp import __main__ as cli_again

            importlib.reload(cli_again)

    def test_a_host_path_under_docker_explains_the_mounts(self):
        saved = os.environ.get("HPA_CELLEXP_DB")
        os.environ["HPA_CELLEXP_DB"] = "/data/hpa_cellexp.sqlite"
        try:
            code, err = self._run([
                "build", "--database", self.db,
                "--expression", "/Users/someone/Drive/rna_celline.tsv.zip",
            ])
            self.assertEqual(code, 1)
            self.assertIn("/source/", err)
            self.assertIn("/cellosaurus/", err)
            self.assertIn("HPA_SOURCE_DIR", err)
        finally:
            if saved is None:
                os.environ.pop("HPA_CELLEXP_DB", None)
            else:
                os.environ["HPA_CELLEXP_DB"] = saved

    def test_every_missing_input_is_listed_at_once(self):
        code, err = self._run([
            "build", "--database", self.db,
            "--expression", "/nope/rna.tsv.zip",
            "--metadata", "/nope/meta.tsv",
            "--tcga", "/nope/tcga.tsv",
        ])
        self.assertEqual(code, 1)
        for flag in ("--expression", "--metadata", "--tcga"):
            with self.subTest(flag=flag):
                self.assertIn(flag, err)


class InspectRoleTests(unittest.TestCase):
    """inspect has to answer "which of my downloads is the metadata file?".

    Reported for real: cell_line_analysis_data.tsv.zip was passed to
    --metadata, but its columns are cell_line/analysis_type/name/z_score/
    significant - long-format analysis output with no annotation at all, so
    968 cell lines fell back to a TCGA-similarity guess.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        write(self.tmp.name, "cell_line_analysis_data.tsv",
              "cell_line\tanalysis_type\tname\tz_score\tsignificant\n"
              "A-549\tTF\tSOX2\t2.1\tyes\n")
        write(self.tmp.name, "rna_celline.tsv",
              "Gene\tGene name\tCell line\tnTPM\nENSG1\tGAPDH\tA-549\t100\n")
        write(self.tmp.name, "description.tsv",
              "Cell line\tPrimary tissue\tDisease\tSpecies\n"
              "A-549\tlung\tLung carcinoma\tHuman\n")
        write(self.tmp.name, "tcga.tsv",
              "TCGA cancer\tCell line\tRank\nLUAD\tA-549\t1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _inspect(self, *paths):
        import contextlib
        import io

        from hpa_cellexp.__main__ import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["inspect", "--rows", "1"] + list(paths))
        return out.getvalue()

    def test_an_analysis_table_is_not_offered_as_metadata(self):
        text = self._inspect(os.path.join(self.tmp.name, "cell_line_analysis_data.tsv"))
        self.assertIn("--metadata としては使えません", text)
        self.assertNotIn("--metadata に使えます", text)

    def test_each_real_input_is_named_for_its_flag(self):
        text = self._inspect(self.tmp.name)
        for filename, verdict in (
            ("rna_celline.tsv", "--expression に使えます"),
            ("description.tsv", "--metadata に使えます"),
            ("tcga.tsv", "--tcga に使えます"),
        ):
            with self.subTest(filename=filename):
                section = text.split("== ")[
                    next(i for i, part in enumerate(text.split("== "))
                         if part.startswith(os.path.join(self.tmp.name, filename)))
                ]
                self.assertIn(verdict, section)

    def test_a_whole_folder_can_be_inspected_at_once(self):
        text = self._inspect(self.tmp.name)
        for filename in ("rna_celline.tsv", "description.tsv", "tcga.tsv",
                         "cell_line_analysis_data.tsv"):
            with self.subTest(filename=filename):
                self.assertIn(filename, text)

    def test_the_cellosaurus_flat_file_is_recognised(self):
        """It is not delimited, so column detection reported "no usable
        fields" on the single most useful input for 由来臓器."""
        write(self.tmp.name, "cellosaurus.txt",
              "-------------------------------------\n"
              "        CALIPHO group at the SIB\n"
              "-------------------------------------\n"
              "ID   HeLa\nAC   CVCL_0030\n"
              "CC   Derived from site: In situ; Uterine cervix.\n//\n")
        text = self._inspect(os.path.join(self.tmp.name, "cellosaurus.txt"))
        self.assertIn("--cellosaurus に使えます", text)
        self.assertNotIn("使える列なし", text)

    def test_an_unreadable_file_does_not_stop_the_scan(self):
        with open(os.path.join(self.tmp.name, "notes.txt"), "wb") as handle:
            handle.write(b"\xff\xfe not a table at all")
        text = self._inspect(self.tmp.name)
        self.assertIn("rna_celline.tsv", text)


class StaticCachingTests(unittest.TestCase):
    """index.html carries the ?v= cache buster, so it must not be the file
    that goes stale - otherwise the buster never changes and the browser keeps
    the previous app.js indefinitely."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from hpa_cellexp.demo import build_demo_database

        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "demo.sqlite")
        build_demo_database(path)
        cls.client = TestClient(create_app(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_entry_document_is_revalidated(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers.get("cache-control", ""))

    def test_the_entry_document_carries_the_current_version(self):
        from hpa_cellexp import __version__

        self.assertIn("app.js?v={}".format(__version__), self.client.get("/").text)


class SpeciesTests(unittest.TestCase):
    """Regression: the species facet listed ヒト and Homo sapiens separately."""

    def test_japanese_spellings_collapse_to_the_scientific_name(self):
        for value in ("ヒト", "ヒト由来", "人", "Human", "homo sapiens", "9606"):
            with self.subTest(value=value):
                self.assertEqual(R.normalise_species(value), "Homo sapiens")
        self.assertEqual(R.normalise_species("マウス"), "Mus musculus")

    def test_the_cellosaurus_common_name_annotation_collapses_too(self):
        """Cellosaurus writes "! Homo sapiens (Human)", which sat in the facet
        list beside a plain "Homo sapiens" as though they were two species."""
        self.assertEqual(R.normalise_species("Homo sapiens (Human)"), "Homo sapiens")
        self.assertEqual(R.normalise_species("Mus musculus (Mouse)"), "Mus musculus")
        self.assertEqual(R.normalise_species("NCBI_TaxID=9606"), "Homo sapiens")
        self.assertEqual(
            R.normalise_species("NCBI_TaxID=9606; ! Homo sapiens (Human)"), "Homo sapiens"
        )
        # A species with no alias keeps its scientific name, minus the note.
        self.assertEqual(R.normalise_species("Sus scrofa (Pig)"), "Sus scrofa")

    def test_missing_species_falls_back_to_human(self):
        self.assertEqual(R.normalise_species(None), R.DEFAULT_SPECIES)
        self.assertEqual(R.normalise_species("  "), R.DEFAULT_SPECIES)


class FacetSearchTermTests(unittest.TestCase):
    """The optional third column of labels_ja.tsv: words that find a facet
    value without changing how anything is classified."""

    def test_the_nervous_system_is_one_bucket_findable_by_its_parts(self):
        """Kelly kept disappearing because neuroblastoma lines sat in a
        separate organ from the 脳 everyone searches for."""
        self.assertIsNone(R.label_ja("Peripheral nervous system"))
        for word in ("末梢神経系", "neuroblastoma", "神経芽腫", "glioma"):
            with self.subTest(word=word):
                self.assertIn(word, R.search_terms("Brain"))

    def test_values_without_extra_terms_return_none(self):
        self.assertIsNone(R.search_terms("Liver"))
        self.assertIsNone(R.search_terms(None))

    def test_search_terms_do_not_disturb_the_labels_or_the_order(self):
        self.assertEqual(R.label_ja("Brain"), "脳")
        self.assertLess(R.display_rank("Brain"), R.display_rank("Lung"))

    def test_the_api_serves_them(self):
        with tempfile.TemporaryDirectory() as d:
            from hpa_cellexp.demo import build_demo_database
            from hpa_cellexp.queries import Database

            path = os.path.join(d, "demo.sqlite")
            build_demo_database(path)
            organs = {item["value"]: item for item in Database(path).facets()["organs"]}
            self.assertIn("末梢神経系", organs["Brain"]["searchTerms"])
            self.assertIsNone(organs["Liver"]["searchTerms"])


class OrganCoverageTests(unittest.TestCase):
    """Filling in 由来臓器 when the metadata does not supply it."""

    EXPR = (
        "Gene name\tCell line\tnTPM\n"
        "GAPDH\tCACO-2\t100\nGAPDH\tA-549\t100\nGAPDH\tSK-MEL-30\t100\n"
        "GAPDH\tMYSTERY-XYZ\t100\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, metadata=None, overrides=None):
        d = self.tmp.name
        db_path = os.path.join(d, "cov.sqlite")
        build_database(
            db_path=db_path,
            expression_path=write(d, "e.tsv", self.EXPR),
            metadata_paths=[write(d, "m.tsv", metadata)] if metadata else [],
            progress=False,
            column_overrides=overrides,
        )
        db = Database(db_path)
        return db, {c["name"]: c for c in db.cell_lines()}

    def test_curated_list_fills_gaps_when_no_metadata(self):
        _, rows = self._build()
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertEqual(rows["A-549"]["organ"], "Lung")
        self.assertEqual(rows["SK-MEL-30"]["organ"], "Skin")
        self.assertIn("参考値", rows["CACO-2"]["organSource"])
        # Genuinely unknown stays unknown rather than being guessed at.
        self.assertIsNone(rows["MYSTERY-XYZ"]["organ"])

    def test_metadata_always_beats_the_curated_list(self):
        _, rows = self._build(
            "Cell line\tOrgan\nCACO-2\tRectum\nA-549\tLung\n"
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Rectum")
        self.assertEqual(rows["CACO-2"]["organSource"], "メタデータの organ 列")

    def test_unrecognised_headers_still_yield_organs(self):
        """The reported case: a metadata file whose columns are not detected."""
        db, rows = self._build(
            "CellLineName\tSiteOfOrigin\nCACO-2\t-\nA-549\t-\nSK-MEL-30\t-\nMYSTERY-XYZ\t-\n"
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Colon")
        self.assertEqual(rows["A-549"]["organ"], "Lung")

    def test_column_overrides(self):
        _, rows = self._build(
            "Line\tWhereFrom\nCACO-2\tRectum\nA-549\tLung\n",
            overrides={"cell-line": "Line", "organ": "WhereFrom"},
        )
        self.assertEqual(rows["CACO-2"]["organ"], "Rectum")

    def test_bad_override_names_the_problem(self):
        with self.assertRaises(C.MissingColumn) as ctx:
            self._build("Line\tWhereFrom\nCACO-2\tRectum\n",
                        overrides={"cell-line": "Line", "organ": "Nope"})
        self.assertIn("Nope", str(ctx.exception))

    def test_report_records_the_column_mapping_and_organ_sources(self):
        d = self.tmp.name
        report = build_database(
            db_path=os.path.join(d, "r.sqlite"),
            expression_path=write(d, "e2.tsv", self.EXPR),
            metadata_paths=[write(d, "m2.tsv", "CellLineName\tHistology\nCACO-2\tcolon cancer\n")],
            progress=False,
        )
        mapping = report.column_mapping["m2.tsv"]
        self.assertEqual(mapping["cell line"], "CellLineName")
        self.assertEqual(mapping["disease"], "Histology")
        self.assertIsNone(mapping["organ"])
        self.assertEqual(sum(report.organ_sources.values()), report.cell_lines)
        self.assertIn("（判定できず / 未設定）", report.organ_sources)

    def test_unassigned_organ_is_a_selectable_facet(self):
        from hpa_cellexp.queries import UNASSIGNED_ORGAN

        db, _ = self._build()
        facets = db.facets()
        self.assertEqual(facets["organUnassigned"], 1)
        entry = [f for f in facets["organs"] if f["value"] == UNASSIGNED_ORGAN]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["labelJa"], "（未設定）")

        picked = [r["name"] for r in db.cell_lines(organs=[UNASSIGNED_ORGAN])]
        self.assertEqual(picked, ["MYSTERY-XYZ"])
        combined = sorted(r["name"] for r in db.cell_lines(organs=[UNASSIGNED_ORGAN, "Lung"]))
        self.assertEqual(combined, ["A-549", "MYSTERY-XYZ"])

    def test_cell_lines_come_back_in_canonical_order(self):
        db, _ = self._build()
        names = [r["name"] for r in db.cell_lines()]
        # Lung -> Colon -> Skin anatomically, unassigned last.
        self.assertEqual(names, ["A-549", "CACO-2", "SK-MEL-30", "MYSTERY-XYZ"])


CELLOSAURUS = """ID   HeLa
AC   CVCL_0030
SY   Hela; HELA; He La
CC   Derived from site: In situ; Uterus, cervix; UBERON=UBERON_0000002.
DI   NCIt; C27677; Cervical adenocarcinoma
OX   NCBI_TaxID=9606; ! Homo sapiens
SX   Female
AG   30Y
CA   Cancer cell line
//
ID   A-549
AC   CVCL_0023
SY   A549
CC   Derived from site: In situ; Lung; UBERON=UBERON_0002048.
DI   NCIt; C3512; Lung adenocarcinoma
OX   NCBI_TaxID=9606; ! Homo sapiens
CA   Cancer cell line
//
ID   MDA-MB-231
AC   CVCL_0062
CC   Derived from site: Metastatic; Pleural effusion; UBERON=UBERON_0000175.
DI   NCIt; C4194; Breast adenocarcinoma
OX   NCBI_TaxID=9606; ! Homo sapiens
CA   Cancer cell line
//
ID   NIH/3T3
AC   CVCL_0594
CC   Derived from site: In situ; Embryo; UBERON=UBERON_0000922.
OX   NCBI_TaxID=10090; ! Mus musculus
CA   Spontaneously immortalized cell line
//
ID   MYSTERY-XYZ
AC   CVCL_9999
OX   NCBI_TaxID=9606; ! Homo sapiens
//
"""


class CellosaurusParserTests(unittest.TestCase):
    """Parsing the flat file from https://ftp.expasy.org/databases/cellosaurus/"""

    @classmethod
    def setUpClass(cls):
        from hpa_cellexp import cellosaurus as CS

        cls.CS = CS
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = write(cls.tmp.name, "cellosaurus.txt", CELLOSAURUS)
        cls.entries = {e.name: e for e in CS.parse(cls.path)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_entry_is_read(self):
        self.assertEqual(
            sorted(self.entries),
            ["A-549", "HeLa", "MDA-MB-231", "MYSTERY-XYZ", "NIH/3T3"],
        )

    def test_fields(self):
        hela = self.entries["HeLa"]
        self.assertEqual(hela.accession, "CVCL_0030")
        self.assertEqual(hela.species, "Homo sapiens")
        self.assertEqual(hela.sex, "Female")
        self.assertEqual(hela.age, "30Y")
        self.assertEqual(hela.disease, "Cervical adenocarcinoma")
        self.assertEqual(hela.site, "Uterus, cervix")
        self.assertEqual(hela.site_type, "In situ")
        self.assertIn("HELA", list(hela.keys()))

    def test_species_from_taxid_line(self):
        self.assertEqual(self.entries["NIH/3T3"].species, "Mus musculus")

    def test_in_situ_site_gives_the_organ(self):
        self.assertEqual(self.CS.organ_of(self.entries["A-549"]), "Lung")
        self.assertEqual(self.CS.organ_of(self.entries["HeLa"]), "Cervix")

    def test_metastatic_site_defers_to_the_disease(self):
        """A metastatic sample's site is where it was taken, not where the
        tumour arose - MDA-MB-231 is breast, not pleura."""
        entry = self.entries["MDA-MB-231"]
        self.assertEqual(entry.site, "Pleural effusion")
        self.assertEqual(self.CS.organ_of(entry), "Breast")

    def test_entry_without_site_or_disease_has_no_organ(self):
        self.assertIsNone(self.CS.organ_of(self.entries["MYSTERY-XYZ"]))

    def test_lookup_ignores_punctuation_and_case(self):
        index = self.CS.load_for(self.path, ["hela", "a 549", "mdamb231", "nope"])
        self.assertEqual(sorted(index), ["A549", "HELA", "MDAMB231"])
        self.assertEqual(index["HELA"].accession, "CVCL_0030")

    def test_lookup_of_nothing_reads_nothing(self):
        self.assertEqual(self.CS.load_for(self.path, []), {})


class CellosaurusIngestTests(unittest.TestCase):
    EXPR = (
        "Gene name\tCell line\tnTPM\n"
        "GAPDH\tHeLa\t100\nGAPDH\tA-549\t100\n"
        "GAPDH\tMDA-MB-231\t100\nGAPDH\tMYSTERY-XYZ\t100\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.cellosaurus = write(self.d, "cellosaurus.txt", CELLOSAURUS)

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, metadata=None):
        db_path = os.path.join(self.d, "cs-{}.sqlite".format(abs(hash(str(metadata))) % 10 ** 6))
        build_database(
            db_path=db_path,
            expression_path=write(self.d, "e.tsv", self.EXPR),
            metadata_paths=[write(self.d, "m.tsv", metadata)] if metadata else [],
            cellosaurus_path=self.cellosaurus,
            progress=False,
        )
        return {c["name"]: c for c in Database(db_path).cell_lines()}

    def test_fills_the_organ_from_sourced_data(self):
        rows = self._build()
        self.assertEqual(rows["HeLa"]["organ"], "Cervix")
        self.assertEqual(rows["A-549"]["organ"], "Lung")
        self.assertEqual(rows["MDA-MB-231"]["organ"], "Breast")
        for name in ("HeLa", "A-549", "MDA-MB-231"):
            with self.subTest(cell_line=name):
                self.assertIn("Cellosaurus", rows[name]["organSource"])
                # Sourced, so it must NOT be marked as an estimate.
                self.assertNotIn("参考値", rows[name]["organSource"])

    def test_accession_makes_the_outbound_link_exact(self):
        rows = self._build()
        self.assertEqual(rows["HeLa"]["cellosaurusId"], "CVCL_0030")
        self.assertEqual(rows["HeLa"]["databaseUrl"], "https://www.cellosaurus.org/CVCL_0030")
        # Even a cell line with no organ gets a direct link.
        self.assertEqual(
            rows["MYSTERY-XYZ"]["databaseUrl"], "https://www.cellosaurus.org/CVCL_9999"
        )

    def test_species_sex_age_and_disease_are_filled(self):
        rows = self._build()
        self.assertEqual(rows["HeLa"]["species"], "Homo sapiens")
        self.assertEqual(rows["HeLa"]["sex"], "Female")
        self.assertEqual(rows["HeLa"]["age"], "30Y")
        self.assertEqual(rows["HeLa"]["disease"], "Cervical adenocarcinoma")

    def test_dataset_metadata_still_wins(self):
        """Cellosaurus fills gaps; it does not overrule the user's own file."""
        rows = self._build("Cell line\tOrgan\nHeLa\tRectum\n")
        self.assertEqual(rows["HeLa"]["organ"], "Rectum")
        self.assertEqual(rows["HeLa"]["organSource"], "メタデータの organ 列")
        # ... but the accession is still picked up.
        self.assertEqual(rows["HeLa"]["cellosaurusId"], "CVCL_0030")

    def test_unknown_cell_line_stays_unassigned(self):
        self.assertIsNone(self._build()["MYSTERY-XYZ"]["organ"])

    def test_file_is_recorded_in_the_sources(self):
        db_path = os.path.join(self.d, "src.sqlite")
        build_database(
            db_path=db_path,
            expression_path=write(self.d, "e2.tsv", self.EXPR),
            cellosaurus_path=self.cellosaurus,
            progress=False,
        )
        sources = Database(db_path).info()["sources"]
        self.assertEqual(sources["cellosaurus"]["name"], "cellosaurus.txt")


class SourceProvenanceTests(unittest.TestCase):
    """The site has to be able to say which files it was built from."""

    def test_meta_records_every_input_file(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "s.sqlite")
            expression = write_zip(d, "rna_celline.tsv.zip", "rna_celline.tsv", EXPRESSION)
            metadata = write(d, "cell_line_analysis_data.tsv", METADATA)
            tcga = write(d, "rna_cell_line_tcga_comparison.tsv", TCGA)
            build_database(
                db_path=db_path,
                expression_path=expression,
                metadata_paths=[metadata],
                tcga_path=tcga,
                release="HPA v24",
                progress=False,
            )
            info = Database(db_path).info()
            sources = info["sources"]

            self.assertEqual(sources["expression"]["name"], "rna_celline.tsv.zip")
            self.assertEqual(sources["expression"]["path"], os.path.abspath(expression))
            self.assertGreater(sources["expression"]["bytes"], 0)
            self.assertIn("modified", sources["expression"])

            self.assertEqual(
                [f["name"] for f in sources["metadata"]], ["cell_line_analysis_data.tsv"]
            )
            self.assertEqual(sources["tcga"]["name"], "rna_cell_line_tcga_comparison.tsv")
            self.assertEqual(info["databasePath"], os.path.abspath(db_path))
            self.assertEqual(info["release"], "HPA v24")

    def test_optional_inputs_are_reported_as_absent(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "s2.sqlite")
            build_database(
                db_path=db_path,
                expression_path=write(d, "e.tsv", EXPRESSION),
                progress=False,
            )
            sources = Database(db_path).info()["sources"]
            self.assertEqual(sources["metadata"], [])
            self.assertIsNone(sources["tcga"])

    def test_sources_are_served_by_the_api(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "s3.sqlite")
            build_database(
                db_path=db_path,
                expression_path=write_zip(d, "rna_celline.tsv.zip", "rna_celline.tsv", EXPRESSION),
                metadata_paths=[write(d, "meta.tsv", METADATA)],
                progress=False,
            )
            body = TestClient(create_app(db_path)).get("/api/meta").json()
            self.assertEqual(body["sources"]["expression"]["name"], "rna_celline.tsv.zip")
            self.assertEqual([f["name"] for f in body["sources"]["metadata"]], ["meta.tsv"])

    def test_demo_sources_say_they_are_synthetic(self):
        from hpa_cellexp.demo import build_demo_database

        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "demo.sqlite")
            build_demo_database(db_path)
            info = Database(db_path).info()
            self.assertTrue(info["isDemo"])
            self.assertIn("合成", info["sources"]["expression"]["name"])


class SchemaVersionTests(unittest.TestCase):
    """A database from an older build must fail loudly, not halfway.

    Before this, a stale database served /api/meta happily (the dataset banner
    appeared) and then returned 500 on every cell line query - which looks like
    "the search is broken", not "the database needs rebuilding".
    """

    def _stale_db(self, directory):
        from hpa_cellexp.config import SCHEMA_VERSION

        db_path = os.path.join(directory, "stale.sqlite")
        build_database(
            db_path=db_path,
            expression_path=write(directory, "e.tsv", EXPRESSION),
            progress=False,
        )
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_fresh_database_is_stamped(self):
        from hpa_cellexp.config import SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "v.sqlite")
            build_database(
                db_path=db_path,
                expression_path=write(d, "e.tsv", EXPRESSION),
                progress=False,
            )
            import sqlite3

            conn = sqlite3.connect(db_path)
            value = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(int(value), SCHEMA_VERSION)
            Database(db_path).info()  # must not raise

    def test_demo_database_is_stamped(self):
        from hpa_cellexp.config import SCHEMA_VERSION
        from hpa_cellexp.demo import build_demo_database

        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "demo.sqlite")
            build_demo_database(db_path)
            db = Database(db_path)
            db.info()  # must not raise
            import sqlite3

            conn = sqlite3.connect(db_path)
            value = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(int(value), SCHEMA_VERSION)

    def test_stale_database_raises_with_rebuild_instructions(self):
        from hpa_cellexp.queries import SchemaOutdated

        with tempfile.TemporaryDirectory() as d:
            db = Database(self._stale_db(d))
            with self.assertRaises(SchemaOutdated) as ctx:
                db.info()
            message = str(ctx.exception)
            self.assertIn("再構築", message)
            self.assertIn("python -m hpa_cellexp build", message)

    def test_stale_database_fails_the_metadata_endpoint_too(self):
        """Not just the cell line queries - the page must say so immediately."""
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as d:
            client = TestClient(create_app(self._stale_db(d)))
            for path in ("/api/meta", "/api/cell-lines", "/api/genes?q=AL"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("再構築", response.json()["detail"])

    def test_serve_refuses_a_stale_database(self):
        from hpa_cellexp.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["--database", self._stale_db(d), "serve"]), 1)


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

    def test_database_may_be_given_after_the_subcommand(self):
        """`demo --database X` used to die with "unrecognized arguments",
        which reads as if the option did not exist."""
        from hpa_cellexp.__main__ import build_parser

        parser = build_parser()
        self.assertEqual(parser.parse_args(["demo", "--database", "/tmp/x"]).database, "/tmp/x")
        self.assertEqual(parser.parse_args(["--database", "/tmp/x", "demo"]).database, "/tmp/x")
        # No value anywhere still leaves the default in place.
        from hpa_cellexp.config import DEFAULT_DB_PATH

        self.assertEqual(parser.parse_args(["demo"]).database, DEFAULT_DB_PATH)

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
