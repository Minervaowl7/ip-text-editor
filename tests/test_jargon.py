from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape
import zipfile

from tests.test_skill import build_docx

SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "check_ip_jargon.py"
TRANSITIONAL_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_NAMESPACE = "http://purl.oclc.org/ooxml/wordprocessingml/main"
VML_NAMESPACE = "urn:schemas-microsoft-com:vml"
MC_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS_NAMESPACE = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"


def findings(stdout: str) -> list[str]:
    return [line.strip()[2:] for line in stdout.splitlines() if line.startswith("  - ")]


def protected(stdout: str) -> list[str]:
    return [line.strip()[2:] for line in stdout.splitlines() if line.startswith("  · ")]


def paragraph(text: str, properties: str = "") -> str:
    return f"<w:p>{properties}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def row(content: str, properties: str = "") -> str:
    return f"<w:tr>{properties}<w:tc>{content}</w:tc></w:tr>"


def table(*rows: str) -> str:
    return "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol/></w:tblGrid>" + "".join(rows) + "</w:tbl>"


def cell(content: str) -> str:
    return f"<w:tc>{content}</w:tc>"


def row_of(*cells: str, before: int = 0) -> str:
    properties = f'<w:trPr><w:gridBefore w:val="{before}"/></w:trPr>' if before else ""
    return f"<w:tr>{properties}{''.join(cells)}</w:tr>"


def grid_table(columns: int, *rows: str) -> str:
    return (
        "<w:tbl><w:tblPr/><w:tblGrid>"
        + "<w:gridCol/>" * columns
        + "</w:tblGrid>"
        + "".join(rows)
        + "</w:tbl>"
    )


def content_control(element: str, identifier: int) -> str:
    return (
        f'<w:sdt><w:sdtPr><w:id w:val="{identifier}"/></w:sdtPr>'
        f"<w:sdtContent>{element}</w:sdtContent></w:sdt>"
    )


def textbox_content(text: str) -> str:
    return (f'<w:pict xmlns:v="{VML_NAMESPACE}"><v:shape><v:textbox><w:txbxContent>'
            + paragraph(text) + '</w:txbxContent></v:textbox></v:shape></w:pict>')


def spanned_cell(content: str, span: int) -> str:
    return f'<w:tc><w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>{content}</w:tc>'


def build_docx_root(directory: Path, root_xml: str, filename: str = "sample.docx") -> Path:
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("word/document.xml", root_xml)
    return path


def build_docx_body(
    directory: Path,
    body_xml: str,
    *,
    namespace: str = TRANSITIONAL_NAMESPACE,
    filename: str = "sample.docx",
) -> Path:
    return build_docx_root(
        directory,
        f'<w:document xmlns:w="{namespace}"><w:body>{body_xml}</w:body></w:document>',
        filename,
    )


