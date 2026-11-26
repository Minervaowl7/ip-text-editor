from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_skill import build_docx

SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "check_ip_jargon.py"


class JargonScannerTests(unittest.TestCase):
    def scan(self, *args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCANNER), *map(str, args)],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def test_markdown_distinguishes_metaphor_from_physical_part(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.md"
            path.write_text(
                "预对准底座与基台连接。\n评估有效底座和前向被引。\n\n"
                "| 角色／取证 | 说明 |\n| --- | --- |\n| 外围／细化／低 | 待核 |\n",
                encoding="utf-8",
            )
            original = path.read_bytes()
            result = self.scan("--strict", path)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("有效底座", result.stdout)
            self.assertIn("前向被引", result.stdout)
            self.assertIn("L4: 压缩表头", result.stdout)
            self.assertNotIn("预对准底座", result.stdout)
            self.assertEqual(path.read_bytes(), original)

    def test_docx_ignores_deleted_text_and_reports_visible_table_header(self):
        body = (
            '<w:p><w:del><w:r><w:delText>权利底座</w:delText></w:r></w:del>'
            '<w:r><w:t>预对准底座</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>角色／取证</w:t>'
            '</w:r></w:p></w:tc></w:tr></w:tbl>'
        )
        with tempfile.TemporaryDirectory() as temp:
            path = build_docx(Path(temp), body_xml=body)
            result = self.scan(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("T#1 R#1: 压缩表头", result.stdout)
            self.assertNotIn("权利底座", result.stdout)
            clean = build_docx(Path(temp), filename="clean.docx")
            self.assertEqual(self.scan("--strict", clean).returncode, 0)
            self.assertEqual(self.scan(path.with_name("missing.docx")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
