"""Tarea 11: tests baratos del parser del CLI.

No ejecutamos los `func` (que tocarían discovery/DB/uvicorn): solo
validamos el parseo. Los IsolatedTestCase nos dan sandbox limpio para
que `import cli` no contamine ~/.rastrillo del usuario real.
"""
from __future__ import annotations
from pathlib import Path

from .helpers import IsolatedTestCase


class TestCLIParser(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        # `cli` vive en la raíz del repo, no en el paquete; lo importamos
        # tras el sandbox para que su `from rastrillo import config` lea
        # las envs del IsolatedTestCase.
        repo_root = Path(__file__).resolve().parent.parent
        import sys
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        # Purgamos cli del caché por si quedó de un test anterior.
        sys.modules.pop("cli", None)
        import cli  # noqa: F401
        self.cli = cli

    def test_subcomandos_disponibles(self):
        """Los 4 subcomandos del plan están registrados."""
        p = self.cli.build_parser()
        # `add_subparsers(dest='cmd')` añade un único action al final.
        sub_actions = [a for a in p._actions
                       if a.__class__.__name__ == "_SubParsersAction"]
        self.assertEqual(len(sub_actions), 1)
        choices = set(sub_actions[0].choices.keys())
        self.assertEqual(choices, {"scan", "list", "report", "run"})

    def test_report_parsea_format_y_out(self):
        p = self.cli.build_parser()
        args = p.parse_args(["report", "--format", "json", "--out", "x.json"])
        self.assertEqual(args.cmd, "report")
        self.assertEqual(args.format, "json")
        self.assertEqual(args.out, "x.json")
        self.assertIs(args.func, self.cli.cmd_report)

    def test_report_format_invalido_falla(self):
        """argparse rechaza formatos fuera de json/csv/pdf con SystemExit."""
        p = self.cli.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["report", "--format", "yaml", "--out", "x"])

    def test_report_sin_out_falla(self):
        p = self.cli.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["report", "--format", "json"])

    def test_scan_acumula_listas(self):
        p = self.cli.build_parser()
        args = p.parse_args([
            "scan",
            "--user", "alice",
            "--user", "bob",
            "--email", "c@example.com",
        ])
        self.assertEqual(args.user, ["alice", "bob"])
        self.assertEqual(args.email, ["c@example.com"])
        self.assertIs(args.func, self.cli.cmd_scan)

    def test_list_con_status(self):
        p = self.cli.build_parser()
        args = p.parse_args(["list", "--status", "found"])
        self.assertEqual(args.cmd, "list")
        self.assertEqual(args.status, "found")
        self.assertIs(args.func, self.cli.cmd_list)

    def test_sin_subcomando_usa_default(self):
        """Sin args, `args.func` debe caer a cmd_default vía getattr."""
        p = self.cli.build_parser()
        args = p.parse_args([])
        # No hay `func` definida → main() usa getattr(args, 'func', cmd_default)
        self.assertFalse(hasattr(args, "func"))
        self.assertIsNone(args.cmd)


class TestReportEscribeFichero(IsolatedTestCase):
    """`rastrillo report --format json --out X` debe materializar el JSON.

    Es el smoke principal del subcomando `report`: comprueba que el
    contenido se serializa y queda en disco. Sin tocar discovery ni
    invocar la red.
    """

    def setUp(self):
        super().setUp()
        from rastrillo import db
        db.init()
        repo_root = Path(__file__).resolve().parent.parent
        import sys
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        sys.modules.pop("cli", None)
        import cli
        self.cli = cli

    def test_report_json_escribe_archivo(self):
        import json
        out = self.HOME / "informe.json"
        # Simulamos `args` mínimos (los que usa cmd_report).
        class _A: pass
        a = _A()
        a.format = "json"
        a.out = str(out)
        rc = self.cli.cmd_report(a)
        self.assertEqual(rc, 0)
        self.assertTrue(out.exists())
        data = json.loads(out.read_text(encoding="utf-8"))
        # Estructura esperada: clave 'summary' + 'accounts'.
        self.assertIn("summary", data)
        self.assertIn("accounts", data)
        self.assertEqual(data["accounts"], [])  # DB vacía en el sandbox