class JargonScannerTests(unittest.TestCase):
    def scan(self, *args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCANNER), *map(str, args)],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def temporary_directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def write_document(self, content: str, name: str = "sample.md") -> Path:
        path = self.temporary_directory() / name
        path.write_text(content, encoding="utf-8")
        return path

    def scan_text(self, content: str, name: str = "sample.md"):
        path = self.write_document(content, name)
        original = path.read_bytes()
        result = self.scan("--strict", path)
        self.assertEqual(path.read_bytes(), original, "the scanner must not modify its input")
        return result

    def test_markdown_distinguishes_metaphor_from_physical_part(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = build_docx(Path(temp), body_xml=body)
            result = self.scan(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("T#1 R#1: 压缩表头", result.stdout)
            self.assertNotIn("权利底座", result.stdout)
            clean = build_docx(Path(temp), filename="clean.docx")
            self.assertEqual(self.scan("--strict", clean).returncode, 0)
            self.assertEqual(self.scan(path.with_name("missing.docx")).returncode, 2)

    def test_markdown_fences_skip_nested_and_tilde_code_blocks(self):
        content = (
            "\ufeff````text\n普通字段示例\n```\n````\n评分画像需要改。\n\n"
            "~~~text\n字段：质量体检\n~~~\n"
        )
        result = self.scan_text(content)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L5: 评分画像", output)
        self.assertNotIn("质量体检", output)
        self.assertNotIn("普通字段示例", output)

    def test_markdown_table_variants_report_compressed_header(self):
        cases = {
            "optional_outer_pipes": "角色／取证 | 说明\n--- | ---\n外围 | 未定义\n",
            "spaced_slash": "| 角色 / 取证 | 说明 |\n| --- | --- |\n| 外围 | 未定义 |\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                result = self.scan_text(content, name=f"{label}.md")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("L1: 压缩表头", result.stdout)

    def test_quotation_and_original_field_values_are_not_rewrite_targets(self):
        content = (
            "> 对方文稿原文如下。权利底座。\n\n"
            "按原字段 `前向被引` 检索。\n\n"
            "| 项目 | 原字段值 |\n| --- | --- |\n| 标签 | 权利底座 |\n\n"
            "改稿候选：评分画像。\n"
        )
        result = self.scan_text(content)
        self.assertEqual(result.returncode, 1, result.stderr)
        action = "\n".join(findings(result.stdout))
        kept = "\n".join(protected(result.stdout))
        self.assertIn("L9: 评分画像", action)
        self.assertNotIn("权利底座", action)
        self.assertNotIn("前向被引", action)
        for location in ("L1", "L3", "L7"):
            self.assertIn(location, kept)
        self.assertIn("引文", kept)
        self.assertIn("原表值", kept)

    def test_quotation_frequency_is_excluded_from_body_density(self):
        result = self.scan_text("> 基于该案公开材料。\n" * 6 + "\n以上为引文。\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("全文密度", result.stdout)

    def test_evidence_direction_and_technical_terms(self):
        content = (
            "侵权取证难度较高，关键参数位于封闭腔体。\n"
            "侵权取证：高。\n"
            "螺栓穿过底座后与螺母连接。\n"
            "采用磷酸钙沉淀制备粉体。\n"
            "拆去保护盖后易观察到连接端。\n"
            "专利组合是公司业务的支撑底座。\n"
        )
        result = self.scan_text(content)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L2: 方向不明取证评级", output)
        self.assertIn("L6: 底座", output)
        for location in ("L1:", "L3:", "L4:", "L5:"):
            self.assertNotIn(location, output)

    def test_explicit_direction_and_legend_resolve_ratings(self):
        legend = (
            "| 评级 | 含义 |\n| --- | --- |\n| 高 | 较难 |\n| 中 | 中等 |\n| 低 | 较易 |\n\n"
        )
        content = legend + "表A：取证难度：高。\n\n表B：取证容易程度：高。\n\n统一写作取证：高。\n"
        result = self.scan_text(content)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("方向不明取证评级", result.stdout)

    def test_ipc_and_enumeration_cells_are_not_packed_columns(self):
        content = (
            "| 分组口径 | 样例 |\n| --- | --- |\n"
            "| H01L21/67／H01L21/68 | 有效／审中／终止 |\n"
            "| 外围／细化／低 | 待核 |\n"
            "| H/M/N/D | S/A/B |\n"
        )
        result = self.scan_text(content)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L4: 拼栏格值", output)
        for location in ("L3:", "L5:"):
            self.assertNotIn(location, output)

    def test_original_value_column_is_protected_while_unknown_table_is_marked(self):
        original = "| 项目 | 原字段值 |\n| --- | --- |\n| 标签 | 权利底座 |\n"
        unknown = "| 项目 | 说明 |\n| --- | --- |\n| 标签 | 权利底座 |\n"

        result = self.scan_text(original, name="original.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])
        self.assertIn("原表值", "\n".join(protected(result.stdout)))

        result = self.scan_text(unknown, name="unknown.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("来源需判定", "\n".join(findings(result.stdout)))

    def test_mixed_table_protects_original_column_only(self):
        content = (
            "| 原字段值 | 编辑说明 |\n| --- | --- |\n"
            "| 权利底座 | 权利底座应改为有效专利占比 |\n"
        )
        result = self.scan_text(content, name="mixed.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = findings(result.stdout)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("权利底座应改为", rows[0])
        self.assertIn("原表值", "\n".join(protected(result.stdout)))

    def test_repetition_reports_identical_rows_separately(self):
        first = "项目组应持续跟踪重点企业提出的申请并核对申请文本中的关键器件及其装配关系。"
        second = "保留历次会议讨论形成的原始记录和试验数据供后续专家检查相同型号的设备。"
        rows = "| 建议甲 | 建议乙 |\n| --- | --- |\n" + f"| {first} | {second} |\n" * 3
        result = self.scan_text(rows, name="rows.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        output = "\n".join(findings(result.stdout))
        self.assertIn("3x [L3, L4, L5]", output)
        self.assertIn(first.rstrip("。"), output)
        self.assertIn(second.rstrip("。"), output)
        self.assertNotIn("2x [", output)

    def test_repetition_reports_two_verbatim_paragraphs(self):
        text = "项目组应持续跟踪重点企业提出的申请并核对申请文本中的关键器件及其装配关系。"
        result = self.scan_text(f"{text}\n\n{text}\n", name="twice.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("2x [L1, L3]", result.stdout)
        self.assertIn("两处逐字相同", result.stdout)

    def test_shared_wording_keeps_different_companies_and_cells_separate(self):
        statement = "对公开登记资料进行核对后，将涉及的权利要求按设备部件和使用条件分别记录。"
        rows = "".join(f"| {name}：{statement} | 同一栏位 |\n" for name in ("甲公司", "乙公司", "丙公司"))
        result = self.scan_text("| 核查记录 | 备注 |\n| --- | --- |\n" + rows)
        self.assertEqual(result.returncode, 0, result.stdout)
        for name in ("甲公司", "乙公司", "丙公司"):
            self.assertIn(name, result.stdout)

    def test_rating_definition_cannot_move_across_columns_or_sections(self):
        cases = (
            "| 研发等级 | 研发含义 | 取证难易 |\n| --- | --- | --- |\n| 高 | 较难 | 高 |",
            "# 甲表\n本表取证等级定义为高＝较难。\n\n# 乙表\n侵权取证：高。",
        )
        for content in cases:
            with self.subTest(content=content):
                result = self.scan_text(content)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("方向不明取证评级", result.stdout)

    def test_repetition_keeps_distinct_facts_apart(self):
        paragraphs = [
            f"截至{year}年核对公开数据库登记的A-{model}型设备涉及的证据，专利申请为{amount}件"
            f"并且其中的活塞组件相关专利有效数量为{valid}件，仍处于实质审查程序中的另作记录。"
            for year, model, amount, valid in [(2023, 10, 3, 2), (2024, 20, 5, 4), (2025, 30, 9, 8)]
        ]
        result = self.scan_text("\n\n".join(paragraphs), name="facts.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])
        kept = "\n".join(protected(result.stdout))
        self.assertIn("相近句式", kept)
        for token in ("2023", "2024", "2025", "A-10", "A-20", "A-30", "不可并作删除重复"):
            self.assertIn(token, kept)

    def test_docx_strict_namespace_reports_visible_text(self):
        path = build_docx_body(
            self.temporary_directory(),
            paragraph("权利底座需要说明。"),
            namespace=STRICT_NAMESPACE,
        )
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("P#1: 权利底座", result.stdout)

    def test_docx_invalid_root_is_not_reported_as_clean(self):
        path = build_docx_root(
            self.temporary_directory(),
            f'<w:p xmlns:w="{TRANSITIONAL_NAMESPACE}"/>',
        )
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn("CJK=", result.stdout)

    def test_docx_hidden_text_soft_breaks_and_deleted_rows(self):
        hidden = '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>权利活度</w:t></w:r></w:p>'
        broken = '<w:p><w:r><w:t>权利</w:t><w:br/><w:t>底座</w:t></w:r></w:p>'
        quote = paragraph("报告原文写作质量体检。", '<w:pPr><w:pStyle w:val="Quote"/></w:pPr>')
        deleted_row = row(paragraph("评分画像"), '<w:trPr><w:del w:id="3" w:author="审阅"/></w:trPr>')
        body = hidden + broken + quote + table(
            row(paragraph("项目")), deleted_row, row(paragraph("配套专利"))
        )
        path = build_docx_body(self.temporary_directory(), body)
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("权利活度", result.stdout)
        self.assertNotIn("权利底座", result.stdout)
        self.assertNotIn("评分画像", result.stdout)
        self.assertIn("质量体检", "\n".join(protected(result.stdout)))

    def test_docx_multirow_headers_and_nested_cell_content(self):
        header = "<w:trPr><w:tblHeader/></w:trPr>"
        inner = table(row(paragraph("角色／取证")))
        cell_control = (
            "<w:sdt><w:sdtPr/><w:sdtContent>" + paragraph("前向被引") + "</w:sdtContent></w:sdt>"
        )
        body = table(
            row(paragraph("工作分工"), header),
            row(paragraph("角色／取证"), header),
            row(paragraph("配套专利")),
        ) + table(row(paragraph("说明") + inner + cell_control + paragraph("补充")))
        path = build_docx_body(self.temporary_directory(), body)
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = "\n".join(findings(result.stdout))
        self.assertIn("T#1 R#2: 压缩表头", output)
        self.assertIn("T#3 R#1: 压缩表头", output)
        self.assertIn("T#2 R#1: 前向被引", output)

    def test_unsupported_extension_is_reported_as_unchecked(self):
        path = self.write_document("品牌,型号\n", name="table.csv")
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("UNCHECKED", result.stderr)

    def test_corrupt_docx_is_reported_as_error(self):
        path = self.write_document("not a zip archive", name="broken.docx")
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR", result.stderr)

    def test_legends_only_cover_their_own_table_and_indicator(self):
        cross_table = (
            "表A　研发实施难度\n\n| 难度等级 | 含义 |\n| --- | --- |\n| 高 | 较难 |\n\n"
            "表B　外部取证（未附图例）\n\n| 案例 | 评分 |\n| --- | --- |\n"
            "| 电机方案 | 侵权取证：高 |\n"
        )
        quoted_legend = (
            "> 对方的取证难度等级定义：高＝较难。\n\n"
            "本报告采用独立评级，材料未附图例。\n\n侵权取证：高。\n"
        )
        for label, content, location in (
            ("cross-table legend", cross_table, "L11"),
            ("quoted legend", quoted_legend, "L5"),
            ("no legend at all", "侵权取证：高。\n", "L1"),
        ):
            with self.subTest(case=label):
                result = self.scan_text(content, name=f"{label}.md")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"{location}: 方向不明取证评级: 侵权取证：高", result.stdout)

        own_table_legend = (
            "| 案例 | 取证难易 | 含义 |\n| --- | --- | --- |\n| 电机方案 | 高 | 较难 |\n"
        )
        result = self.scan_text(own_table_legend, name="own_table_legend.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("方向不明取证评级", result.stdout)

    def test_rating_columns_report_the_direction_they_lack(self):
        ambiguous = (
            ("| 案例 | 取证难易 |\n| --- | --- |\n| 电机方案 | 高 |\n", "rating_table.md", "L3"),
            ("电机方案的取证难易程度：高。\n", "rating_inline.md", "L1"),
        )
        for content, name, location in ambiguous:
            with self.subTest(case=name):
                result = self.scan_text(content, name=name)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"{location}: 方向不明取证评级", result.stdout)

        directed = (
            "| 案例 | 取证难度 |\n| --- | --- |\n| 电机方案 | 高 |\n\n"
            "电机方案的侵权取证难度较高。\n"
        )
        result = self.scan_text(directed, name="rating_directed.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("方向不明取证评级", result.stdout)

    def test_repetition_keeps_leading_dates_and_qualifiers_apart(self):
        dates = [
            f"{year}年核对公开登记后，将所涉专利申请按申请人和技术主题分别统计，并在表后记录公开日期。"
            for year in (2023, 2024, 2025)
        ]
        result = self.scan_text("\n\n".join(dates), name="leading_dates.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])
        kept = "\n".join(protected(result.stdout))
        self.assertIn("事实不同", kept)
        for token in ("2023", "2024", "2025"):
            self.assertIn(token, kept)

        description = (
            "外壳连接件在压接工艺的全部工位均采用独立编号并在投产记录中保存检验时间和操作人信息。"
        )
        paragraphs = [
            prefix + description
            for prefix in ("已经核实", "尚未核实", "仅在样机条件下确认")
        ]
        result = self.scan_text("\n\n".join(paragraphs), name="leading_qualifiers.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])
        self.assertIn("事实不同", "\n".join(protected(result.stdout)))

    def test_two_places_with_different_sources_are_not_verbatim_repeats(self):
        opening = "项目组在检测报告中逐项记录样机的外壳卡扣数量、连接位置、检验时间和操作工位，见 "
        content = "\n\n".join(
            f"{opening}https://example.com/materials/{key}" for key in ("sample-a", "sample-b")
        )
        result = self.scan_text(content, name="distinct_sources.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("两处逐字相同", result.stdout)
        self.assertIn("来源不同", "\n".join(protected(result.stdout)))

    def test_blockquote_continuation_line_stays_a_quotation(self):
        content = (
            "> 引用合同条款时请保留其如下约定，\n"
            "权利底座的使用范围由双方在附件中确认。\n\n"
            "报告初稿使用了质量体检一词。\n"
        )
        result = self.scan_text(content, name="quote_continuation.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("L4: 质量体检", "\n".join(findings(result.stdout)))
        self.assertNotIn("权利底座", "\n".join(findings(result.stdout)))
        self.assertIn("L2: 权利底座", "\n".join(protected(result.stdout)))

    def test_soft_wrapped_inline_code_span_stays_protected(self):
        tick = "`"
        content = (
            f"字段示例：{tick}前缀\n"
            "权利底座\n"
            f"后缀{tick}。\n\n"
            f"统计口径按{tick}基于\n基于基于基于基于基于{tick}记录。\n\n"
            "报告初稿提到质量体检。\n"
        )
        result = self.scan_text(content, name="wrapped_code.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L8: 质量体检", output)
        self.assertNotIn("权利底座", output)
        self.assertIn("L1: 权利底座", "\n".join(protected(result.stdout)))
        self.assertNotIn("全文密度", result.stdout)

    def test_fence_inside_numbered_list_is_protected(self):
        fence = "```"
        content = (
            "1. 示例：\n"
            f"    {fence}text\n"
            "    权利底座\n"
            f"    {fence}\n"
            "\n"
            "报告初稿提到质量体检。\n"
        )
        result = self.scan_text(content, name="list_fence.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L6: 质量体检", output)
        self.assertNotIn("权利底座", output)

    def test_unclosed_list_fence_ends_at_the_list_boundary(self):
        for opener in ("1. 示例：\n    ~~~", "1) ~~~"):
            with self.subTest(opener=opener):
                result = self.scan_text(opener + "\n    权利底座\n\n质量体检。")
                self.assertIn("质量体检", "\n".join(findings(result.stdout)))
                self.assertNotIn("权利底座", "\n".join(findings(result.stdout)))

    def test_thematic_break_ends_lazy_blockquote_continuation(self):
        content = "> 对方引文。\n---\n权利底座\n"
        result = self.scan_text(content, name="quote_break.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("L3: 权利底座", "\n".join(findings(result.stdout)))
        self.assertNotIn("权利底座", "\n".join(protected(result.stdout)))

    def test_inline_code_in_cells_and_repeated_literals_stay_protected(self):
        tick = "`"
        result = self.scan_text(
            f"| 字段 | 校验指令示例 |\n| --- | --- |\n| 名称 | {tick}宽保护核心/标签组/输出字段{tick} |\n",
            name="code_cell.md",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])

        description = (
            "外壳连接件在压接工艺的全部工位均采用独立编号并在投产记录中保存检验时间和操作人信息。"
        )
        repeated = "\n\n".join(
            f"附录字段{ordinal}照录 {tick}{description}{tick}" for ordinal in ("甲", "乙", "丙")
        )
        result = self.scan_text(repeated, name="code_repeat.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])

    def test_fence_is_closed_only_by_an_unindented_marker(self):
        fence = "```"
        content = f"{fence}text\n    {fence}\n值示例：质量体检\n{fence}\n\n评分画像应核对。\n"
        result = self.scan_text(content, name="fence_indent.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L6: 评分画像", output)
        self.assertNotIn("质量体检", output)

    def test_docx_content_controls_around_rows_and_cells_keep_visible_text(self):
        header = row_of(cell(paragraph("项目")), cell(paragraph("说明")))
        wrapped_row = content_control(
            row_of(cell(paragraph("检索记录")), cell(paragraph("质量体检需要改。"))), 1
        )
        wrapped_cell = row_of(
            cell(paragraph("比对摘要")), content_control(cell(paragraph("权利底座需要改。")), 2)
        )
        path = build_docx_body(
            self.temporary_directory(),
            grid_table(2, header, wrapped_row, wrapped_cell),
        )
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 1, result.stdout)
        output = "\n".join(findings(result.stdout))
        self.assertIn("T#1 R#2: 质量体检", output)
        self.assertIn("T#1 R#3: 权利底座", output)

    def test_docx_quote_paragraph_inside_a_cell_is_protected(self):
        quotation = paragraph(
            "权利底座的使用范围以合同附件为准。", '<w:pPr><w:pStyle w:val="Quote"/></w:pPr>'
        )
        body = (
            quotation
            + grid_table(
                2,
                row_of(cell(paragraph("条款")), cell(paragraph("说明"))),
                row_of(cell(paragraph("条款原文")), cell(quotation)),
            )
            + paragraph("质量体检的名称需核实。")
        )
        path = build_docx_body(self.temporary_directory(), body)
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("T#1 R#2: 权利底座", "\n".join(protected(result.stdout)))
        self.assertNotIn("T#1 R#2: 权利底座", "\n".join(findings(result.stdout)))
        self.assertIn("P#2: 质量体检", "\n".join(findings(result.stdout)))

    def test_docx_grid_before_row_keeps_column_roles(self):
        body = grid_table(
            3,
            row_of(
                cell(paragraph("主题")),
                cell(paragraph("原字段值")),
                cell(paragraph("编辑建议")),
            ),
            row_of(cell(paragraph("权利底座")), cell(paragraph("质量体检")), before=1),
        )
        path = build_docx_body(self.temporary_directory(), body)
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 1, result.stdout)
        rows = findings(result.stdout)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("T#1 R#2: 质量体检", rows[0])
        self.assertIn("（第3列）", rows[0])
        kept = protected(result.stdout)
        self.assertTrue(
            any("T#1 R#2: 权利底座" in line and "（第2列）" in line for line in kept), kept
        )

    def test_docx_foreign_namespace_root_is_rejected(self):
        path = build_docx_body(
            self.temporary_directory(),
            paragraph("说明普通资料。"),
            namespace="urn:unrelated",
        )
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn("CJK=", result.stdout)

    def test_docx_text_box_paragraphs_are_scanned_and_hidden_ones_skipped(self):
        visible = "<w:r>" + textbox_content("权利底座") + "</w:r>"
        hidden = "<w:r><w:rPr><w:vanish/></w:rPr>" + textbox_content("质量体检") + "</w:r>"
        deleted = "<w:del><w:r>" + textbox_content("评分画像") + "</w:r></w:del>"
        doc = build_docx_body(self.temporary_directory(), f"<w:p>{visible}{hidden}{deleted}</w:p>")
        result = self.scan("--strict", doc)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("权利底座", "\n".join(findings(result.stdout)))
        self.assertNotIn("质量体检", result.stdout)
        self.assertNotIn("评分画像", result.stdout)

    def test_docx_alternate_content_text_box_counts_once(self):
        box = "<w:txbxContent>" + paragraph("质量体检") + "</w:txbxContent>"
        choice = f'<mc:Choice Requires="wps"><w:drawing><wps:txbx>{box}</wps:txbx></w:drawing></mc:Choice>'
        fallback = "<mc:Fallback>" + textbox_content("质量体检") + "</mc:Fallback>"
        body = (f'<w:p><w:r><mc:AlternateContent xmlns:mc="{MC_NAMESPACE}" xmlns:wps="{WPS_NAMESPACE}">'
                f'{choice}{fallback}</mc:AlternateContent></w:r></w:p>')
        result = self.scan("--strict", build_docx_body(self.temporary_directory(), body))
        self.assertEqual(len(findings(result.stdout)), 1, result.stdout)
        self.assertIn("质量体检", findings(result.stdout)[0])

    def test_docx_grid_span_keeps_later_columns_with_their_headers(self):
        body = grid_table(
            3,
            row_of(
                cell(paragraph("项目")),
                cell(paragraph("说明")),
                cell(paragraph("原字段值")),
            ),
            row_of(spanned_cell(paragraph("标签与范围"), 2), cell(paragraph("权利底座"))),
        )
        path = build_docx_body(self.temporary_directory(), body)
        result = self.scan("--strict", path)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(findings(result.stdout), [])
        kept = protected(result.stdout)
        self.assertTrue(
            any("T#1 R#2: 权利底座" in line and "（第3列）" in line for line in kept), kept
        )

    def test_docx_merged_header_protects_each_original_field_column(self):
        body = grid_table(3,
            row_of(spanned_cell(paragraph("原字段值"), 2), cell(paragraph("编辑建议"))),
            row_of(cell(paragraph("编号")), cell(paragraph("权利底座")), cell(paragraph("质量体检"))),
        )
        result = self.scan("--strict", build_docx_body(self.temporary_directory(), body))
        self.assertIn("权利底座", "\n".join(protected(result.stdout)))
        self.assertNotIn("权利底座", "\n".join(findings(result.stdout)))
        self.assertIn("质量体检", "\n".join(findings(result.stdout)))

    def test_docx_mixed_quote_cell_protects_only_the_quoted_paragraph(self):
        quote = paragraph("权利底座", '<w:pPr><w:pStyle w:val="Quote"/></w:pPr>')
        for comment, expected in [("此处为说明。", 0), ("此处使用质量体检的说法。", 1)]:
            with self.subTest(comment=comment):
                body = grid_table(1, row_of(cell(paragraph("说明"))), row_of(cell(quote + paragraph(comment))))
                result = self.scan("--strict", build_docx_body(self.temporary_directory(), body))
                self.assertEqual(result.returncode, expected, result.stdout)
                self.assertIn("权利底座", "\n".join(protected(result.stdout)))
                self.assertNotIn("权利底座", "\n".join(findings(result.stdout)))
                if expected:
                    self.assertIn("质量体检", "\n".join(findings(result.stdout)))

    def test_protected_markdown_header_and_multibacktick_field_value(self):
        for text in ("| `角色／取证` | 说明 |\n| --- | --- |\n| A | B |",
                     "原字段 ``前缀`权利底座`后缀``。"):
            with self.subTest(text=text):
                result = self.scan_text(text)
                self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.scan_text("``未配对，权利底座。").returncode, 1)

    def test_evidence_ratings_require_a_metric_and_a_rating_value(self):
        for text in ("在侵权取证中，应先核对产品型号和销售时间。", "取证高度依赖现场检查结果。",
                     "| 产品 | 侵权风险 |\n| --- | --- |\n| 方案甲 | 高 |"):
            with self.subTest(text=text):
                self.assertEqual(self.scan_text(text).returncode, 0, text)
        self.assertEqual(self.scan_text("侵权取证：中。").returncode, 1)

    def test_disabled_table_header_is_a_regular_data_row(self):
        data = row(paragraph("外围／细化／低"), '<w:trPr><w:tblHeader w:val="0"/></w:trPr>')
        body = table(row(paragraph("自编分析")), data)
        result = self.scan("--strict", build_docx_body(self.temporary_directory(), body))
        self.assertIn("拼栏格值", "\n".join(findings(result.stdout)))

    def test_soft_wraps_keep_sentence_and_repetition_checks(self):
        text = "项目组应持续跟踪重点企业提出的申请并核对申请文本中的关键器件及其装配关系。"
        wrapped = text.replace("申请并", "申请\n并")
        result = self.scan_text("\n\n".join((text, wrapped, wrapped)))
        self.assertIn("3x [L1, L3-L4, L6-L7]", result.stdout)
        long_sentence = "对申请文本中的关键器件及其装配关系进行核对" * 5
        result = self.scan_text(long_sentence[:58] + "\n" + long_sentence[58:] + "。")
        self.assertIn("长句", "\n".join(findings(result.stdout)))

    def test_markdown_indented_code_block_is_not_prose(self):
        content = (
            "字段示例：\n"
            "\n"
            "    权利底座\n"
            "\n"
            "- 清单第一项\n"
            "\n"
            "    列表续行提到权利底座。\n"
            "\n"
            "普通段落说明，\n"
            "    续行提到质量体检。\n"
        )
        result = self.scan_text(content, name="indented_code.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        output = "\n".join(findings(result.stdout))
        self.assertIn("L3: 权利底座", "\n".join(findings(self.scan_text(content, name="sample.txt").stdout)))
        self.assertNotIn("L3:", output)
        self.assertIn("L7: 权利底座", output)
        self.assertIn("L10: 质量体检", output)


if __name__ == "__main__":
    unittest.main()
