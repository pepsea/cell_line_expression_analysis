"""Header detection for Human Protein Atlas TSV files.

HPA renames and reorders columns between releases (``TPM`` only in the early
releases, ``pTPM``/``nTPM`` added later; ``Cell line`` vs ``Cell_line``; the
cell line metadata file has gone by several names).  Rather than hard-coding a
single layout we resolve every logical field against a list of accepted header
spellings, so a new release usually ingests without a code change.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    "normalise_header",
    "resolve",
    "resolve_all",
    "GENE_ID",
    "GENE_SYMBOL",
    "CELL_LINE",
    "TPM",
    "PTPM",
    "NTPM",
    "ORGAN",
    "TISSUE",
    "DISEASE",
    "SPECIES",
    "CELLOSAURUS",
    "SEX",
    "AGE",
    "TCGA_CANCER",
    "RANK",
    "NES",
    "SPEARMAN",
    "MissingColumn",
]


class MissingColumn(LookupError):
    """Raised when a required logical column cannot be found in a header row."""


def normalise_header(name: str) -> str:
    """Fold a raw header cell to a comparable key.

    Lowercases, drops a UTF-8 BOM and surrounding quotes, and collapses every
    run of non-alphanumeric characters to a single underscore, so that
    ``"Cell line"``, ``"Cell_line"`` and ``"CELL-LINE"`` all become
    ``cell_line``.
    """
    cleaned = name.strip().lstrip("﻿").strip('"').strip()
    out: List[str] = []
    prev_sep = False
    for ch in cleaned.lower():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        elif not prev_sep:
            out.append("_")
            prev_sep = True
    return "".join(out).strip("_")


# --- logical field definitions -------------------------------------------
# Each entry is an ordered list of accepted (normalised) header spellings;
# earlier entries win when a file happens to contain more than one match.

GENE_ID = ("gene", ("gene", "ensembl", "ensembl_id", "gene_id", "ensg", "gene_stable_id"))
GENE_SYMBOL = (
    "gene_name",
    ("gene_name", "gene_symbol", "symbol", "genename", "hgnc_symbol", "gene_description"),
)
CELL_LINE = ("cell_line", ("cell_line", "cellline", "cell", "cell_line_name", "name"))

TPM = ("tpm", ("tpm",))
PTPM = ("ptpm", ("ptpm", "p_tpm", "protein_coding_tpm"))
NTPM = ("ntpm", ("ntpm", "n_tpm", "normalized_tpm", "normalised_tpm", "consensus_ntpm"))

ORGAN = (
    "organ",
    ("organ", "organ_of_origin", "primary_organ", "tissue_of_origin", "origin_organ", "organ_system"),
)
TISSUE = (
    "tissue",
    ("tissue", "primary_tissue", "tissue_origin", "origin", "primary_site", "site", "cell_line_origin"),
)
DISEASE = (
    "disease",
    ("disease", "cancer", "cancer_type", "disease_name", "diagnosis", "cellosaurus_disease", "cell_line_disease"),
)
SPECIES = ("species", ("species", "organism", "taxon", "cellosaurus_species"))
CELLOSAURUS = (
    "cellosaurus_id",
    ("cellosaurus_id", "cellosaurus", "cvcl", "cvcl_id", "rrid", "cell_line_id", "cellosaurus_accession"),
)
SEX = ("sex", ("sex", "gender"))
AGE = ("age", ("age", "age_at_sampling", "patient_age"))

TCGA_CANCER = ("tcga_cancer", ("tcga_cancer", "tcga", "cancer", "tcga_cancer_type"))
RANK = ("rank", ("rank",))
NES = ("nes", ("normalized_enrichment_score", "normalised_enrichment_score", "nes", "enrichment_score"))
SPEARMAN = ("spearman", ("spearman_correlation", "spearman", "correlation", "rho"))

Field = Sequence  # (label, aliases) - kept loose to avoid a runtime dependency


def _index_header(header: Iterable[str]) -> Dict[str, int]:
    index: Dict[str, int] = {}
    for position, raw in enumerate(header):
        key = normalise_header(raw)
        # First occurrence wins so a duplicated header does not shadow the
        # column we already bound.
        index.setdefault(key, position)
    return index


def resolve(header: Sequence[str], field: Field, *, required: bool = False) -> Optional[int]:
    """Return the column index for ``field`` in ``header``, or ``None``.

    ``field`` is one of the module-level tuples such as :data:`GENE_SYMBOL`.
    Set ``required=True`` to raise :class:`MissingColumn` instead of returning
    ``None``.
    """
    label, aliases = field[0], field[1]
    index = _index_header(header)
    for alias in aliases:
        if alias in index:
            return index[alias]
    if required:
        raise MissingColumn(
            "required column {!r} not found; accepted spellings are {} "
            "but the file header is {}".format(label, ", ".join(aliases), list(header))
        )
    return None


def resolve_all(header: Sequence[str], fields: Iterable[Field]) -> Dict[str, Optional[int]]:
    """Resolve several fields at once, keyed by each field's label."""
    return {field[0]: resolve(header, field) for field in fields}
