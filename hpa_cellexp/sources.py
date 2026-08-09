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

__all__ = ["open_table", "read_rows", "describe"]

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


def describe(path: str, member: Optional[str] = None, limit: int = 3) -> str:
    """Return a short human-readable preview - used by ``inspect`` CLI command."""
    lines = []
    for header, rows in read_rows(path, member):
        lines.append("columns ({}): {}".format(len(header), ", ".join(header)))
        for i, row in enumerate(rows):
            if i >= limit:
                break
            lines.append("  " + " | ".join(row))
    return "\n".join(lines)
