-- Schema for the HPA cell line expression explorer.
--
-- Design notes:
--   * `expression` is the only large table (~20k genes x ~1.2k cell lines =~ 24M rows
--     for the full HPA rna_celline.tsv).  It is declared WITHOUT ROWID with a
--     (gene_id, cell_line_id) primary key so rows for one gene are physically
--     clustered: the site's core query ("give me these N genes across all cell
--     lines") becomes N contiguous index range scans.
--   * Metric columns are nullable because HPA releases have varied over time
--     (older releases ship TPM only, newer ones nTPM/pTPM as well).

-- No journal_mode pragma here on purpose: the bulk loader turns journalling
-- off for the load (a single 24M-row transaction in WAL mode would grow a
-- -wal file the size of the whole database) and restores the default DELETE
-- journal at the end, so the shipped database is one self-contained file.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS genes (
    id         INTEGER PRIMARY KEY,
    ensembl_id TEXT UNIQUE,
    symbol     TEXT,
    -- Uppercased symbol, used for case-insensitive exact matching without
    -- relying on COLLATE NOCASE (which is ASCII-only but also blocks the
    -- index from being used for LIKE 'x%' prefix scans).
    symbol_uc  TEXT
);

CREATE INDEX IF NOT EXISTS idx_genes_symbol_uc ON genes (symbol_uc);

CREATE TABLE IF NOT EXISTS cell_lines (
    id             INTEGER PRIMARY KEY,
    name           TEXT UNIQUE,
    name_uc        TEXT,
    -- Upper-cased, punctuation stripped: lets "hek293" match "HEK 293".
    name_key       TEXT,
    organ          TEXT,   -- 由来臓器 (normalised, e.g. "Lung")
    -- How `organ` was decided: metadata column, refined from disease text,
    -- inferred from TCGA similarity, ... Lets a surprising organ be traced.
    organ_source   TEXT,
    tissue         TEXT,   -- finer-grained origin as reported by the source file
    disease        TEXT,   -- e.g. "Lung adenocarcinoma"
    species        TEXT,   -- 種 (e.g. "Homo sapiens")
    cellosaurus_id TEXT,   -- e.g. "CVCL_0023"
    sex            TEXT,
    age            TEXT,
    source         TEXT    -- which input file supplied the metadata
);

CREATE INDEX IF NOT EXISTS idx_cell_lines_organ   ON cell_lines (organ);
CREATE INDEX IF NOT EXISTS idx_cell_lines_species ON cell_lines (species);
CREATE INDEX IF NOT EXISTS idx_cell_lines_name_uc  ON cell_lines (name_uc);
CREATE INDEX IF NOT EXISTS idx_cell_lines_name_key ON cell_lines (name_key);

CREATE TABLE IF NOT EXISTS expression (
    gene_id      INTEGER NOT NULL,
    cell_line_id INTEGER NOT NULL,
    tpm          REAL,
    ptpm         REAL,
    ntpm         REAL,
    PRIMARY KEY (gene_id, cell_line_id)
) WITHOUT ROWID;

-- Optional enrichment: HPA's rna_cell_line_tcga_comparison.tsv.
CREATE TABLE IF NOT EXISTS tcga_similarity (
    cell_line_id INTEGER NOT NULL,
    tcga_cancer  TEXT    NOT NULL,
    rank         INTEGER,
    nes          REAL,
    spearman     REAL,
    PRIMARY KEY (cell_line_id, tcga_cancer)
);
