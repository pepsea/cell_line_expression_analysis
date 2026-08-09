"""Shared configuration.

Kept separate from :mod:`hpa_cellexp.api` so the CLI can read the default
database path without importing the API module - importing it would build the
module-level ``app`` against the default path before ``--database`` has been
applied.
"""

from __future__ import annotations

import os

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bump whenever schema.sql changes in a way the read side depends on.
#
# A database built by an older version loads far enough to look healthy - the
# dataset banner appears - and then fails on every cell line query, which shows
# up as "search is broken" rather than as "the database is stale".  The version
# turns that into one clear message at startup.
#
#   1  initial
#   2  cell_lines.name_key    (punctuation-insensitive name search)
#   3  cell_lines.organ_source (provenance of the 由来臓器 assignment)
SCHEMA_VERSION = 3

DEFAULT_DB_PATH = os.environ.get(
    "HPA_CELLEXP_DB", os.path.join(os.path.dirname(_PACKAGE_DIR), "data", "hpa_cellexp.sqlite")
)

STATIC_DIR = os.path.join(_PACKAGE_DIR, "static")

# --- data folders ----------------------------------------------------------
#
# The reference tables (tcga_organ.tsv, labels_ja.tsv, cell_line_organ.tsv,
# organ_keywords.tsv) ship inside the package, but they are curation, not
# code: a deployment will want to edit them without touching the checkout or
# rebuilding the image.  Point HPA_CELLEXP_REFERENCE_DIR at a folder and each
# file is looked up there FIRST, falling back to the packaged copy per file -
# so overriding tcga_organ.tsv alone does not mean copying the other three.
PACKAGED_REFERENCE_DIR = os.path.join(_PACKAGE_DIR, "reference")
REFERENCE_DIR = os.environ.get("HPA_CELLEXP_REFERENCE_DIR") or None

# Default for `build --cellosaurus`.  cellosaurus.txt is ~200 MiB and lives
# wherever the machine keeps its downloads, which is a property of the machine
# rather than of each command line.
CELLOSAURUS_PATH = os.environ.get("HPA_CELLEXP_CELLOSAURUS") or None


def reference_path(filename: str) -> str:
    """Absolute path to a reference table, honouring REFERENCE_DIR."""
    if REFERENCE_DIR:
        candidate = os.path.join(REFERENCE_DIR, filename)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(PACKAGED_REFERENCE_DIR, filename)


def reference_origin(filename: str) -> str:
    """"external" or "packaged" - which copy reference_path() resolved to."""
    packaged = os.path.abspath(os.path.join(PACKAGED_REFERENCE_DIR, filename))
    return "packaged" if os.path.abspath(reference_path(filename)) == packaged else "external"
