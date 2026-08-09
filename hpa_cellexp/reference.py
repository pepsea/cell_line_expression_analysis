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
    "name_key",
    "BROAD_ORGANS",
    "is_broad_organ",
    "labels_ja",
    "label_ja",
    "search_terms",
    "display_rank",
    "natural_key",
    "tcga_organ_map",
    "organ_from_text",
    "organ_from_cell_line_name",
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
    # Japanese spellings collapse too, so a source that writes ヒト does not
    # create a second species facet alongside "Homo sapiens".
    "ヒト": "Homo sapiens",
    "ヒト由来": "Homo sapiens",
    "人": "Homo sapiens",
    "ホモサピエンス": "Homo sapiens",
    "マウス": "Mus musculus",
    "ラット": "Rattus norvegicus",
    "イヌ": "Canis lupus familiaris",
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
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def name_key(name: Optional[str]) -> str:
    """Collapse a cell line name to letters and digits only, upper-cased.

    HPA punctuates cell line names inconsistently ("HEK 293", "U-2 OS",
    "MDA-MB-231"), and nobody types them back the same way.  Matching on this
    key as well as on the literal name makes "hek293" find "HEK 293".
    """
    if not name:
        return ""
    return _NON_ALNUM_RE.sub("", name.upper())


_DIGITS_RE = re.compile(r"(\d+)")


def natural_key(name: Optional[str]):
    """Sort key that orders embedded numbers numerically.

    Plain lexicographic order puts NCI-H1650 before NCI-H2, which is not what
    anyone reading a list of cell lines expects - and HPA ships dozens of
    numbered series (NCI-H*, SK-MEL-*, MDA-MB-*).
    """
    if not name:
        return ()
    parts = _DIGITS_RE.split(name.upper())
    return tuple(
        (1, int(part), "") if index % 2 else (0, 0, part)
        for index, part in enumerate(parts)
        if part != ""
    )


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
def labels_ja() -> Dict[str, str]:
    """``{"Lung": "肺", ...}`` for organ and species facet values."""
    rows = _read_tsv("labels_ja.tsv")
    mapping: Dict[str, str] = {}
    for row in rows[1:]:  # skip header
        if len(row) >= 2 and row[0] and row[1]:
            mapping[row[0].strip()] = row[1].strip()
    return mapping


@lru_cache(maxsize=1)
def _search_terms() -> Dict[str, str]:
    """Optional third column of labels_ja.tsv: extra words that should find
    this value in the facet list.

    Lets a search for 脳 also surface 末梢神経系 - a neuroblastoma line is
    filed under the peripheral nervous system, which is correct but not where
    someone looking for "brain" thinks to look.  Search only; it never changes
    how a cell line is classified.
    """
    rows = _read_tsv("labels_ja.tsv")
    return {
        row[0].strip(): row[2].strip()
        for row in rows[1:]
        if len(row) >= 3 and row[0] and row[2].strip()
    }


