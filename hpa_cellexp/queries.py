"""Read-side data access for the web API."""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .reference import cellosaurus_url, label_ja, name_key

__all__ = ["Database", "DatabaseMissing"]

METRICS = ("ntpm", "ptpm", "tpm")

# SQLite has a hard cap (SQLITE_MAX_VARIABLE_NUMBER, historically 999) on bound
# parameters per statement; chunk long IN () lists below it with room to spare.
_PARAM_CHUNK = 500


class DatabaseMissing(RuntimeError):
    pass


class Database:
    """Thin, thread-safe wrapper around a read-only SQLite connection pool.

    One connection per thread: ``sqlite3`` connections are not safe to share
    across threads, and uvicorn runs sync endpoints in a worker pool.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()

    # -- plumbing ---------------------------------------------------------

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self.exists():
                raise DatabaseMissing(
                    "database not found at {}. Build it first: "
                    "python -m hpa_cellexp build --expression rna_celline.tsv.zip".format(self.path)
                )
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(self.path), uri=True, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            self._local.conn = conn
        return conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    # -- metadata ---------------------------------------------------------

    def info(self) -> Dict[str, Any]:
        rows = self._query("SELECT key, value FROM meta")
        meta = {row["key"]: row["value"] for row in rows}
        metrics = [m for m in (meta.get("metrics") or "").split(",") if m]
        return {
            "release": meta.get("release", "unspecified"),
            "isDemo": meta.get("is_demo") == "1",
            "builtAt": meta.get("built_at"),
            "source": meta.get("expression_source"),
            "metrics": metrics or list(METRICS),
            "geneCount": int(meta.get("gene_count") or 0),
            "cellLineCount": int(meta.get("cell_line_count") or 0),
            "expressionRows": int(meta.get("expression_rows") or 0),
        }

    def facets(self) -> Dict[str, List[Dict[str, Any]]]:
        """Distinct organ / species / disease values with cell line counts."""

        def collect(column: str) -> List[Dict[str, Any]]:
            rows = self._query(
                "SELECT {0} AS value, COUNT(*) AS n FROM cell_lines "
                "WHERE {0} IS NOT NULL AND {0} != '' GROUP BY {0} ORDER BY n DESC, value".format(column)
            )
            # `labelJa` is display/search only - `value` stays the filter key.
            return [
                {"value": r["value"], "count": r["n"], "labelJa": label_ja(r["value"])}
                for r in rows
            ]

        unassigned = self._query(
            "SELECT COUNT(*) AS n FROM cell_lines WHERE organ IS NULL OR organ = ''"
        )[0]["n"]
        return {
            "organs": collect("organ"),
            "species": collect("species"),
            "diseases": collect("disease"),
            "organUnassigned": unassigned,
        }

    # -- genes ------------------------------------------------------------

    def search_genes(self, prefix: str, limit: int = 20) -> List[Dict[str, Any]]:
        prefix = (prefix or "").strip().upper()
        if not prefix:
            return []
        like = prefix.replace("%", "").replace("_", "") + "%"
        rows = self._query(
            "SELECT symbol, ensembl_id FROM genes "
            "WHERE symbol_uc LIKE ? OR ensembl_id LIKE ? "
            "ORDER BY LENGTH(symbol), symbol LIMIT ?",
            (like, like, limit),
        )
        return [{"symbol": r["symbol"], "ensemblId": r["ensembl_id"]} for r in rows]

    def resolve_genes(self, tokens: Iterable[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Map user input (symbols and/or Ensembl ids) onto gene rows.

        Returns ``(found, unmatched)``.  Input order is preserved and repeats
        are collapsed.
        """
        wanted: List[str] = []
        seen = set()
        for token in tokens:
            key = (token or "").strip()
            if not key:
                continue
            upper = key.upper()
            if upper in seen:
                continue
            seen.add(upper)
            wanted.append(key)
        if not wanted:
            return [], []

        by_key: Dict[str, Dict[str, Any]] = {}
        uppers = [w.upper() for w in wanted]
        for start in range(0, len(uppers), _PARAM_CHUNK):
            chunk = uppers[start : start + _PARAM_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._query(
                "SELECT id, ensembl_id, symbol, symbol_uc FROM genes "
                "WHERE symbol_uc IN ({0}) OR UPPER(ensembl_id) IN ({0})".format(placeholders),
                chunk + chunk,
            )
            for row in rows:
                entry = {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "ensemblId": row["ensembl_id"],
                }
                if row["symbol_uc"]:
                    by_key.setdefault(row["symbol_uc"], entry)
                if row["ensembl_id"]:
                    by_key.setdefault(row["ensembl_id"].upper(), entry)

        found: List[Dict[str, Any]] = []
        unmatched: List[str] = []
        used_ids = set()
        for token in wanted:
            entry = by_key.get(token.upper())
            if entry is None:
                unmatched.append(token)
            elif entry["id"] not in used_ids:
                used_ids.add(entry["id"])
                found.append({**entry, "query": token})
        return found, unmatched

    # -- cell lines -------------------------------------------------------

    def _cell_line_filter(
        self,
        organs: Optional[Sequence[str]] = None,
        species: Optional[Sequence[str]] = None,
        diseases: Optional[Sequence[str]] = None,
        name_query: Optional[str] = None,
        names: Optional[Sequence[str]] = None,
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        def add_in(column: str, values: Optional[Sequence[str]]) -> None:
            values = [v for v in (values or []) if v]
            if not values:
                return
            if len(values) > _PARAM_CHUNK:
                raise ValueError("too many values for filter '{}'".format(column))
            clauses.append("{} IN ({})".format(column, ",".join("?" * len(values))))
            params.extend(values)

        add_in("organ", organs)
        add_in("species", species)
        add_in("disease", diseases)
        if names:
            uppers = [n.strip().upper() for n in names if n and n.strip()]
            if uppers:
                if len(uppers) > _PARAM_CHUNK:
                    raise ValueError("too many cell line names")
                clauses.append("name_uc IN ({})".format(",".join("?" * len(uppers))))
                params.extend(uppers)
        if name_query and name_query.strip():
            raw = name_query.strip().upper().replace("%", "").replace("_", "")
            key = name_key(name_query)
            # Match the literal name and the punctuation-stripped key, so both
            # "HEK 293" and "hek293" find the same cell line.
            if key:
                clauses.append("(name_uc LIKE ? OR name_key LIKE ?)")
                params.extend(["%" + raw + "%", "%" + key + "%"])
            else:
                clauses.append("name_uc LIKE ?")
                params.append("%" + raw + "%")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def cell_lines(self, limit: Optional[int] = None, **filters: Any) -> List[Dict[str, Any]]:
        where, params = self._cell_line_filter(**filters)
        sql = (
            "SELECT id, name, organ, tissue, disease, species, cellosaurus_id, sex, age "
            "FROM cell_lines" + where + " ORDER BY organ IS NULL, organ, name"
        )
        if limit:
            sql += " LIMIT ?"
            params = params + [limit]
        return [self._cell_line_dict(row) for row in self._query(sql, params)]

    def count_cell_lines(self, **filters: Any) -> int:
        where, params = self._cell_line_filter(**filters)
        return self._query("SELECT COUNT(*) AS n FROM cell_lines" + where, params)[0]["n"]

    @staticmethod
    def _cell_line_dict(row: sqlite3.Row) -> Dict[str, Any]:
        name = row["name"]
        cvcl = row["cellosaurus_id"]
        return {
            "id": row["id"],
            "name": name,
            "organ": row["organ"],
            "tissue": row["tissue"],
            "disease": row["disease"],
            "species": row["species"],
            "cellosaurusId": cvcl,
            "sex": row["sex"] if "sex" in row.keys() else None,
            "age": row["age"] if "age" in row.keys() else None,
            "databaseUrl": cellosaurus_url(name, cvcl),
        }

    # -- expression -------------------------------------------------------

    def expression_matrix(
        self,
        gene_tokens: Sequence[str],
        metric: str = "ntpm",
        **filters: Any,
    ) -> Dict[str, Any]:
        """Core query behind the site: N genes x filtered cell lines."""
        if metric not in METRICS:
            raise ValueError("unknown metric {!r}; expected one of {}".format(metric, ", ".join(METRICS)))

        genes, unmatched = self.resolve_genes(gene_tokens)
        cell_lines = self.cell_lines(**filters)

        if not genes or not cell_lines:
            return {
                "metric": metric,
                "genes": genes,
                "unmatchedGenes": unmatched,
                "cellLines": cell_lines,
                "values": [[None] * len(cell_lines) for _ in genes],
            }

        cell_index = {row["id"]: position for position, row in enumerate(cell_lines)}
        values: List[List[Optional[float]]] = [[None] * len(cell_lines) for _ in genes]

        # One statement per gene keeps us inside the bound-parameter cap and
        # lets SQLite do a single clustered range scan per gene.
        cell_ids = list(cell_index)
        restrict = len(cell_ids) < self.count_cell_lines()
        conn = self.connect()

        for gene_position, gene in enumerate(genes):
            if restrict:
                rows: List[sqlite3.Row] = []
                for start in range(0, len(cell_ids), _PARAM_CHUNK):
                    chunk = cell_ids[start : start + _PARAM_CHUNK]
                    rows.extend(
                        conn.execute(
                            "SELECT cell_line_id, {} AS v FROM expression "
                            "WHERE gene_id = ? AND cell_line_id IN ({})".format(
                                metric, ",".join("?" * len(chunk))
                            ),
                            [gene["id"]] + chunk,
                        ).fetchall()
                    )
            else:
                rows = conn.execute(
                    "SELECT cell_line_id, {} AS v FROM expression WHERE gene_id = ?".format(metric),
                    (gene["id"],),
                ).fetchall()

            row_values = values[gene_position]
            for row in rows:
                position = cell_index.get(row["cell_line_id"])
                if position is not None:
                    row_values[position] = row["v"]

        return {
            "metric": metric,
            "genes": genes,
            "unmatchedGenes": unmatched,
            "cellLines": cell_lines,
            "values": values,
        }

    def tcga_for_cell_lines(self, cell_line_ids: Sequence[int], top: int = 1) -> Dict[int, List[Dict[str, Any]]]:
        out: Dict[int, List[Dict[str, Any]]] = {}
        ids = list(cell_line_ids)
        for start in range(0, len(ids), _PARAM_CHUNK):
            chunk = ids[start : start + _PARAM_CHUNK]
            rows = self._query(
                "SELECT cell_line_id, tcga_cancer, rank, spearman FROM tcga_similarity "
                "WHERE cell_line_id IN ({}) AND (rank IS NULL OR rank <= ?) "
                "ORDER BY cell_line_id, rank".format(",".join("?" * len(chunk))),
                chunk + [top],
            )
            for row in rows:
                out.setdefault(row["cell_line_id"], []).append(
                    {"cancer": row["tcga_cancer"], "rank": row["rank"], "spearman": row["spearman"]}
                )
        return out
