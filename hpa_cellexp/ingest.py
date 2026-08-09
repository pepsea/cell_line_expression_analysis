"""Build the SQLite database the web app serves from.

Input files (all optional except the expression matrix):

``rna_celline.tsv[.zip]``
    The expression matrix.  One row per (gene, cell line) with TPM / pTPM /
    nTPM columns.  ~24M rows for a full HPA release.

``cell_line_analysis_data.tsv[.zip]`` (or any cell line metadata table)
    Per-cell-line annotation: origin organ / tissue, disease, species,
    Cellosaurus accession.  Drives the 由来臓器 and 種 facets.

``rna_cell_line_tcga_comparison.tsv[.zip]``
    Cell line <-> TCGA cancer similarity.  Used both as a results-page
    annotation and as a last-resort source of organ assignment.

Everything streams, so peak memory stays proportional to the number of genes
and cell lines (tens of thousands of small dicts) rather than to the row count.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from . import columns as C
from .config import SCHEMA_VERSION
from . import reference as R
from .sources import read_rows

__all__ = ["IngestReport", "build_database", "connect_for_write"]

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Rows buffered before each executemany() against the expression table.
_BATCH = 50_000


@dataclass
class IngestReport:
    genes: int = 0
    cell_lines: int = 0
    expression_rows: int = 0
    skipped_rows: int = 0
    tcga_rows: int = 0
    metrics: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_text(self) -> str:
        lines = [
            "genes            : {:,}".format(self.genes),
            "cell lines       : {:,}".format(self.cell_lines),
            "expression rows  : {:,}".format(self.expression_rows),
            "metrics stored   : {}".format(", ".join(self.metrics) or "(none)"),
            "TCGA rows        : {:,}".format(self.tcga_rows),
            "skipped rows     : {:,}".format(self.skipped_rows),
            "elapsed          : {:.1f}s".format(self.seconds),
        ]
        for warning in self.warnings:
            lines.append("warning          : {}".format(warning))
        return "\n".join(lines)


def connect_for_write(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Bulk-load pragmas.  Both synchronous=OFF and journal_mode=OFF are safe
    # here because a failed build is thrown away and restarted from the source
    # files; journalling in particular has to be off, or the single large load
    # transaction spills a rollback/WAL file as big as the database itself.
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")  # ~256 MB page cache
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as handle:
        conn.executescript(handle.read())


def _file_info(path: Optional[str]) -> Optional[Dict[str, object]]:
    """Identify an input file well enough to tell two releases apart."""
    if not path:
        return None
    info: Dict[str, object] = {
        "name": os.path.basename(path),
        "path": os.path.abspath(path),
    }
    try:
        stat = os.stat(path)
        info["bytes"] = stat.st_size
        info["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    except OSError:
        pass
    return info


def _to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() in {"NA", "N/A", "NAN", "NULL", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class _Interner:
    """Assigns stable small integer ids to repeated string keys."""

    def __init__(self) -> None:
        self._ids: Dict[str, int] = {}

    def get(self, key: str) -> int:
        existing = self._ids.get(key)
        if existing is not None:
            return existing
        new_id = len(self._ids) + 1
        self._ids[key] = new_id
        return new_id

    def __contains__(self, key: str) -> bool:
        return key in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def items(self):
        return self._ids.items()


# --------------------------------------------------------------------------
# cell line metadata
# --------------------------------------------------------------------------


@dataclass
class CellLineRecord:
    name: str
    organ: Optional[str] = None
    organ_source: Optional[str] = None
    tissue: Optional[str] = None
    disease: Optional[str] = None
    species: Optional[str] = None
    cellosaurus_id: Optional[str] = None
    sex: Optional[str] = None
    age: Optional[str] = None
    source: Optional[str] = None


def _load_metadata(path: str, report: IngestReport) -> Dict[str, CellLineRecord]:
    """Read a cell line annotation table into ``{name: CellLineRecord}``."""
    records: Dict[str, CellLineRecord] = {}
    label = os.path.basename(path)

    for header, rows in read_rows(path):
        idx_name = C.resolve(header, C.CELL_LINE, required=True)
        idx = {
            "organ": C.resolve(header, C.ORGAN),
            "tissue": C.resolve(header, C.TISSUE),
            "disease": C.resolve(header, C.DISEASE),
            "species": C.resolve(header, C.SPECIES),
            "cvcl": C.resolve(header, C.CELLOSAURUS),
            "sex": C.resolve(header, C.SEX),
            "age": C.resolve(header, C.AGE),
        }
        if idx["organ"] is None and idx["tissue"] is None and idx["disease"] is None:
            report.warnings.append(
                "{}: no organ/tissue/disease column found (headers: {}); "
                "由来臓器 facet will fall back to TCGA inference".format(label, ", ".join(header))
            )

        def cell(row: Sequence[str], key: str) -> Optional[str]:
            position = idx[key]
            if position is None or position >= len(row):
                return None
            value = row[position].strip()
            return value or None

        for row in rows:
            if idx_name >= len(row):
                continue
            name = row[idx_name].strip()
            if not name:
                continue

            tissue = cell(row, "tissue")
            disease = cell(row, "disease")

            # Resolve the organ and remember HOW it was resolved, so a
            # surprising assignment can be traced instead of just doubted.
            explicit = R.normalise_organ(cell(row, "organ"))
            inferred = R.organ_from_text(disease, tissue)
            if explicit and R.is_broad_organ(explicit) and inferred:
                organ = inferred
                organ_source = "{}列を疾患/組織名で細分化 ({} → {})".format(
                    "organ", explicit, inferred
                )
            elif explicit:
                organ, organ_source = explicit, "メタデータの organ 列"
            elif inferred:
                organ, organ_source = inferred, "疾患/組織名からの推定"
            else:
                organ, organ_source = None, None

            record = records.get(name)
            if record is None:
                record = CellLineRecord(name=name, source=label)
                records[name] = record

            # Merge: never overwrite an already-populated field, so passing
            # several metadata files layers them in argument order.
            if record.organ is None and organ is not None:
                record.organ, record.organ_source = organ, organ_source
            record.tissue = record.tissue or R.normalise_organ(tissue)
            record.disease = record.disease or disease
            record.species = record.species or (
                R.normalise_species(cell(row, "species")) if idx["species"] is not None else None
            )
            record.cellosaurus_id = record.cellosaurus_id or R.extract_cvcl(cell(row, "cvcl"))
            record.sex = record.sex or cell(row, "sex")
            record.age = record.age or cell(row, "age")

    return records


def _load_tcga(path: str, report: IngestReport):
    """Yield ``(cell_line_name, tcga_code, rank, nes, spearman)`` tuples."""
    for header, rows in read_rows(path):
        idx_cell = C.resolve(header, C.CELL_LINE, required=True)
        idx_cancer = C.resolve(header, C.TCGA_CANCER, required=True)
        idx_rank = C.resolve(header, C.RANK)
        idx_nes = C.resolve(header, C.NES)
        idx_rho = C.resolve(header, C.SPEARMAN)

        def cell(row: Sequence[str], position: Optional[int]) -> Optional[str]:
            if position is None or position >= len(row):
                return None
            return row[position].strip() or None

        for row in rows:
            if idx_cell >= len(row) or idx_cancer >= len(row):
                report.skipped_rows += 1
                continue
            name = row[idx_cell].strip()
            cancer = row[idx_cancer].strip().upper()
            if not name or not cancer:
                report.skipped_rows += 1
                continue
            rank_text = cell(row, idx_rank)
            try:
                rank = int(float(rank_text)) if rank_text else None
            except ValueError:
                rank = None
            yield (
                name,
                cancer,
                rank,
                _to_float(cell(row, idx_nes) or ""),
                _to_float(cell(row, idx_rho) or ""),
            )


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def build_database(
    db_path: str,
    expression_path: str,
    metadata_paths: Optional[Iterable[str]] = None,
    tcga_path: Optional[str] = None,
    release: Optional[str] = None,
    demo: bool = False,
    progress: bool = True,
) -> IngestReport:
    """Create ``db_path`` from the given HPA files, replacing any existing file."""
    started = time.time()
    report = IngestReport()

    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        stale = db_path + suffix
        if os.path.exists(stale):
            os.remove(stale)

    conn = connect_for_write(db_path)
    try:
        _apply_schema(conn)

        # 1. Cell line metadata -------------------------------------------
        metadata: Dict[str, CellLineRecord] = {}
        for path in metadata_paths or ():
            for name, record in _load_metadata(path, report).items():
                existing = metadata.get(name)
                if existing is None:
                    metadata[name] = record
                else:
                    if existing.organ is None and record.organ is not None:
                        existing.organ = record.organ
                        existing.organ_source = record.organ_source
                    for attr in ("tissue", "disease", "species", "cellosaurus_id", "sex", "age"):
                        if getattr(existing, attr) is None:
                            setattr(existing, attr, getattr(record, attr))

        # 2. TCGA similarity ----------------------------------------------
        tcga_rows: List[tuple] = []
        best_tcga: Dict[str, tuple] = {}  # name -> (rank, code)
        if tcga_path:
            for name, cancer, rank, nes, rho in _load_tcga(tcga_path, report):
                tcga_rows.append((name, cancer, rank, nes, rho))
                key = rank if rank is not None else 10**6
                current = best_tcga.get(name)
                if current is None or key < current[0]:
                    best_tcga[name] = (key, cancer)

        # 3. Expression matrix (streaming) ---------------------------------
        gene_ids = _Interner()
        cell_ids = _Interner()
        gene_symbol: Dict[str, str] = {}

        cursor = conn.cursor()
        cursor.execute("BEGIN")

        batch: List[tuple] = []
        # Duplicate (gene, cell line) pairs are dropped by INSERT OR IGNORE
        # against the primary key rather than by a Python-side `seen` set,
        # which would need several GB at full-release scale.
        rows_read = 0

        for header, rows in read_rows(expression_path):
            idx_cell = C.resolve(header, C.CELL_LINE, required=True)
            idx_gene = C.resolve(header, C.GENE_ID)
            idx_symbol = C.resolve(header, C.GENE_SYMBOL)
            if idx_gene is None and idx_symbol is None:
                raise C.MissingColumn(
                    "expression file has neither a gene id nor a gene name column "
                    "(headers: {})".format(", ".join(header))
                )
            idx_tpm = C.resolve(header, C.TPM)
            idx_ptpm = C.resolve(header, C.PTPM)
            idx_ntpm = C.resolve(header, C.NTPM)
            if idx_tpm is None and idx_ptpm is None and idx_ntpm is None:
                raise C.MissingColumn(
                    "expression file has no TPM/pTPM/nTPM column (headers: {})".format(", ".join(header))
                )
            report.metrics = [
                name
                for name, position in (("tpm", idx_tpm), ("ptpm", idx_ptpm), ("ntpm", idx_ntpm))
                if position is not None
            ]

            width = max(p for p in (idx_cell, idx_gene, idx_symbol, idx_tpm, idx_ptpm, idx_ntpm) if p is not None)

            for row in rows:
                if len(row) <= width:
                    report.skipped_rows += 1
                    continue

                cell_name = row[idx_cell].strip()
                gene_key = (row[idx_gene].strip() if idx_gene is not None else "") or (
                    row[idx_symbol].strip() if idx_symbol is not None else ""
                )
                if not cell_name or not gene_key:
                    report.skipped_rows += 1
                    continue

                gid = gene_ids.get(gene_key)
                if idx_symbol is not None and gene_key not in gene_symbol:
                    symbol = row[idx_symbol].strip()
                    if symbol:
                        gene_symbol[gene_key] = symbol
                cid = cell_ids.get(cell_name)

                batch.append(
                    (
                        gid,
                        cid,
                        _to_float(row[idx_tpm]) if idx_tpm is not None else None,
                        _to_float(row[idx_ptpm]) if idx_ptpm is not None else None,
                        _to_float(row[idx_ntpm]) if idx_ntpm is not None else None,
                    )
                )
                rows_read += 1
                if len(batch) >= _BATCH:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO expression VALUES (?,?,?,?,?)", batch
                    )
                    batch.clear()
                    if progress:
                        _tick(rows_read, len(gene_ids), len(cell_ids), started)

        if batch:
            cursor.executemany("INSERT OR IGNORE INTO expression VALUES (?,?,?,?,?)", batch)
            batch.clear()
        if progress:
            _tick(rows_read, len(gene_ids), len(cell_ids), started, final=True)

        report.expression_rows = cursor.execute("SELECT COUNT(*) FROM expression").fetchone()[0]
        report.skipped_rows += rows_read - report.expression_rows

        # 4. Dimension tables ----------------------------------------------
        cursor.executemany(
            "INSERT INTO genes (id, ensembl_id, symbol, symbol_uc) VALUES (?,?,?,?)",
            [
                (
                    gid,
                    key if key.upper().startswith("ENSG") else None,
                    gene_symbol.get(key, key),
                    gene_symbol.get(key, key).upper(),
                )
                for key, gid in gene_ids.items()
            ],
        )
        report.genes = len(gene_ids)

        unannotated = 0
        tcga_inferred = 0
        cell_rows = []
        for name, cid in cell_ids.items():
            record = metadata.get(name) or CellLineRecord(name=name)
            organ = record.organ
            organ_source = record.organ_source
            if organ is None:
                best = best_tcga.get(name)
                if best is not None:
                    organ = R.tcga_organ_map().get(best[1])
                    if organ:
                        # Similarity to a TCGA cohort is not provenance - it is
                        # the weakest source here and is labelled as such.
                        organ_source = "TCGA類似度からの推定 ({}) ※参考値".format(best[1])
                        tcga_inferred += 1
            if organ is None:
                organ = R.organ_from_text(name)
                if organ:
                    organ_source = "細胞株名からの推定 ※参考値"
            if organ is None:
                unannotated += 1
            cell_rows.append(
                (
                    cid,
                    name,
                    name.upper(),
                    R.name_key(name),
                    organ,
                    organ_source,
                    record.tissue,
                    record.disease,
                    R.normalise_species(record.species),
                    record.cellosaurus_id,
                    record.sex,
                    record.age,
                    record.source,
                )
            )
        cursor.executemany(
            "INSERT INTO cell_lines "
            "(id, name, name_uc, name_key, organ, organ_source, tissue, disease, "
            " species, cellosaurus_id, sex, age, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            cell_rows,
        )
        report.cell_lines = len(cell_rows)
        if tcga_inferred:
            report.warnings.append(
                "{} cell lines have an organ inferred from TCGA similarity rather than "
                "from metadata; these are marked '参考値' in the UI".format(tcga_inferred)
            )
        if unannotated:
            report.warnings.append(
                "{} of {} cell lines have no organ assignment; supply a metadata file "
                "with an organ/tissue/disease column to populate the 由来臓器 filter".format(
                    unannotated, len(cell_rows)
                )
            )

        # 5. TCGA table (needs cell line ids) --------------------------------
        if tcga_rows:
            resolved = [
                (cell_ids.get(name), cancer, rank, nes, rho)
                for name, cancer, rank, nes, rho in tcga_rows
                if name in cell_ids
            ]
            cursor.executemany(
                "INSERT OR IGNORE INTO tcga_similarity VALUES (?,?,?,?,?)", resolved
            )
            report.tcga_rows = len(resolved)

        # 6. Metadata --------------------------------------------------------
        report.seconds = time.time() - started
        meta_values = {
            "schema_version": str(SCHEMA_VERSION),
            "release": release or "unspecified",
            "is_demo": "1" if demo else "0",
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expression_source": os.path.basename(expression_path),
            # Which files this database was actually built from, so the site
            # can state its provenance instead of just "some HPA release".
            "sources": json.dumps(
                {
                    "expression": _file_info(expression_path),
                    "metadata": [_file_info(p) for p in (metadata_paths or ())],
                    "tcga": _file_info(tcga_path),
                },
                ensure_ascii=False,
            ),
            "metrics": ",".join(report.metrics),
            "gene_count": str(report.genes),
            "cell_line_count": str(report.cell_lines),
            "expression_rows": str(report.expression_rows),
        }
        cursor.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", sorted(meta_values.items())
        )

        conn.commit()
        if progress:
            print("optimising database ...", file=sys.stderr)
        conn.execute("ANALYZE")
        conn.commit()
        # Restore normal journalling so the finished file behaves like an
        # ordinary SQLite database for whoever reads it next.
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()

    report.seconds = time.time() - started
    return report


def _tick(rows: int, genes: int, cells: int, started: float, final: bool = False) -> None:
    elapsed = max(time.time() - started, 1e-6)
    print(
        "\r  {:>12,} rows | {:>6,} genes | {:>5,} cell lines | {:>7,.0f} rows/s".format(
            rows, genes, cells, rows / elapsed
        ),
        end="\n" if final else "",
        file=sys.stderr,
        flush=True,
    )
