"""Transparent streaming readers for the HPA download files.

The published files are distributed as ``*.tsv.zip``; users also commonly have
them lying around unzipped or gzipped.  Everything here streams so that
``rna_celline.tsv`` (~2 GB uncompressed, ~24M rows) never has to be unpacked to
disk or held in memory.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import zipfile
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence, Tuple

__all__ = ["open_table", "read_rows", "describe", "looks_like_cellosaurus"]

# HPA gene rows are long-ish but well under the default limit; raise it anyway
# so a stray quoted field can never abort a multi-hour ingest.
csv.field_size_limit(4 * 1024 * 1024)


def _pick_member(archive: zipfile.ZipFile, hint: Optional[str]) -> str:
    members = [n for n in archive.namelist() if not n.endswith("/")]
    # Ignore macOS resource forks that sneak into user-made archives.
    members = [n for n in members if not os.path.basename(n).startswith("._")]
    if not members:
        raise ValueError("zip archive contains no files")
    if hint:
        for name in members:
            if os.path.basename(name) == hint:
                return name
    tabular = [n for n in members if n.lower().endswith((".tsv", ".csv", ".txt"))]
    candidates = tabular or members
    if len(candidates) > 1:
        raise ValueError(
            "zip archive contains multiple tables ({}); pass member= to choose one".format(
                ", ".join(sorted(candidates))
            )
        )
    return candidates[0]


@contextmanager
def open_table(path: str, member: Optional[str] = None) -> Iterator[io.TextIOBase]:
    """Yield a text stream for ``path``, transparently handling .zip and .gz."""
    lower = path.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            name = _pick_member(archive, member)
            with archive.open(name, "r") as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    elif lower.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            yield handle
    else:
        with open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            yield handle


def _sniff_delimiter(header_line: str) -> str:
    if "\t" in header_line:
        return "\t"
    if header_line.count(",") > header_line.count(";"):
        return ","
    return ";" if ";" in header_line else "\t"


def read_rows(
    path: str, member: Optional[str] = None
) -> Iterator[Tuple[Sequence[str], Iterator[List[str]]]]:
    """Context-manager-ish generator yielding ``(header, row_iterator)`` once.

    Usage::

        for header, rows in read_rows("rna_celline.tsv.zip"):
            ...
    """
    with open_table(path, member) as stream:
        first = stream.readline()
        if not first:
            raise ValueError("{}: file is empty".format(path))
        delimiter = _sniff_delimiter(first)
        header = next(csv.reader([first], delimiter=delimiter))
        reader = csv.reader(stream, delimiter=delimiter)
        yield [h.strip() for h in header], reader


def looks_like_cellosaurus(path: str) -> bool:
    """Is this the Cellosaurus flat file rather than a table?

    It is not delimited at all - "ID   HeLa" / "AC   CVCL_0030" records
    separated by //  - so column detection reports nothing usable and the file
    reads as junk.  It is in fact the single most useful input for 由来臓器.
    """
    try:
        with open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for _ in range(400):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("AC   CVCL_") or line.startswith("ID   "):
                    return True
    except OSError:
        return False
    return False


def describe(path: str, member: Optional[str] = None, limit: int = 3) -> str:
    """Return a short human-readable preview - used by ``inspect`` CLI command."""
    if looks_like_cellosaurus(path):
        return ("Cellosaurus flat file (ID / AC / CC ... // レコード形式)\n"
                "  → --cellosaurus に使えます")
    lines = []
    for header, rows in read_rows(path, member):
        lines.append("columns ({}): {}".format(len(header), ", ".join(header)))
        lines.append(_role_summary(header))
        for i, row in enumerate(rows):
            if i >= limit:
                break
            lines.append("  " + " | ".join(row))
    return "\n".join(lines)


def _role_summary(header: Sequence[str]) -> str:
    """Which of the fields the pipeline needs this file can supply.

    "Is this the metadata file?" is otherwise answered by squinting at a list
    of column names - and getting it wrong costs a full rebuild.
    """
    from . import columns as C

    roles = [
        ("cell line", C.CELL_LINE),
        ("organ", C.ORGAN),
        ("tissue", C.TISSUE),
        ("disease", C.DISEASE),
        ("species", C.SPECIES),
        ("expression", C.NTPM),
        ("TCGA", C.TCGA_CANCER),
    ]
    found = [label for label, spec in roles if C.resolve(header, spec) is not None]
    if not found:
        return "  → 使える列なし / no recognised fields"

    verdict = "  → 認識できた列 / recognised: {}".format(", ".join(found))
    has_cell_line = "cell line" in found
    annotation = any(f in found for f in ("organ", "tissue", "disease", "species"))
    if has_cell_line and annotation:
        verdict += "\n  → --metadata に使えます"
    elif has_cell_line and "expression" in found:
        verdict += "\n  → --expression に使えます"
    elif has_cell_line and "TCGA" in found:
        verdict += "\n  → --tcga に使えます"
    elif has_cell_line:
        verdict += (
            "\n  → 細胞株名しかありません。--metadata としては使えません"
            "（由来臓器・疾患の列がない）"
        )
    return verdict