def search_terms(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _search_terms().get(value.strip())


def label_ja(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return labels_ja().get(value.strip())


@lru_cache(maxsize=1)
def _display_order() -> Dict[str, int]:
    """``{"Bone marrow": 0, "Blood": 1, ...}`` - row order in labels_ja.tsv."""
    rows = _read_tsv("labels_ja.tsv")
    return {row[0].strip(): i for i, row in enumerate(rows[1:]) if len(row) >= 2 and row[0]}


def display_rank(value: Optional[str]) -> int:
    """Position in the canonical (anatomical) display order.

    Unlisted values sort after every listed one; callers break the remaining
    ties on the value itself.
    """
    if not value:
        return 10 ** 6
    return _display_order().get(value.strip(), 10 ** 5)


@lru_cache(maxsize=1)
def _cell_line_organs() -> Tuple[Dict[str, str], Tuple[Tuple[str, str], ...]]:
    """``({name_key: organ}, ((prefix, organ), ...))`` from cell_line_organ.tsv."""
    exact: Dict[str, str] = {}
    prefixes: List[Tuple[str, str]] = []
    for row in _read_tsv("cell_line_organ.tsv")[1:]:  # skip header
        if len(row) < 3 or not row[1] or not row[2]:
            continue
        kind, name, organ = row[0].strip().lower(), row[1].strip(), row[2].strip()
        if kind == "exact":
            exact[name_key(name)] = organ
        elif kind == "prefix":
            prefixes.append((name_key(name), organ))
    # Longest prefix first, so a more specific series wins over a shorter one.
    prefixes.sort(key=lambda pair: -len(pair[0]))
    return exact, tuple(prefixes)


def organ_from_cell_line_name(name: Optional[str]) -> Optional[Tuple[str, str]]:
    """Look a cell line up in the built-in table.

    Returns ``(organ, provenance label)`` or ``None``.  Only a fallback: the
    caller must try the dataset's own metadata first, and the label marks the
    result as an estimate so it never passes for sourced data.
    """
    key = name_key(name)
    if not key:
        return None
    exact, prefixes = _cell_line_organs()
    organ = exact.get(key)
    if organ:
        return organ, "内蔵の細胞株リストから推定 ※参考値"
    for prefix, organ in prefixes:
        if key.startswith(prefix):
            return organ, "内蔵の細胞株リスト（{}…）から推定 ※参考値".format(prefix)
    return None


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


# Organ values that name a whole tract or system rather than an organ.  When
# the source file gives one of these AND the disease/tissue text points at
# something more specific, the specific one wins - otherwise every colorectal
# line lands under "Intestine" and searching for 結腸 (Colon) finds nothing.
BROAD_ORGANS = frozenset(
    {
        "intestine",
        "intestinal tract",
        "gastrointestinal",
        "gastrointestinal tract",
        "gi tract",
        "digestive system",
        "digestive tract",
        "endocrine gland",
        "endocrine system",
        "female reproductive system",
        "male reproductive system",
        "reproductive system",
        "urinary tract",
        "urogenital",
        "respiratory system",
        "respiratory tract",
        # "nervous system" is deliberately NOT here.  Refining it by the
        # disease text turned every neuroblastoma line into a second organ, so
        # a source that said "Nervous system" stopped appearing under 脳.
        "lymphatic system",
        "haematopoietic system",
        "hematopoietic system",
        "haematopoietic and lymphoid tissue",
        "hematopoietic and lymphoid tissue",
        "soft tissues",
        "other",
        "unknown",
    }
)


def is_broad_organ(value: Optional[str]) -> bool:
    return bool(value) and value.strip().lower() in BROAD_ORGANS


# Organ values that name the same facet under different words.  The nervous
# system entries are the important ones: a dataset writing "Nervous system" and
# one writing "Brain" must land in the same bucket, or half the neural cell
# lines are missing from 脳 depending on which file they came from.
_ORGAN_ALIASES = {
    "nervous system": "Brain",
    "central nervous system": "Brain",
    "cns": "Brain",
    "peripheral nervous system": "Brain",
    "pns": "Brain",
    "autonomic nervous system": "Brain",
    "sympathetic nervous system": "Brain",
    "neural": "Brain",
    "cerebrum": "Brain",
    "cerebellum": "Brain",
    "large intestine": "Colon",
    "colorectum": "Colon",
    "mammary gland": "Breast",
    "uterine cervix": "Cervix",
    "oesophagus": "Esophagus",
    "urinary bladder": "Urinary bladder",
    "bladder": "Urinary bladder",
    "haematopoietic": "Bone marrow",
}


def normalise_organ(value: Optional[str]) -> Optional[str]:
    """Tidy an organ label supplied by the source file (casing, whitespace)."""
    if not value:
        return None
    cleaned = " ".join(value.replace("_", " ").split()).strip(" ,;")
    if not cleaned or cleaned.lower() in {"na", "n/a", "none", "unknown", "-"}:
        return None
    alias = _ORGAN_ALIASES.get(cleaned.lower())
    if alias:
        return alias
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
