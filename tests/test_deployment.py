"""Checks on the Docker deployment files.

These cannot start a container, so they guard the parts that are easy to get
wrong and expensive to discover on a server: the database path the image and
the CLI agree on, and the fact that `docker compose run SERVICE ARGS...`
REPLACES `command` (so a one-shot service must carry its subcommand in
`entrypoint`, or passing --expression silently drops the subcommand).
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


class DockerfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("Dockerfile")

    def test_database_lives_in_a_volume_not_the_image(self):
        """A full release is ~900 MiB; baking it in would be unusable."""
        self.assertIn("HPA_CELLEXP_DB=/data/hpa_cellexp.sqlite", self.text)
        self.assertIn('VOLUME ["/data"]', self.text)

    def test_entrypoint_is_the_cli_module(self):
        self.assertIn('ENTRYPOINT ["python", "-m", "hpa_cellexp"]', self.text)
        # Host/port come from the environment, not the command line, so
        # `docker run -e HPA_CELLEXP_PORT=9000` is enough to move the listener.
        self.assertIn('CMD ["serve"]', self.text)
        self.assertIn("HPA_CELLEXP_HOST=0.0.0.0", self.text)
        self.assertIn("HPA_CELLEXP_PORT=8000", self.text)

    def test_runs_unprivileged(self):
        self.assertIn("USER app", self.text)
        self.assertRegex(self.text, r"useradd .*--uid 10001")
        # The volume has to be writable by that user or `build` cannot run.
        self.assertRegex(self.text, r"chown -R app:app[^\n]*\/data")

    def test_healthcheck_opens_the_database(self):
        """/api/health only stats the file; /api/meta actually opens it, so a
        stale database is reported unhealthy instead of failing every query."""
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn("/api/meta", self.text)
        self.assertNotIn("CMD curl", self.text)  # curl is not in the slim image

    def test_healthcheck_command_is_valid_python(self):
        match = re.search(r'CMD python -c "(.+)"\n', self.text)
        self.assertIsNotNone(match, "healthcheck command not found")
        compile(match.group(1), "<healthcheck>", "exec")

    def test_healthcheck_follows_an_overridden_port(self):
        """Otherwise -e HPA_CELLEXP_PORT=9000 makes the container unhealthy
        even though it is serving perfectly well."""
        self.assertIn("HPA_CELLEXP_PORT", self.text.split("HEALTHCHECK", 1)[1])

    def test_static_assets_are_copied(self):
        """COPY hpa_cellexp/ must bring static/ and reference/ with it."""
        self.assertIn("COPY hpa_cellexp/ ./hpa_cellexp/", self.text)
        for needed in ("static/index.html", "reference/labels_ja.tsv", "schema.sql"):
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, "hpa_cellexp", needed)),
                "{} must exist for the image to be usable".format(needed),
            )

    def test_dockerignore_excludes_data_but_keeps_reference_tables(self):
        ignore = read(".dockerignore")
        self.assertIn("data/", ignore)
        self.assertIn("*.sqlite", ignore)
        self.assertIn("!hpa_cellexp/reference/*.tsv", ignore)


@unittest.skipIf(yaml is None, "pyyaml is not installed")
class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = yaml.safe_load(read("docker-compose.yml"))
        cls.services = cls.spec["services"]

    def test_web_is_always_on(self):
        web = self.services["web"]
        self.assertEqual(web["restart"], "unless-stopped")
        self.assertIn("healthcheck", web)
        self.assertTrue(any("8000" in str(p) for p in web["ports"]))

    def test_host_port_is_configurable_and_defaults_to_8000(self):
        published = self.services["web"]["ports"][0]
        self.assertEqual(published, "${HPA_CELLEXP_PORT:-8000}:8000")

    def test_container_port_is_pinned_to_the_mapping(self):
        """HPA_CELLEXP_PORT in .env changes the HOST port only.

        If it were also allowed to reach the container, the app would move off
        8000 while the ports mapping still pointed there, and nothing would
        answer.  The compose file pins the inside to 8000 explicitly.
        """
        self.assertEqual(self.services["web"]["environment"]["HPA_CELLEXP_PORT"], "8000")
        self.assertTrue(self.services["web"]["ports"][0].endswith(":8000"))

    def test_env_example_documents_the_port(self):
        example = read(".env.example")
        self.assertIn("HPA_CELLEXP_PORT=8000", example)
        self.assertIn("HPA_SOURCE_DIR", example)
        self.assertIn("HPA_CELLEXP_WORKERS", example)

    def test_web_logs_are_bounded(self):
        """An always-on container must not fill the disk with logs."""
        options = self.services["web"]["logging"]["options"]
        self.assertIn("max-size", options)
        self.assertIn("max-file", options)

    def test_every_service_shares_one_database_path(self):
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertEqual(
                    service["environment"]["HPA_CELLEXP_DB"], "/data/hpa_cellexp.sqlite"
                )
                self.assertIn("cellexp-data:/data", service["volumes"])

    def test_one_shot_services_keep_their_subcommand(self):
        """Regression: `docker compose run build --expression X` REPLACES
        `command`, so a subcommand placed there is dropped the moment any
        argument is passed.  It has to be part of `entrypoint`."""
        for name in ("build", "demo", "organs", "inspect"):
            with self.subTest(service=name):
                service = self.services[name]
                self.assertNotIn(
                    "command", service,
                    "{}: put the subcommand in entrypoint, not command".format(name),
                )
                self.assertEqual(
                    service["entrypoint"], ["python", "-m", "hpa_cellexp", name]
                )
                self.assertEqual(service["profiles"], ["tools"])

    def test_one_shot_services_are_not_started_by_up(self):
        """`docker compose up` must bring up the site only."""
        default = [n for n, s in self.services.items() if not s.get("profiles")]
        self.assertEqual(default, ["web"])

    def test_source_files_are_mounted_read_only(self):
        for name, service in self.services.items():
            source = [v for v in service["volumes"] if v.endswith(":/source:ro")]
            self.assertEqual(len(source), 1, "{}: /source must be read-only".format(name))

    def test_subcommands_referenced_by_compose_exist(self):
        """Guards against a compose file that names a CLI command we removed."""
        from hpa_cellexp.__main__ import main

        for name in ("build", "demo", "organs", "inspect", "serve"):
            with self.subTest(command=name):
                with self.assertRaises(SystemExit) as ctx:
                    main([name, "--help"])
                self.assertEqual(ctx.exception.code, 0)


class ServeOptionsTests(unittest.TestCase):
    """The image drives serve entirely through environment variables."""

    def test_database_path_comes_from_the_environment(self):
        import importlib

        original = os.environ.get("HPA_CELLEXP_DB")
        os.environ["HPA_CELLEXP_DB"] = "/data/hpa_cellexp.sqlite"
        try:
            from hpa_cellexp import config

            importlib.reload(config)
            self.assertEqual(config.DEFAULT_DB_PATH, "/data/hpa_cellexp.sqlite")
        finally:
            if original is None:
                del os.environ["HPA_CELLEXP_DB"]
            else:
                os.environ["HPA_CELLEXP_DB"] = original
            from hpa_cellexp import config as config_again

            importlib.reload(config_again)

    def test_worker_count_comes_from_the_environment(self):
        import importlib

        original = os.environ.get("HPA_CELLEXP_WORKERS")
        os.environ["HPA_CELLEXP_WORKERS"] = "4"
        try:
            from hpa_cellexp import __main__ as cli

            importlib.reload(cli)
            self.assertEqual(cli.build_parser().parse_args(["serve"]).workers, 4)
        finally:
            if original is None:
                del os.environ["HPA_CELLEXP_WORKERS"]
            else:
                os.environ["HPA_CELLEXP_WORKERS"] = original
            from hpa_cellexp import __main__ as cli_again

            importlib.reload(cli_again)

    def test_port_and_host_come_from_the_environment(self):
        import importlib

        saved = {k: os.environ.get(k) for k in ("HPA_CELLEXP_HOST", "HPA_CELLEXP_PORT")}
        os.environ["HPA_CELLEXP_HOST"] = "0.0.0.0"
        os.environ["HPA_CELLEXP_PORT"] = "9000"
        try:
            from hpa_cellexp import __main__ as cli

            importlib.reload(cli)
            args = cli.build_parser().parse_args(["serve"])
            self.assertEqual(args.host, "0.0.0.0")
            self.assertEqual(args.port, 9000)
            # An explicit flag still wins over the environment.
            self.assertEqual(cli.build_parser().parse_args(["serve", "--port", "1234"]).port, 1234)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            from hpa_cellexp import __main__ as cli_again

            importlib.reload(cli_again)

    def test_port_defaults_to_8000_on_localhost(self):
        import importlib

        saved = {k: os.environ.pop(k, None) for k in ("HPA_CELLEXP_HOST", "HPA_CELLEXP_PORT")}
        try:
            from hpa_cellexp import __main__ as cli

            importlib.reload(cli)
            args = cli.build_parser().parse_args(["serve"])
            self.assertEqual((args.host, args.port), ("127.0.0.1", 8000))
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_worker_count_defaults_to_one(self):
        import importlib

        original = os.environ.pop("HPA_CELLEXP_WORKERS", None)
        try:
            from hpa_cellexp import __main__ as cli

            importlib.reload(cli)
            self.assertEqual(cli.build_parser().parse_args(["serve"]).workers, 1)
        finally:
            if original is not None:
                os.environ["HPA_CELLEXP_WORKERS"] = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
