import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape
import zipfile
import zlib

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_docx_metadata.py"

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def strip_markdown_code(text: str) -> str:
    kept = []
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is None:
            match = re.match(r"^(`{3,}|~{3,})", stripped)
            if match:
                fence = match.group(1)
                continue
            kept.append(re.sub(r"(`+).*?\1", "", line))
        elif re.match(rf"^{re.escape(fence[0])}{{{len(fence)},}}\s*$", stripped):
            fence = None
    return "\n".join(kept)


def heading_anchors(text: str) -> set[str]:
    anchors = set()
    for line in strip_markdown_code(text).splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", match.group(1))
        heading = heading.replace("`", "").replace("*", "").strip()
        normalized = re.sub(r"\s+", " ", heading).lower()
        slug = re.sub(r"[^\w\s-]", "", normalized).replace(" ", "-")
        anchors.add(normalized)
        anchors.add(slug)
    return anchors


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def build_png(text_payloads: list[bytes], *, chunk_type: bytes = b"tEXt") -> bytes:
    chunks = [
        b"\x89PNG\r\n\x1a\n",
        png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
        png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")),
    ]
    for payload in text_payloads:
        chunks.append(png_chunk(chunk_type, payload))
    chunks.append(png_chunk(b"IEND", b""))
    return b"".join(chunks)


def build_png_itxt(keyword: bytes, text: bytes, *, compressed: bool = False) -> bytes:
    flag = b"\x01" if compressed else b"\x00"
    body = zlib.compress(text) if compressed else text
    payload = keyword + b"\x00" + flag + b"\x00" + b"en\x00\x00" + body
    return build_png([payload], chunk_type=b"iTXt")


def build_jpeg(comment: bytes) -> bytes:
    return b"\xff\xd8\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment + b"\xff\xd9"


def build_jpeg_app1_xmp(xmp_xml: str, *, with_header: bool = True) -> bytes:
    header = b"http://ns.adobe.com/xap/1.0/\x00" if with_header else b""
    payload = header + xmp_xml.encode("utf-8")
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


def build_docx(
    directory: Path,
    body_xml: str = '<w:p><w:r><w:t>正常技术说明。</w:t></w:r></w:p>',
    creator: str = "审阅",
    application: str = "Microsoft Office Word",
    extra_files: dict[str, bytes | str] | None = None,
    title: str = "",
    subject: str = "",
    keywords: str = "",
    description: str = "",
    last_modified_by: str = "",
    filename: str = "sample.docx",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    docx_path = directory / filename
    core_fields = [f"<dc:creator>{creator}</dc:creator>"]
    for tag, value in (
        ("dc:title", title),
        ("dc:subject", subject),
        ("cp:keywords", keywords),
        ("dc:description", description),
        ("cp:lastModifiedBy", last_modified_by),
    ):
        if value:
            core_fields.append(f"<{tag}>{value}</{tag}>")
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{''.join(core_fields)}"
        "</cp:coreProperties>"
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>{application}</Application>"
        "</Properties>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(docx_path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", ROOT_RELS_XML)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("word/document.xml", document_xml)
        if extra_files:
            for member, content in extra_files.items():
                archive.writestr(member, content)
    return docx_path


