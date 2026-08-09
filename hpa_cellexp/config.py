"""Shared configuration.

Kept separate from :mod:`hpa_cellexp.api` so the CLI can read the default
database path without importing the API module - importing it would build the
module-level ``app`` against the default path before ``--database`` has been
applied.
"""

from __future__ import annotations

import os

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_DB_PATH = os.environ.get(
    "HPA_CELLEXP_DB", os.path.join(os.path.dirname(_PACKAGE_DIR), "data", "hpa_cellexp.sqlite")
)

STATIC_DIR = os.path.join(_PACKAGE_DIR, "static")
