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
