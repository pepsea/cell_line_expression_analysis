"""Build a small, clearly-labelled DEMO database.

The cell line names, organs and Cellosaurus accessions here are real, so the
filtering and the outbound database links behave exactly as they will with the
real data.  **The expression values are synthetic** - deterministic
pseudo-random numbers shaped by a few marker rules so the heatmap looks like
something.  They are not measurements and must never be cited.  Every demo
database carries ``meta.is_demo = 1`` and the UI shows a permanent banner.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Dict, List, Optional, Tuple

from .ingest import IngestReport, connect_for_write
from .reference import name_key

__all__ = ["build_demo_database"]

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# (name, organ, disease, CVCL or None)
# CVCL accessions are only filled in where they are well known; where left
# blank the UI links to a Cellosaurus name search instead, which is always
# correct.
_CELL_LINES: List[Tuple[str, str, str, Optional[str]]] = [
    ("A-431", "Skin", "Epidermoid carcinoma", "CVCL_0037"),
    ("A-549", "Lung", "Lung carcinoma", "CVCL_0023"),
    ("AN3-CA", "Endometrium", "Endometrial adenocarcinoma", None),
    ("ASC diff", "Connective tissue", "Normal", None),
    ("BEWO", "Placenta", "Choriocarcinoma", None),
    ("BJ", "Connective tissue", "Normal fibroblast", None),
    ("CACO-2", "Colon", "Colorectal adenocarcinoma", None),
    ("CAPAN-2", "Pancreas", "Pancreatic adenocarcinoma", None),
    ("DAUDI", "Lymphoid tissue", "Burkitt lymphoma", "CVCL_0008"),
    ("EFO-21", "Ovary", "Ovarian cystadenocarcinoma", None),
    ("FADU", "Head and neck", "Pharyngeal squamous cell carcinoma", None),
    ("HAP1", "Bone marrow", "Chronic myeloid leukemia derived", None),
    ("HDLM-2", "Lymphoid tissue", "Hodgkin lymphoma", None),
    ("HEK 293", "Kidney", "Normal, embryonic", "CVCL_0045"),
    ("HEL", "Bone marrow", "Erythroleukemia", None),
    ("HELA", "Cervix", "Cervical adenocarcinoma", "CVCL_0030"),
    ("HEP G2", "Liver", "Hepatocellular carcinoma", "CVCL_0027"),
    ("HHSTEC", "Vasculature", "Normal endothelial", None),
    ("HL-60", "Bone marrow", "Acute promyelocytic leukemia", "CVCL_0002"),
    ("HT-1080", "Soft tissue", "Fibrosarcoma", None),
    ("HT-29", "Colon", "Colorectal adenocarcinoma", None),
    ("HCT 116", "Colon", "Colorectal carcinoma", "CVCL_0291"),
    ("JURKAT", "Lymphoid tissue", "T-cell leukemia", "CVCL_0065"),
    ("K-562", "Bone marrow", "Chronic myeloid leukemia", "CVCL_0004"),
    ("KARPAS-707", "Bone marrow", "Multiple myeloma", None),
    ("LNCAP", "Prostate", "Prostate carcinoma", "CVCL_0395"),
    ("MCF7", "Breast", "Breast adenocarcinoma", "CVCL_0031"),
    ("MDA-MB-231", "Breast", "Breast adenocarcinoma", "CVCL_0062"),
    ("NB-4", "Bone marrow", "Acute promyelocytic leukemia", None),
    ("NTERA-2", "Testis", "Embryonal carcinoma", None),
    ("OE19", "Esophagus", "Esophageal adenocarcinoma", None),
    ("PC-3", "Prostate", "Prostate adenocarcinoma", "CVCL_0035"),
    ("PANC-1", "Pancreas", "Pancreatic carcinoma", "CVCL_0480"),
    ("RH-30", "Soft tissue", "Rhabdomyosarcoma", None),
    ("RPMI-8226", "Bone marrow", "Multiple myeloma", None),
    ("RT4", "Urinary bladder", "Urothelial carcinoma", None),
    ("SH-SY5Y", "Peripheral nervous system", "Neuroblastoma", "CVCL_0019"),
    ("SK-BR-3", "Breast", "Breast adenocarcinoma", None),
    ("SK-MEL-30", "Skin", "Melanoma", None),
    ("SW480", "Colon", "Colorectal adenocarcinoma", None),
    ("T-47D", "Breast", "Breast ductal carcinoma", None),
    ("THP-1", "Bone marrow", "Acute monocytic leukemia", "CVCL_0006"),
    ("TIME", "Vasculature", "Normal endothelial", None),
    ("U-138 MG", "Brain", "Glioblastoma", None),
    ("U-2 OS", "Bone", "Osteosarcoma", "CVCL_0042"),
    ("U-251 MG", "Brain", "Glioblastoma", None),
    ("U-266/70", "Bone marrow", "Multiple myeloma", None),
    ("U-698", "Lymphoid tissue", "B-cell lymphoma", None),
    ("WM-115", "Skin", "Melanoma", None),
    ("SK-N-SH", "Peripheral nervous system", "Neuroblastoma", None),
]

# gene -> (baseline nTPM, {organ: multiplier}) - a caricature of real biology,
# enough to make the heatmap and the sorting controls demonstrably work.
_GENES: Dict[str, Tuple[float, Dict[str, float]]] = {
    "GAPDH": (2200.0, {}),
    "ACTB": (3100.0, {}),
    "MKI67": (75.0, {}),
    "TP53": (48.0, {}),
    "MYC": (95.0, {}),
    "EGFR": (18.0, {"Skin": 12.0, "Lung": 6.0, "Brain": 8.0}),
    "ERBB2": (22.0, {"Breast": 18.0, "Stomach": 5.0}),
    "ESR1": (1.2, {"Breast": 60.0}),
    "AR": (1.0, {"Prostate": 45.0}),
    "KRT19": (30.0, {"Bone marrow": 0.01, "Lymphoid tissue": 0.01, "Brain": 0.05}),
    "EPCAM": (25.0, {"Bone marrow": 0.01, "Lymphoid tissue": 0.01, "Connective tissue": 0.02}),
    "VIM": (180.0, {"Colon": 0.15, "Breast": 0.2}),
    "PTPRC": (0.4, {"Bone marrow": 220.0, "Lymphoid tissue": 260.0}),
    "CD19": (0.1, {"Lymphoid tissue": 55.0}),
    "CD3E": (0.1, {"Lymphoid tissue": 40.0}),
    "ALB": (0.05, {"Liver": 900.0}),
    "AFP": (0.05, {"Liver": 420.0, "Testis": 30.0}),
    "APOA1": (0.1, {"Liver": 350.0}),
    "SFTPC": (0.02, {"Lung": 60.0}),
    "NKX2-1": (0.3, {"Lung": 40.0, "Thyroid gland": 55.0}),
    "GFAP": (0.1, {"Brain": 65.0}),
    "SOX2": (2.0, {"Brain": 35.0, "Testis": 40.0}),
    "MITF": (1.0, {"Skin": 70.0}),
    "PMEL": (0.2, {"Skin": 120.0}),
    "TYR": (0.05, {"Skin": 45.0}),
    "KLK3": (0.05, {"Prostate": 300.0}),
    "CDH1": (40.0, {"Bone marrow": 0.05, "Lymphoid tissue": 0.05, "Soft tissue": 0.1}),
    "CDH2": (25.0, {"Colon": 0.2, "Breast": 0.3}),
    "MUC1": (28.0, {"Breast": 6.0, "Pancreas": 5.0}),
    "CEACAM5": (0.5, {"Colon": 120.0, "Rectum": 100.0}),
    "INS": (0.01, {"Pancreas": 8.0}),
    "PAX8": (1.0, {"Ovary": 40.0, "Kidney": 25.0, "Thyroid gland": 45.0}),
    "WT1": (1.5, {"Kidney": 30.0, "Bone marrow": 12.0}),
    "MYOD1": (0.05, {"Soft tissue": 90.0}),
    "DES": (0.4, {"Soft tissue": 60.0}),
    "COL1A1": (60.0, {"Connective tissue": 25.0, "Bone": 18.0}),
    "RUNX2": (3.0, {"Bone": 30.0}),
    "PECAM1": (0.3, {"Vasculature": 180.0}),
    "VWF": (0.2, {"Vasculature": 240.0}),
    "CGA": (0.05, {"Placenta": 300.0}),
    "HBB": (0.2, {"Bone marrow": 90.0}),
    "SPI1": (0.5, {"Bone marrow": 85.0, "Lymphoid tissue": 30.0}),
    "POU5F1": (0.3, {"Testis": 160.0}),
    "NANOG": (0.1, {"Testis": 90.0}),
}


def _noise(seed: str) -> float:
    """Deterministic pseudo-random multiplier in roughly [0.35, 2.9]."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    # Log-normal-ish spread so a handful of cell lines stand out.
    return math.exp((unit - 0.5) * 2.1)


