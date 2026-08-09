"""Reference tables used to enrich cell line metadata.

Two lookups live here:

``tcga_organ``
    TCGA study code -> organ, used when the only origin information available
    for a cell line is HPA's ``rna_cell_line_tcga_comparison.tsv``.

``organ_from_text``
    Keyword matching over a free-text disease / tissue string, used when the
    metadata file has a disease column but no explicit organ column.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

_REFERENCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")

__all__ = [
    "tcga_organ_map",
    "organ_from_text",
    "normalise_organ",
    "normalise_species",
    "cellosaurus_url",
    "extract_cvcl",
    "DEFAULT_SPECIES",
]

# Every cell line in the HPA cell line resource is human; this is only the
# fallback used when the input files carry no species column at all.
DEFAULT_SPECIES = "Homo sapiens"

_SPECIES_ALIASES = {
    "human": "Homo sapiens",
    "homo sapiens": "Homo sapiens",
    "h. sapiens": "Homo sapiens",
    "hsapiens": "Homo sapiens",
    "9606": "Homo sapiens",
    "mouse": "Mus musculus",
    "mus musculus": "Mus musculus",
    "m. musculus": "Mus musculus",
    "10090": "Mus musculus",
    "rat": "Rattus norvegicus",
    "rattus norvegicus": "Rattus norvegicus",
    "10116": "Rattus norvegicus",
    "dog": "Canis lupus familiaris",
    "monkey": "Chlorocebus sabaeus",
    "hamster": "Cricetulus griseus",
}

_CVCL_RE = re.compile(r"CVCL[_:]?([0-9A-Z]{4})", re.IGNORECASE)


def _read_tsv(filename: str) -> List[List[str]]:
    path = os.path.join(_REFERENCE_DIR, filename)
    rows: List[List[str]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            rows.append(line.split("\t"))
    return rows


@lru_cache(maxsize=1)
def tcga_organ_map() -> Dict[str, str]:
    """``{"LUAD": "Lung", ...}`` - keys are upper-case TCGA study codes."""
    rows = _read_tsv("tcga_organ.tsv")
    mapping: Dict[str, str] = {}
    for row in rows[1:]:  # skip header
        if len(row) >= 2 and row[0] and row[1]:
            mapping[row[0].strip().upper()] = row[1].strip()
    return mapping


@lru_cache(maxsize=1)
def _organ_keywords() -> Tuple[Tuple[str, str], ...]:
    """Ordered ``(lowercase pattern, organ)`` pairs; first hit wins."""
    rows = _read_tsv("organ_keywords.tsv")
    pairs: List[Tuple[str, str]] = []
    for row in rows[1:]:  # skip header
        if len(row) >= 2 and row[0] and row[1]:
            pairs.append((row[0].strip().lower(), row[1].strip()))
    return tuple(pairs)


def organ_from_text(*texts: Optional[str]) -> Optional[str]:
    """Infer an organ from one or more free-text origin/disease strings.

    Arguments are tried in order, so pass the most specific field first.
    """
    for text in texts:
        if not text:
            continue
        haystack = text.lower()
        for pattern, organ in _organ_keywords():
            if pattern in haystack:
                return organ
    return None


def normalise_organ(value: Optional[str]) -> Optional[str]:
    """Tidy an organ label supplied by the source file (casing, whitespace)."""
    if not value:
        return None
    cleaned = " ".join(value.replace("_", " ").split()).strip(" ,;")
    if not cleaned or cleaned.lower() in {"na", "n/a", "none", "unknown", "-"}:
        return None
    # Preserve embedded capitals (e.g. "B-cell") but capitalise a leading
    # lower-case word so "lung" and "Lung" collapse into one facet value.
    if cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def normalise_species(value: Optional[str]) -> str:
    if not value:
        return DEFAULT_SPECIES
    cleaned = " ".join(value.split()).strip()
    return _SPECIES_ALIASES.get(cleaned.lower(), cleaned or DEFAULT_SPECIES)


def extract_cvcl(*values: Optional[str]) -> Optional[str]:
    """Pull a ``CVCL_xxxx`` accession out of any of ``values``."""
    for value in values:
        if not value:
            continue
        match = _CVCL_RE.search(value)
        if match:
            return "CVCL_" + match.group(1).upper()
    return None


def cellosaurus_url(cell_line_name: str, cvcl: Optional[str] = None) -> str:
    """Deep link into Cellosaurus, the reference cell line database.

    Uses the stable accession URL when we know the CVCL id, otherwise falls
    back to a name search so the link is always useful.
    """
    if cvcl:
        return "https://www.cellosaurus.org/{}".format(cvcl)
    from urllib.parse import quote

    return "https://www.cellosaurus.org/search?input={}".format(quote(cell_line_name))