def run_check_paths(paths: list[Path], *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT_PATH), *(str(path) for path in paths), *extra_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def run_check(docx_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return run_check_paths([docx_path], *extra_args)


class SkillStructureTests(unittest.TestCase):
    def test_frontmatter_and_agent_metadata(self) -> None:
        skill_text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", skill_text, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")

        frontmatter: dict[str, str] = {}
        current_key = None
        for line in match.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace() and ":" in line:
                key, raw_value = line.split(":", 1)
                current_key = key.strip()
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                frontmatter[current_key] = value
            elif current_key is not None and line[0].isspace():
                frontmatter[current_key] = f"{frontmatter[current_key]} {line.strip()}".strip()

        self.assertEqual(frontmatter.get("name"), "ip-text-editor")
        description = frontmatter.get("description", "")
        self.assertTrue(description.strip())
        with self.subTest(file="SKILL.md"):
            self.assertNotIn("怪话", description)

        openai_yaml = (REPO_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        with self.subTest(file="agents/openai.yaml"):
            self.assertNotIn("怪话", openai_yaml)

    def test_relative_markdown_links_and_anchors(self) -> None:
        md_files = [p for p in sorted(REPO_ROOT.rglob("*.md")) if ".git" not in p.parts]
        for md_file in md_files:
            cleaned = strip_markdown_code(md_file.read_text(encoding="utf-8"))
            for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", cleaned):
                raw_target = match.group(1).strip()
                if raw_target.startswith("<") and ">" in raw_target:
                    target = raw_target[1 : raw_target.index(">")]
                else:
                    target = raw_target.split()[0]
                if target.lower().startswith(("http://", "https://", "mailto:", "file://")):
                    continue

                path_part, _, anchor = target.partition("#")
                dest = (md_file.parent / path_part).resolve() if path_part else md_file.resolve()
                with self.subTest(source=md_file.relative_to(REPO_ROOT), target=target):
                    self.assertTrue(dest.is_relative_to(REPO_ROOT))
                    self.assertTrue(dest.is_file())
                    if anchor:
                        anchors = heading_anchors(dest.read_text(encoding="utf-8"))
                        self.assertIn(anchor.strip().lower(), anchors)

    def test_privacy_scan(self) -> None:
        win_users_pattern = re.compile(r"[A-Za-z]:[\\/]Users[\\/]")
        unix_home_pattern = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
        web_url_re = re.compile(r"https?://[^\s)>\"'`]+")
        all_repo_files = [
            p
            for p in sorted(REPO_ROOT.rglob("*"))
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
        ]
        for path in all_repo_files:
            content = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(file=path.relative_to(REPO_ROOT), check="windows-users-path"):
                self.assertEqual(win_users_pattern.findall(content), [])
            without_web_urls = web_url_re.sub("", content)
            with self.subTest(file=path.relative_to(REPO_ROOT), check="unix-home-path"):
                self.assertEqual(unix_home_pattern.findall(without_web_urls), [])

        raw_blocklist = os.environ.get("IP_TEXT_EDITOR_PRIVATE_TERMS", "")
        private_terms = [
            term.strip()
            for term in re.split(r"[\r\n,]+", raw_blocklist)
            if term.strip()
        ]
        if private_terms:
            for path in all_repo_files:
                lowered = path.read_text(encoding="utf-8", errors="replace").lower()
                for term in private_terms:
                    with self.subTest(file=path.relative_to(REPO_ROOT), term="configured-private-term"):
                        self.assertNotIn(term.lower(), lowered)

    def test_runtime_hygiene(self) -> None:
        targets = [REPO_ROOT / "SKILL.md", *sorted((REPO_ROOT / "references").rglob("*.md"))]
        for path in targets:
            content = path.read_text(encoding="utf-8")
            for forbidden in ("范例状态", "Vale", "考试大纲"):
                with self.subTest(file=path.relative_to(REPO_ROOT), term=forbidden):
                    self.assertNotIn(forbidden, content)

    def test_skill_md_non_table_line_length(self) -> None:
        lines = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if line.strip().startswith("|"):
                continue
            with self.subTest(lineno=lineno, length=len(line)):
                self.assertLessEqual(len(line), 280)

    def test_no_cjk_digit_pangu_spacing_in_runtime_markdown(self) -> None:
        patterns = (
            re.compile(r"[\u4e00-\u9fff] [0-9]|(?<![A-Za-z0-9])[0-9]+ [\u4e00-\u9fff]"),
            re.compile(
                r"去 AI 味|[ABC] 档|FTO 报告|FTO 稿|FTO 中|Word 改稿|Word 编辑|Word 修订|Word 稿|AI 味|AI 腔|AI 套|IPC 分类|IPC 号|IPC 层次"
            ),
        )
        targets = [REPO_ROOT / "SKILL.md", *sorted((REPO_ROOT / "references").rglob("*.md"))]
        for path in targets:
            cleaned = strip_markdown_code(path.read_text(encoding="utf-8"))
            for pattern in patterns:
                matches = pattern.findall(cleaned)
                with self.subTest(file=path.relative_to(REPO_ROOT), pattern=pattern.pattern):
                    self.assertEqual(matches, [])

    def test_no_prose_double_em_dash_in_runtime_markdown(self) -> None:
        negative_prefixes = ("- **改前**", "- **误改**", "- **AI腔原句**", "- **AI 腔原句**")
        targets = [REPO_ROOT / "SKILL.md", *sorted((REPO_ROOT / "references").rglob("*.md"))]
        for path in targets:
            fence = None
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.lstrip()
                if fence is None:
                    match = re.match(r"^(`{3,}|~{3,})", stripped)
                    if match:
                        fence = match.group(1)
                        continue
                elif re.match(rf"^{re.escape(fence[0])}{{{len(fence)},}}\s*$", stripped):
                    fence = None
                    continue
                else:
                    continue

                if stripped.startswith(negative_prefixes):
                    continue
                prose = re.sub(r"`[^`]+`", "", line)
                with self.subTest(file=path.relative_to(REPO_ROOT), lineno=lineno):
                    self.assertNotIn("——", prose)


class DocxMetadataScriptTests(unittest.TestCase):
    def test_clean_docx_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml="<w:p><w:r><w:t>该专利涉及 ChatGPT 对话模型的技术特征比对，以及使用O3处理晶圆表面。</w:t></w:r></w:p>",
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS:", result.stdout)

    def test_local_path_in_second_paragraph_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml=(
                    "<w:p><w:r><w:t>第一段正常说明。</w:t></w:r></w:p>"
                    r"<w:p><w:r><w:t>C:\work\draft.docx</w:t></w:r></w:p>"
                ),
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [p#2] (local-path)", result.stdout)
            combined = result.stdout + result.stderr
            self.assertNotIn(r"C:\work\draft.docx", combined)
            self.assertNotIn("draft.docx", combined)

    def test_xml_comment_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml="<!-- python-docx --><w:p><w:r><w:t>正文</w:t></w:r></w:p>",
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [xml-comment#1] (tool-identity)", result.stdout)

    def test_deleted_field_instruction_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml='<w:p><w:del w:id="1" w:author="审阅"><w:r><w:delInstrText>file:///C:/a</w:delInstrText></w:r></w:del></w:p>',
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [p#1] (local-path)", result.stdout)

    def test_expanded_tool_identities_in_properties(self) -> None:
        for tool in ("Kimi", "Cursor", "Qwen", "豆包"):
            with self.subTest(tool=tool, target="docProps/core.xml"), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), creator=tool)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1)
                self.assertRegex(
                    result.stdout,
                    r"(?m)^REVIEW docProps/core\.xml \[[^\]]+\] \(tool-identity\)$",
                )
            with self.subTest(tool=tool, target="docProps/app.xml"), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), application=tool)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1)
                self.assertRegex(
                    result.stdout,
                    r"(?m)^REVIEW docProps/app\.xml \[[^\]]+\] \(tool-identity\)$",
                )

    def test_claude_session_path_in_body_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml="<w:p><w:r><w:t>.claude/session-123</w:t></w:r></w:p>",
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [p#1] (local-path)", result.stdout)
            self.assertNotIn(".claude/session-123", result.stdout + result.stderr)

    def test_chinese_credit_in_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml="<w:p><w:r><w:t>经大模型润色完成</w:t></w:r></w:p>",
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [p#1] (tool-identity)", result.stdout)

    def test_contains_split_across_t_and_del_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml=(
                    '<w:p><w:r><w:t>内部</w:t></w:r>'
                    '<w:del w:id="1" w:author="审阅"><w:r><w:delText>代号X</w:delText></w:r></w:del></w:p>'
                ),
            )
            result = run_check(docx_path, "--contains", "内部代号X")
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/document.xml [p#1] (contains#1)", result.stdout)
            self.assertNotIn("内部代号X", result.stdout + result.stderr)

    def test_png_text_metadata_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={"word/media/sample.png": build_png([b"Software\x00ChatGPT"])},
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/media/sample.png [image-metadata] (tool-identity)", result.stdout)

    def test_png_text_metadata_local_path_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={"word/media/sample.png": build_png([b"Source\x00C:\\work\\chart.png"])},
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/media/sample.png [image-metadata] (local-path)", result.stdout)
            self.assertNotIn("chart.png", result.stdout + result.stderr)

    def test_jpeg_com_metadata_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={"word/media/photo.jpg": build_jpeg(b"Generated by Gemini")},
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("REVIEW word/media/photo.jpg [image-metadata] (tool-identity)", result.stdout)

    def test_ai_topic_in_core_properties_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                title="具身大模型产业知识产权研究简报",
                subject="OpenAI与DeepSeek专利布局对比",
                keywords="具身大模型, ChatGPT, 专利分析",
                description="围绕大语言模型技术路线分析",
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS:", result.stdout)

    def test_generic_ai_in_identity_and_doc_builder_in_description_flagged(self) -> None:
        cases = (
            ("creator", {"creator": "大模型"}, "creator"),
            ("doc-builder-description", {"description": "python-docx"}, "description"),
            ("credit-description", {"description": "由大模型生成"}, "description"),
        )
        for label, kwargs, expected_field in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), **kwargs)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    f"REVIEW docProps/core.xml [{expected_field}] (tool-identity)",
                    result.stdout,
                )

    def test_png_topic_title_passes_while_software_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={
                    "word/media/sample.png": build_png(
                        [b"Title\x00" + "具身大模型专利趋势".encode("utf-8")]
                    )
                },
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS:", result.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={"word/media/sample.png": build_png([b"Software\x00Cursor"])},
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("REVIEW word/media/sample.png [image-metadata] (tool-identity)", result.stdout)

    def test_custom_xml_identity_vs_topic_property(self) -> None:
        template = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            '<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="{name}">'
            "<vt:lpwstr>{value}</vt:lpwstr>"
            "</property>"
            "</Properties>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={
                    "docProps/custom.xml": template.format(name="Topic", value="具身大模型")
                },
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS:", result.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={
                    "docProps/custom.xml": template.format(name="GeneratedBy", value="Claude")
                },
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(
                "REVIEW docProps/custom.xml [custom property value] (tool-identity)",
                result.stdout,
            )

    def test_body_ai_technical_prose_passes(self) -> None:
        technical_sentences = (
            "通过大模型生成动作序列",
            "使用ChatGPT生成施工方案",
            "由人工智能完成任务",
            "通过大语言模型处理",
            "通过大语言模型输出",
            "通过AI辅助生成",
            "an image generated by AI",
            "作者：Wenxin Wang",
            "作者：Anthropic",
            "作者：AI产品经理入门教程",
            "作者：ChatGPT",
        )
        for text in technical_sentences:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(
                    Path(tmp),
                    body_xml=f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>",
                )
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS:", result.stdout)

    def test_body_real_provenance_flagged(self) -> None:
        provenance_sentences = (
            "本报告由ChatGPT生成",
            "经大模型润色完成",
            "This document was generated by Claude",
            "生成工具：DeepSeek",
            "generated by python-docx",
            "由python-docx生成",
            "经AI去异味",
        )
        for text in provenance_sentences:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(
                    Path(tmp),
                    body_xml=f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>",
                )
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REVIEW word/document.xml [p#1] (tool-identity)", result.stdout)

    def test_metadata_credit_author_prose_passes_while_tool_flagged(self) -> None:
        for text in ("作者：AI产品经理入门教程", "作者：人工智能导论", "作者：Wenxin Wang"):
            with self.subTest(expected="PASS", text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), description=text)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for text in ("作者：ChatGPT", "作者：AI", "generated by python-docx"):
            with self.subTest(expected="REVIEW", text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), description=text)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REVIEW docProps/core.xml [description] (tool-identity)", result.stdout)

    def test_false_positive_paths_latex_urls_ratios_and_bare_terms_pass(self) -> None:
        safe_texts = (
            r"f(t)=\\left\\langle x,y\\right\\rangle \\\\ \\cdots",
            r"\\gamma_{1}}(t)\\, \\&\\, \\cdots",
            r'\\"d_n\\, \\n\\',
            "session_id",
            "thread_id",
            "tool_call_id",
            "会话标识",
            "/users/login",
            "/private/key",
            "/home/index",
            "https://example.com/users/profile",
            "https://example.com/Users/profile",
            "C:/N",
            "A:/B",
            "profile:/token",
            "session_id = SHA-512(otp||id_user)",
            r"Y:\r\n",
        )
        for text in safe_texts:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(
                    Path(tmp),
                    body_xml=f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>",
                )
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS:", result.stdout)

    def test_real_local_paths_and_session_ids_flagged(self) -> None:
        user_home_doc = "/Users" + "/alice/draft.docx"
        linux_home_doc = "/home" + "/dev/work"
        flagged_texts = (
            r"\\fileserver\share\draft.docx",
            r"\\192.0.2.10\docs",
            r"\\file-srv\docs",
            "C:/work/draft.docx",
            "D:/report.docx",
            "session-6f3feecf-1f35",
            "session_id=abcdef1234",
            "会话ID：abc12345",
            user_home_doc,
            linux_home_doc,
            "/tmp/work/out.docx",
            "/private/tmp/a",
            "https://example.com/?next=file:///C:/a",
        )
        for text in flagged_texts:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(
                    Path(tmp),
                    body_xml=f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>",
                )
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REVIEW word/document.xml [p#1] (local-path)", result.stdout)
                self.assertNotIn(text, result.stdout + result.stderr)

    def test_human_names_and_ui_cursor_pass_while_real_cursor_flagged(self) -> None:
        for creator in ("Jean-Claude", "pre-cursor", "Wenxin Wang"):
            with self.subTest(creator=creator), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), creator=creator)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml=(
                    "<!-- cursor position -->"
                    "<w:p><w:r><w:drawing>"
                    '<wp:docPr xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                    'id="1" name="cursor" descr="cursor" title="cursor"/>'
                    '<pic:cNvPr xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
                    'id="2" name="cursor" descr="cursor" title="cursor"/>'
                    "</w:drawing></w:r></w:p>"
                ),
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for field_kwargs, expected_part in (
            ({"creator": "Cursor"}, "docProps/core.xml"),
            ({"last_modified_by": "Cursor"}, "docProps/core.xml"),
            ({"application": "Cursor"}, "docProps/app.xml"),
            ({"creator": "Wenxin Yiyan"}, "docProps/core.xml"),
            ({"creator": "文心一言"}, "docProps/core.xml"),
            ({"body_xml": '<w:p><w:ins w:id="1" w:author="Cursor"><w:r><w:t>文本</w:t></w:r></w:ins></w:p>'}, "word/document.xml"),
        ):
            with self.subTest(kwargs=field_kwargs), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), **field_kwargs)
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertRegex(
                    result.stdout,
                    rf"(?m)^REVIEW {re.escape(expected_part)} \[[^\]]+\] \(tool-identity\)$",
                )

        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml=(
                    "<!-- Cursor IDE -->"
                    "<w:p><w:r><w:drawing>"
                    '<wp:docPr xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                    'id="1" name="Cursor IDE"/>'
                    "</w:drawing></w:r></w:p>"
                ),
            )
            result = run_check(docx_path)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("REVIEW word/document.xml [xml-comment#1] (tool-identity)", result.stdout)
            self.assertIn("REVIEW word/document.xml [name] (tool-identity)", result.stdout)

    def test_jpeg_and_png_xmp_domain_separation_and_fallback(self) -> None:
        topic_xmp = (
            '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<rdf:Description rdf:about="">'
            '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">OpenAI与DeepSeek专利布局</rdf:li></rdf:Alt></dc:title>'
            '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">ChatGPT与Claude对比</rdf:li></rdf:Alt></dc:description>'
            "</rdf:Description>"
            "</rdf:RDF>"
            "</x:xmpmeta>"
            '<?xpacket end="w"?>'
        )
        for label, media_name, media_bytes in (
            ("jpeg-topic", "word/media/chart.jpg", build_jpeg_app1_xmp(topic_xmp)),
            ("png-topic", "word/media/chart.png", build_png_itxt(b"XML:com.adobe.xmp", topic_xmp.encode("utf-8"))),
            (
                "png-compressed-topic",
                "word/media/chart2.png",
                build_png_itxt(b"XML:com.adobe.xmp", topic_xmp.encode("utf-8"), compressed=True),
            ),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), extra_files={media_name: media_bytes})
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        provenance_xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<rdf:Description rdf:about="">'
            "<xmp:CreatorTool>ChatGPT</xmp:CreatorTool>"
            "<dc:title><rdf:Alt><rdf:li>OpenAI与DeepSeek专利布局</rdf:li></rdf:Alt></dc:title>"
            "</rdf:Description>"
            "</rdf:RDF>"
            "</x:xmpmeta>"
        )
        nested_creator_xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<rdf:Description rdf:about="" tiff:Software="Cursor">'
            "<dc:creator><rdf:Seq><rdf:li>Claude</rdf:li></rdf:Seq></dc:creator>"
            "</rdf:Description>"
            "</rdf:RDF>"
            "</x:xmpmeta>"
        )
        for label, media_name, media_bytes in (
            ("jpeg-provenance", "word/media/chart.jpg", build_jpeg_app1_xmp(provenance_xmp)),
            (
                "png-provenance",
                "word/media/chart.png",
                build_png_itxt(b"XML:com.adobe.xmp", provenance_xmp.encode("utf-8")),
            ),
            ("jpeg-nested-creator", "word/media/chart3.jpg", build_jpeg_app1_xmp(nested_creator_xmp)),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                docx_path = build_docx(Path(tmp), extra_files={media_name: media_bytes})
                result = run_check(docx_path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"REVIEW {media_name} [image-metadata] (tool-identity)", result.stdout)

        malformed_xmp = "<x:xmpmeta><dc:title>OpenAI与DeepSeek"
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                extra_files={
                    "word/media/broken.jpg": build_jpeg_app1_xmp(malformed_xmp),
                    "word/media/broken.png": build_png_itxt(b"XML:com.adobe.xmp", malformed_xmp.encode("utf-8")),
                },
            )
            result = run_check(docx_path)
            self.assertNotEqual(result.returncode, 2, result.stderr)

    def test_clean_draft_mode_vs_default_review_draft(self) -> None:
        comments_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:comment w:id="0" w:author="审阅"><w:p><w:r><w:t>请核对专利号。</w:t></w:r></w:p></w:comment>'
            "</w:comments>"
        )
        body_xml = (
            "<w:p>"
            '<w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>待确认技术特征</w:t></w:r>'
            '<w:ins w:id="1" w:author="审阅"><w:r><w:t>补充说明。</w:t></w:r></w:ins>'
            "</w:p>"
            "<w:tbl><w:tblPr>"
            '<w:tblPrChange w:id="2" w:author="审阅"><w:tblPr/></w:tblPrChange>'
            "</w:tblPr></w:tbl>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = build_docx(
                Path(tmp),
                body_xml=body_xml,
                extra_files={"word/comments.xml": comments_xml},
            )
            default_result = run_check(docx_path)
            self.assertEqual(default_result.returncode, 0, default_result.stdout + default_result.stderr)
            self.assertEqual(
                default_result.stdout,
                "PASS: no known provenance markers in scanned package fields.\n",
            )

            clean_result = run_check(docx_path, "--clean-draft")
            self.assertEqual(clean_result.returncode, 1, clean_result.stdout + clean_result.stderr)
            self.assertIn("REVIEW word/document.xml [p#1] (review-highlight)", clean_result.stdout)
            self.assertIn("REVIEW word/document.xml [p#1] (unfinished-revision)", clean_result.stdout)
            self.assertIn("REVIEW word/document.xml [tblPrChange] (unfinished-revision)", clean_result.stdout)
            self.assertIn("REVIEW word/comments.xml [comment#1] (leftover-comment)", clean_result.stdout)

    def test_multifile_directory_scan_and_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_doc = build_docx(root / "a", filename="clean.docx")
            dirty_doc = build_docx(
                root / "b" / "nested",
                body_xml=r"<w:p><w:r><w:t>C:\work\confidential_secret.docx</w:t></w:r></w:p>",
                filename="dirty.docx",
            )
            build_docx(
                root,
                body_xml="<w:p><w:r><w:t>ChatGPT</w:t></w:r></w:p>",
                creator="ChatGPT",
                filename="~$temp.docx",
            )

            dir_result = run_check(root)
            self.assertEqual(dir_result.returncode, 1, dir_result.stdout + dir_result.stderr)
            self.assertIn(f"{clean_doc}: PASS", dir_result.stdout)
            self.assertIn(f"{dirty_doc}: REVIEW word/document.xml [p#1] (local-path)", dir_result.stdout)
            self.assertNotIn("~$temp.docx", dir_result.stdout + dir_result.stderr)
            self.assertNotIn("confidential_secret", dir_result.stdout + dir_result.stderr)

            json_result = run_check(root, "--json")
            self.assertEqual(json_result.returncode, 1, json_result.stderr)
            payload = json.loads(json_result.stdout)
            self.assertEqual(payload["status"], "REVIEW")
            self.assertEqual([entry["path"] for entry in payload["files"]], [str(clean_doc), str(dirty_doc)])
            self.assertEqual(payload["files"][0]["status"], "PASS")
            self.assertEqual(payload["files"][0]["findings"], [])
            self.assertIsNone(payload["files"][0]["error"])
            self.assertEqual(payload["files"][1]["status"], "REVIEW")
            self.assertEqual(
                payload["files"][1]["findings"],
                [{"part": "word/document.xml", "location": "p#1", "category": "local-path"}],
            )
            self.assertNotIn("confidential_secret", json_result.stdout)

            all_pass_json = run_check(clean_doc, "--json")
            self.assertEqual(all_pass_json.returncode, 0, all_pass_json.stderr)
            self.assertEqual(json.loads(all_pass_json.stdout)["status"], "PASS")

            empty_dir = root / "empty_dir"
            empty_dir.mkdir()
            empty_json = run_check(empty_dir, "--json")
            self.assertEqual(empty_json.returncode, 2)
            self.assertEqual(json.loads(empty_json.stdout)["status"], "ERROR")

            missing_doc = root / "missing.docx"
            multi_error = run_check_paths([clean_doc, missing_doc], "--json")
            self.assertEqual(multi_error.returncode, 2)
            multi_payload = json.loads(multi_error.stdout)
            self.assertEqual(multi_payload["status"], "ERROR")
            self.assertEqual(multi_payload["files"][1]["status"], "ERROR")
            self.assertTrue(multi_payload["files"][1]["error"])


if __name__ == "__main__":
    unittest.main()
