"""Command line entry point: ``python -m hpa_cellexp <command>``."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from . import config
from .config import DEFAULT_DB_PATH, SCHEMA_VERSION


def _cmd_build(args: argparse.Namespace) -> int:
    from .columns import MissingColumn
    from .ingest import build_database

    try:
        report = _run_build(args, build_database)
    except MissingColumn as exc:
        print("列を認識できませんでした / column not found:\n  {}\n\n"
              "ファイルの列構成を確認:  python -m hpa_cellexp inspect <FILE>\n"
              "列を明示指定:            --cell-line-column / --organ-column / "
              "--disease-column / --tissue-column".format(exc), file=sys.stderr)
        return 1
    print(report.as_text())
    size = os.path.getsize(args.database) / (1024 ** 2)
    print("database         : {} ({:,.1f} MiB)".format(args.database, size))
    return 0


def _run_build(args, build_database):
    return build_database(
        db_path=args.database,
        expression_path=args.expression,
        metadata_paths=args.metadata,
        tcga_path=args.tcga,
        cellosaurus_path=args.cellosaurus,
        release=args.release,
        demo=False,
        progress=not args.quiet,
        column_overrides={
            "cell-line": args.cell_line_column,
            "organ": args.organ_column,
            "tissue": args.tissue_column,
            "disease": args.disease_column,
        },
    )


def _cmd_inspect(args: argparse.Namespace) -> int:
    from .sources import describe

    for path in args.files:
        print("== {}".format(path))
        print(describe(path, limit=args.rows))
        print()
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import build_demo_database

    report = build_demo_database(args.database)
    print(report.as_text())
    print("database         : {} (DEMO DATA - not real measurements)".format(args.database))
    return 0


def _cmd_organs(args: argparse.Namespace) -> int:
    """Show how each cell line got its 由来臓器 - the answer to "is this wrong?"."""
    from .queries import Database, DatabaseMissing

    db = Database(args.database)
    try:
        rows = db.cell_lines(name_query=args.grep) if args.grep else db.cell_lines()
    except DatabaseMissing as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.organ:
        wanted = args.organ.strip().lower()
        rows = [r for r in rows if (r["organ"] or "").lower() == wanted]

    if not rows:
        print("no cell lines matched", file=sys.stderr)
        return 1

    width = max(len(r["name"]) for r in rows)
    print("{:<{w}}  {:<26} {:<34} {}".format("CELL LINE", "ORGAN", "DISEASE", "SOURCE", w=width))
    for row in rows:
        print(
            "{:<{w}}  {:<26} {:<34} {}".format(
                row["name"],
                row["organ"] or "-",
                (row["disease"] or "-")[:34],
                row["organSource"] or "-",
                w=width,
            )
        )
    print("\n{} cell lines".format(len(rows)), file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    db_path = os.path.abspath(args.database)
    # Also exported so the --reload worker, which re-imports the module in a
    # fresh process, picks up the same database.
    os.environ["HPA_CELLEXP_DB"] = db_path

    if not os.path.exists(db_path):
        print(
            "database not found: {}\n"
            "build it first:  python -m hpa_cellexp build --expression rna_celline.tsv.zip\n"
            "or try the demo: python -m hpa_cellexp demo".format(db_path),
            file=sys.stderr,
        )
        return 1

    # Fail fast on a stale database: it would otherwise start cleanly, show the
    # dataset banner, and then 500 on every cell line query.
    from .queries import Database, DatabaseMissing

    try:
        Database(db_path).info()
    except DatabaseMissing as exc:
        print(exc, file=sys.stderr)
        return 1

    print("serving {} on http://{}:{}".format(db_path, args.host, args.port), file=sys.stderr)
    if args.reload or args.workers > 1:
        # Both need an import string so uvicorn can re-import in each worker
        # process; the env var set above carries the database path across.
        uvicorn.run(
            "hpa_cellexp.api:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=None if args.reload else args.workers,
            log_level="info",
        )
    else:
        # Pass the application object so --database applies even though this
        # process may already have imported hpa_cellexp.api.
        from .api import create_app

        uvicorn.run(create_app(db_path), host=args.host, port=args.port, log_level="info")
    return 0


class _PrintAndExit(argparse.Action):
    """Print `text` verbatim and exit 0."""

    def __init__(self, option_strings, dest, text="", **kwargs):
        super().__init__(option_strings, dest, **kwargs)
        self.text = text

    def __call__(self, parser, namespace, values, option_string=None):
        print(self.text)
        parser.exit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpa_cellexp",
        description="Human Protein Atlas cell line expression explorer",
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DB_PATH,
        help="path to the SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--reference-dir",
        metavar="DIR",
        default=config.REFERENCE_DIR,
        help="folder holding the reference tables (tcga_organ.tsv, labels_ja.tsv, "
             "cell_line_organ.tsv, organ_keywords.tsv).  Each file is taken from "
             "here when present and from the packaged copy otherwise "
             "(env HPA_CELLEXP_REFERENCE_DIR, default: %(default)s)",
    )
    parser.add_argument(
        "--version",
        # argparse's built-in version action word-wraps, which mangles a
        # multi-line report into one paragraph.
        action=_PrintAndExit,
        nargs=0,
        text="hpa_cellexp {} (schema v{})\n"
                "  install       : {}\n"
                "  database      : {}\n"
                "  reference dir : {}\n"
                "  cellosaurus   : {}".format(
                    __version__,
                    SCHEMA_VERSION,
                    os.path.dirname(os.path.abspath(__file__)),
                    DEFAULT_DB_PATH,
                    config.REFERENCE_DIR or "(packaged: {})".format(
                        config.PACKAGED_REFERENCE_DIR),
                    config.CELLOSAURUS_PATH or "(unset)",
                ),
        help="print the version, schema version and the folders in use",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def accept_database_after_the_subcommand(subparser: argparse.ArgumentParser) -> None:
        """Let --database appear on either side of the subcommand.

        argparse binds an option to the parser that declares it, so a global
        --database placed after the subcommand fails with "unrecognized
        arguments" - which reads like the option does not exist at all.
        SUPPRESS keeps the subparser from overwriting a value given globally.
        """
        subparser.add_argument("--database", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        subparser.add_argument(
            "--reference-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )

    build = sub.add_parser("build", help="build the database from HPA download files")
    build.add_argument(
        "--expression",
        required=True,
        help="rna_celline.tsv (.tsv, .tsv.zip or .tsv.gz)",
    )
    build.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="FILE",
        help="cell line annotation table; repeatable, earlier files win",
    )
    build.add_argument("--tcga", help="rna_cell_line_tcga_comparison.tsv[.zip]")
    build.add_argument(
        "--cellosaurus",
        metavar="FILE",
        default=config.CELLOSAURUS_PATH,
        help="cellosaurus.txt from https://ftp.expasy.org/databases/cellosaurus/ - "
             "fills in 由来臓器, Cellosaurus accession, species, sex, age and disease "
             "for cell lines the metadata does not cover "
             "(env HPA_CELLEXP_CELLOSAURUS, default: %(default)s)",
    )
    build.add_argument("--release", help="label for the HPA release, e.g. 'HPA v24'")
    build.add_argument("--quiet", action="store_true", help="suppress progress output")
    # Escape hatch for metadata files whose headers the alias lists miss.
    build.add_argument("--cell-line-column", metavar="HEADER",
                       help="exact header holding the cell line name")
    build.add_argument("--organ-column", metavar="HEADER",
                       help="exact header holding the organ of origin (由来臓器)")
    build.add_argument("--tissue-column", metavar="HEADER", help="exact header holding the tissue")
    build.add_argument("--disease-column", metavar="HEADER", help="exact header holding the disease")
    accept_database_after_the_subcommand(build)
    build.set_defaults(func=_cmd_build)

    inspect = sub.add_parser("inspect", help="print the header and first rows of input files")
    inspect.add_argument("files", nargs="+")
    inspect.add_argument("--rows", type=int, default=3)
    accept_database_after_the_subcommand(inspect)
    inspect.set_defaults(func=_cmd_inspect)

    demo = sub.add_parser("demo", help="build a small synthetic database for UI testing")
    accept_database_after_the_subcommand(demo)
    demo.set_defaults(func=_cmd_demo)

    organs = sub.add_parser(
        "organs", help="show each cell line's 由来臓器 and where that assignment came from"
    )
    organs.add_argument("--grep", help="only cell lines whose name matches (punctuation-insensitive)")
    organs.add_argument("--organ", help="only cell lines assigned to this organ, e.g. Colon")
    accept_database_after_the_subcommand(organs)
    organs.set_defaults(func=_cmd_organs)

    serve = sub.add_parser("serve", help="run the web server")
    # Defaults come from the environment so a container can be reconfigured
    # without overriding the image's CMD.
    serve.add_argument(
        "--host",
        default=os.environ.get("HPA_CELLEXP_HOST", "127.0.0.1"),
        help="interface to bind (env HPA_CELLEXP_HOST, default: %(default)s)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HPA_CELLEXP_PORT", "8000")),
        help="port to listen on (env HPA_CELLEXP_PORT, default: %(default)s)",
    )
    serve.add_argument("--reload", action="store_true")
    serve.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("HPA_CELLEXP_WORKERS", "1")),
        help="worker processes (default: %(default)s). The database is opened "
             "read-only, so workers scale reads safely.",
    )
    accept_database_after_the_subcommand(serve)
    serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # A folder given on the command line has to reach the reference readers
    # before anything asks them a question, and their caches may already be
    # warm from an earlier call in the same process.
    if getattr(args, "reference_dir", None) != config.REFERENCE_DIR:
        config.REFERENCE_DIR = args.reference_dir
        os.environ.pop("HPA_CELLEXP_REFERENCE_DIR", None)
        if args.reference_dir:
            os.environ["HPA_CELLEXP_REFERENCE_DIR"] = args.reference_dir
        from . import reference

        reference.reload()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
