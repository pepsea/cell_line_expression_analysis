"""Reader for the Cellosaurus flat file (``cellosaurus.txt``).

Cellosaurus (SIB) is the reference catalogue of cell lines, and it is the right
answer to "the dataset does not say where this cell line came from".  It covers
far more lines than any expression release and, unlike the built-in guess list
in ``reference/cell_line_organ.tsv``, it is sourced data.

    https://ftp.expasy.org/databases/cellosaurus/cellosaurus.txt

The flat file is a sequence of entries terminated by ``//``, each a set of
two-letter line codes::

    ID   HeLa
    AC   CVCL_0030
    SY   Hela; HELA; He La
    CC   Derived from site: In situ; Uterus, cervix; UBERON=UBERON_0000002.
    DI   NCIt; C27677; Cervical adenocarcinoma
    OX   NCBI_TaxID=9606; ! Homo sapiens
    SX   Female
    AG   30Y
    CA   Cancer cell line
    //

Only the fields this app needs are kept, and entries are streamed and filtered
against the cell lines actually present in the expression matrix, so the ~150k
entry file costs a single pass and almost no memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Set

from .reference import name_key, normalise_species, organ_from_text
from .sources import open_table

__all__ = ["CellosaurusEntry", "parse", "load_for", "organ_of"]

# "In situ; Colon; UBERON=UBERON_0001155." -> ("In situ", "Colon")
_SITE_RE = re.compile(r"^\s*([^;]+?)\s*;\s*([^;]+?)\s*(?:;.*)?$")
_TAXID_RE = re.compile(r"NCBI_TaxID=(\d+)")


@dataclass
class CellosaurusEntry:
    accession: Optional[str] = None      # CVCL_xxxx
    name: Optional[str] = None           # recommended name (ID)
    synonyms: List[str] = field(default_factory=list)
    species: Optional[str] = None
    sex: Optional[str] = None
    age: Optional[str] = None
    diseases: List[str] = field(default_factory=list)
    site: Optional[str] = None           # e.g. "Colon"
    site_type: Optional[str] = None      # e.g. "In situ" / "Metastatic"
    category: Optional[str] = None

    @property
    def disease(self) -> Optional[str]:
        return self.diseases[0] if self.diseases else None

    def keys(self) -> Iterator[str]:
        """Every name this entry can be matched by, punctuation-stripped."""
        for value in [self.name] + self.synonyms:
            key = name_key(value)
            if key:
                yield key


def _finish(entry: CellosaurusEntry) -> Optional[CellosaurusEntry]:
    return entry if (entry.accession or entry.name) else None


def parse(path: str) -> Iterator[CellosaurusEntry]:
    """Stream ``cellosaurus.txt`` (optionally .gz/.zip) entry by entry."""
    with open_table(path) as stream:
        entry = CellosaurusEntry()
        started = False
        for line in stream:
            line = line.rstrip("\n").rstrip("\r")
            if line.startswith("//"):
                if started:
                    done = _finish(entry)
                    if done is not None:
                        yield done
                entry = CellosaurusEntry()
                started = False
                continue
            if len(line) < 3 or line[2] != " ":
                continue  # file header and blank lines
            code, value = line[:2], line[5:].strip() if len(line) > 5 else line[3:].strip()

            if code == "ID":
                entry.name = value
                started = True
            elif code == "AC":
                entry.accession = value.strip()
                started = True
            elif code == "SY":
                entry.synonyms.extend(s.strip() for s in value.split(";") if s.strip())
            elif code == "SX":
                entry.sex = value or None
            elif code == "AG":
                entry.age = value or None
            elif code == "CA":
                entry.category = value or None
            elif code == "OX":
                # "NCBI_TaxID=9606; ! Homo sapiens"
                label = value.split("!", 1)[1].strip() if "!" in value else None
                if label:
                    entry.species = normalise_species(label)
                elif _TAXID_RE.search(value):
                    entry.species = normalise_species(_TAXID_RE.search(value).group(1))
            elif code == "DI":
                # "NCIt; C3512; Lung adenocarcinoma"
                parts = [p.strip() for p in value.split(";")]
                if parts and parts[-1]:
                    entry.diseases.append(parts[-1])
            elif code == "CC" and value.startswith("Derived from site:"):
                rest = value[len("Derived from site:"):].strip().rstrip(".")
                match = _SITE_RE.match(rest)
                if match:
                    entry.site_type = match.group(1) or None
                    entry.site = match.group(2) or None
                elif rest:
                    entry.site = rest
        if started:
            done = _finish(entry)
            if done is not None:
                yield done


def organ_of(entry: CellosaurusEntry) -> Optional[str]:
    """Canonical organ for an entry, or None.

    A metastatic sample's site is where the cells were *taken from*, not where
    the tumour arose - "Pleural effusion" for a lung adenocarcinoma - so for
    those the disease decides and the site is only a fallback.
    """
    metastatic = bool(entry.site_type) and "metasta" in entry.site_type.lower()
    if metastatic:
        return organ_from_text(entry.disease) or organ_from_text(entry.site)
    return organ_from_text(entry.site) or organ_from_text(entry.disease)


def load_for(path: str, names: Iterable[str], progress=None) -> Dict[str, CellosaurusEntry]:
    """Index the entries matching ``names``, keyed by :func:`name_key`.

    Only wanted entries are retained, so the full catalogue streams through in
    one pass at negligible memory cost.  When two entries claim the same name,
    the first one wins and the duplicate is ignored - Cellosaurus lists the
    recommended name first, so this favours it over another entry's synonym.
    """
    wanted: Set[str] = {name_key(n) for n in names if name_key(n)}
    if not wanted:
        return {}

    found: Dict[str, CellosaurusEntry] = {}
    scanned = 0
    # Two passes over the keys of each entry: recommended names are matched
    # before synonyms across the whole file, so a synonym collision never
    # shadows a line that has its own entry.
    by_synonym: Dict[str, CellosaurusEntry] = {}
    for entry in parse(path):
        scanned += 1
        if progress and scanned % 20000 == 0:
            progress(scanned, len(found))
        primary = name_key(entry.name)
        if primary in wanted and primary not in found:
            found[primary] = entry
        for key in entry.keys():
            if key in wanted and key not in by_synonym:
                by_synonym[key] = entry
    for key, entry in by_synonym.items():
        found.setdefault(key, entry)
    if progress:
        progress(scanned, len(found))
    return found
