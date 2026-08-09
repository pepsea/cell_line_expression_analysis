"""FastAPI application serving the cell line expression explorer."""

from __future__ import annotations

import csv
import io
import os
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import DEFAULT_DB_PATH, STATIC_DIR
from .queries import METRICS, Database, DatabaseMissing

_STATIC_DIR = STATIC_DIR

# Gene tokens arrive pasted from spreadsheets, so accept any run of commas,
# semicolons, whitespace or pipes as a separator.
_TOKEN_SPLIT = re.compile(r"[\s,;|]+")

MAX_GENES = 200

# How many TCGA cohorts to report per cell line.  Similarity to a patient
# tumour cohort says how good a model the cell line is; the top match alone
# reads as more certain than it is when the runners-up score almost the same.
TCGA_TOP_N = 3


def split_gene_tokens(raw: Any) -> List[str]:
    """Normalise gene input given either as a list or as one pasted string."""
    items: List[str] = []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    for value in values:
        if value is None:
            continue
        for token in _TOKEN_SPLIT.split(str(value)):
            token = token.strip().strip('"').strip("'")
            if token:
                items.append(token)
    return items


class ExpressionRequest(BaseModel):
    genes: Any = Field(default_factory=list, description="Symbols and/or Ensembl ids")
    metric: str = "ntpm"
    organs: List[str] = Field(default_factory=list)
    species: List[str] = Field(default_factory=list)
    diseases: List[str] = Field(default_factory=list)
    cellLineQuery: Optional[str] = None
    cellLines: List[str] = Field(default_factory=list)


def _tcga_summary(hits: List[Dict[str, Any]]) -> str:
    """"LUAD (rho=0.79); LUSC (rho=0.71)" - one cell, so the export stays a
    flat table that a spreadsheet can open."""
    parts = []
    for hit in hits:
        rho = hit.get("spearman")
        parts.append(
            "{} (rho={:.2f})".format(hit["cancer"], rho) if rho is not None else hit["cancer"]
        )
    return "; ".join(parts)


def create_app(db_path: str = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(
        title="HPA Cell Line Expression Explorer",
        description="Gene expression across human cell lines, from Human Protein Atlas data.",
        version=__version__,
    )
    db = Database(db_path)
    app.state.db = db

    def _filters(payload: ExpressionRequest) -> Dict[str, Any]:
        return {
            "organs": payload.organs,
            "species": payload.species,
            "diseases": payload.diseases,
            "name_query": payload.cellLineQuery,
            "names": payload.cellLines,
        }

    def _guard(fn):
        try:
            return fn()
        except DatabaseMissing as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # -- API ---------------------------------------------------------------

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"ok": db.exists(), "database": db.path}

    @app.get("/api/meta")
    def meta() -> Dict[str, Any]:
        def build() -> Dict[str, Any]:
            payload = db.info()
            payload["facets"] = db.facets()
            # The running code's version, so the page can tell the user when
            # the browser is showing a cached copy of an older build.
            payload["appVersion"] = __version__
            payload["maxGenes"] = MAX_GENES
            payload["availableMetrics"] = [m for m in METRICS if m in payload["metrics"]] or list(METRICS)
            return payload

        return _guard(build)

    @app.get("/api/genes")
    def genes(q: str = Query("", max_length=64), limit: int = Query(20, ge=1, le=100)):
        return _guard(lambda: {"results": db.search_genes(q, limit)})

    @app.get("/api/cell-lines")
    def cell_lines(
        organ: List[str] = Query(default_factory=list),
        species: List[str] = Query(default_factory=list),
        disease: List[str] = Query(default_factory=list),
        q: Optional[str] = None,
        limit: int = Query(2000, ge=1, le=10000),
    ):
        def build() -> Dict[str, Any]:
            filters = {
                "organs": organ,
                "species": species,
                "diseases": disease,
                "name_query": q,
            }
            return {
                "total": db.count_cell_lines(**filters),
                "results": db.cell_lines(limit=limit, **filters),
            }

        return _guard(build)

    @app.post("/api/expression")
    def expression(payload: ExpressionRequest) -> Dict[str, Any]:
        tokens = split_gene_tokens(payload.genes)
        if not tokens:
            raise HTTPException(status_code=400, detail="遺伝子を1つ以上入力してください。")
        if len(tokens) > MAX_GENES:
            raise HTTPException(
                status_code=400,
                detail="遺伝子は最大 {} 個までです（{} 個入力されました）。".format(MAX_GENES, len(tokens)),
            )

        def build() -> Dict[str, Any]:
            result = db.expression_matrix(tokens, metric=payload.metric, **_filters(payload))
            ids = [c["id"] for c in result["cellLines"]]
            # Up to three: a single best match reads as more certain than it
            # is when the runners-up score almost the same.
            tcga = db.tcga_for_cell_lines(ids, top=TCGA_TOP_N)
            for cell in result["cellLines"]:
                cell["tcga"] = tcga.get(cell["id"]) or []
            return result

        return _guard(build)

    @app.post("/api/expression.tsv", response_class=PlainTextResponse)
    def expression_tsv(payload: ExpressionRequest) -> PlainTextResponse:
        tokens = split_gene_tokens(payload.genes)
        if not tokens:
            raise HTTPException(status_code=400, detail="遺伝子を1つ以上入力してください。")

        result = _guard(
            lambda: db.expression_matrix(tokens, metric=payload.metric, **_filters(payload))
        )
        tcga = _guard(
            lambda: db.tcga_for_cell_lines([c["id"] for c in result["cellLines"]], top=TCGA_TOP_N)
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
        metric = result["metric"]
        writer.writerow(
            ["Cell line", "Organ", "Disease", "Species", "Cellosaurus", "Database URL",
             "Similar TCGA cohorts (top {})".format(TCGA_TOP_N)]
            + ["{} ({})".format(g["symbol"], metric) for g in result["genes"]]
        )
        for column, cell in enumerate(result["cellLines"]):
            writer.writerow(
                [
                    cell["name"],
                    cell["organ"] or "",
                    cell["disease"] or "",
                    cell["species"] or "",
                    cell["cellosaurusId"] or "",
                    cell["databaseUrl"],
                    _tcga_summary(tcga.get(cell["id"]) or []),
                ]
                + [
                    "" if result["values"][row][column] is None else result["values"][row][column]
                    for row in range(len(result["genes"]))
                ]
            )
        filename = "hpa_cell_line_expression_{}.tsv".format(
            time.strftime("%Y%m%d_%H%M%S", time.localtime())
        )
        return PlainTextResponse(
            buffer.getvalue(),
            headers={
                "Content-Disposition": 'attachment; filename="{}"'.format(filename),
                # Let the browser read the name back for the download.
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
            media_type="text/tab-separated-values; charset=utf-8",
        )

    # -- static site --------------------------------------------------------

    if os.path.isdir(_STATIC_DIR):
        app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    return app


app = create_app()
