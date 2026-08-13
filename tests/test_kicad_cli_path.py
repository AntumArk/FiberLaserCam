from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from offline_export import _find_kicad_cli, normalize_cli_path


class TestNormalizeCliPath(unittest.TestCase):
    def test_strips_trailing_windows_path_separator(self):
        self.assertEqual(
            normalize_cli_path(r"D:\somdisk\Programos\Kicad\bin\kicad-cli.exe;"),
            r"D:\somdisk\Programos\Kicad\bin\kicad-cli.exe",
        )

    def test_preserves_normal_path(self):
        self.assertEqual(
            normalize_cli_path(r"C:\KiCad\bin\kicad-cli.exe"),
            r"C:\KiCad\bin\kicad-cli.exe",
        )


class TestFindKiCadCli(unittest.TestCase):
    def test_find_kicad_cli_normalizes_shutil_which_result(self):
        with patch(
            "offline_export.shutil.which",
            side_effect=[r"D:\somdisk\Programos\Kicad\bin\kicad-cli.exe;", None],
        ):
            self.assertEqual(
                _find_kicad_cli(),
                r"D:\somdisk\Programos\Kicad\bin\kicad-cli.exe",
            )


if __name__ == "__main__":
    unittest.main()