def _value(gene: str, cell_line: str, organ: str) -> float:
    baseline, boosts = _GENES[gene]
    level = baseline * boosts.get(organ, 1.0) * _noise(gene + "|" + cell_line)
    if _noise("dropout|" + gene + "|" + cell_line) < 0.42 and level < 1.0:
        return 0.0
    return round(level, 1)


def build_demo_database(db_path: str) -> IngestReport:
    report = IngestReport()

    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        stale = db_path + suffix
        if os.path.exists(stale):
            os.remove(stale)

    conn = connect_for_write(db_path)
    try:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as handle:
            conn.executescript(handle.read())
        cursor = conn.cursor()

        cursor.executemany(
            "INSERT INTO genes (id, ensembl_id, symbol, symbol_uc) VALUES (?,?,?,?)",
            [(i + 1, None, symbol, symbol.upper()) for i, symbol in enumerate(sorted(_GENES))],
        )
        gene_ids = {symbol: i + 1 for i, symbol in enumerate(sorted(_GENES))}

        cursor.executemany(
            "INSERT INTO cell_lines "
            "(id, name, name_uc, name_key, organ, tissue, disease, species, "
            " cellosaurus_id, sex, age, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (i + 1, name, name.upper(), name_key(name), organ, None, disease,
                 "Homo sapiens", cvcl, None, None, "demo")
                for i, (name, organ, disease, cvcl) in enumerate(_CELL_LINES)
            ],
        )

        rows = []
        for cell_id, (name, organ, _disease, _cvcl) in enumerate(_CELL_LINES, start=1):
            for symbol, gene_id in gene_ids.items():
                value = _value(symbol, name, organ)
                rows.append((gene_id, cell_id, value, value, value))
        cursor.executemany("INSERT INTO expression VALUES (?,?,?,?,?)", rows)

        cursor.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            sorted(
                {
                    "release": "DEMO (synthetic values)",
                    "is_demo": "1",
                    "built_at": "",
                    "expression_source": "hpa_cellexp.demo",
                    "metrics": "tpm,ptpm,ntpm",
                    "gene_count": str(len(gene_ids)),
                    "cell_line_count": str(len(_CELL_LINES)),
                    "expression_rows": str(len(rows)),
                }.items()
            ),
        )
        conn.commit()

        report.genes = len(gene_ids)
        report.cell_lines = len(_CELL_LINES)
        report.expression_rows = len(rows)
        report.metrics = ["tpm", "ptpm", "ntpm"]
        report.warnings.append(
            "DEMO database: expression values are synthetic and must not be used for analysis."
        )
    finally:
        conn.close()
    return report
