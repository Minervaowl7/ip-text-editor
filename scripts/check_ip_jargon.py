"""Read-only candidate scanner for IP jargon, translationese, and AI-style bloat.

Every hit is a review lead rather than a verdict. The report separates editable
prose from protected material: quotations, original table field values, and
inline field or code literals are listed as protected positions and never count
toward body word frequency, slash-column findings, or rewrite suggestions.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
URL_RE = re.compile(r"https?://\S+")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

CATEGORY_ORDER = (
    "伪专业隐喻",
    "英文直译腔",
    "空泛管理堆砌",
    "表头或格内压缩失义",
    "句式与高频套词",
    "跨段长句复读",
)

CATEGORY_ACTIONS = {
    "伪专业隐喻": "按分子分母改为实指名称；机械部件、原表字段值与引文照录",
    "英文直译腔": "改为中文实指名称，写明观察、拆解、测试或取证条件",
    "空泛管理堆砌": "落到实际动作、频率与责任主体；材料未给时不补",
    "表头或格内压缩失义": "拆列或注明原义；原表值照录，来源需判定时先向委托人确认",
    "句式与高频套词": "拆句或删套词，保留限定条件与统计口径",
    "跨段长句复读": "逐处核对差异后再合并；三处及以上才提示删除",
}

EXPLICIT_METAPHOR_RE = re.compile(
    r"有效底座|权利底座|保护底座|质量体检|综合体检|评分画像|质量画像|权利活度|待激活|"
    r"抓手|赋能|颗粒度|赛道|窗口管控|节点触发|性价比|留痕|长周期保护支撑"
)
BARE_BASE_RE = re.compile(r"底座")
MECHANICAL_BASE_CONTEXT_RE = re.compile(
    r"预对准|安装|固定|机台|设备|卡盘|吸盘|横梁|基台|与基台|上固定|传感器|"
    r"穿过|螺母|螺栓|螺纹|法兰|导轨|支架|夹具|焊接|装配|弹簧|垫片|轴孔|台面"
)
SEDIMENT_RE = re.compile(r"沉淀")
SEDIMENT_CONTEXT_RE = re.compile(
    r"化学|溶液|反应|析出|制备|粉体|沉淀法|沉淀剂|离心|过滤|结晶|离子|滴定|pH|钙|铁|铝|碳酸"
)

TRANSLATIONESE_RE = re.compile(
    r"前向被引|后向引|可逆向|易逆向|可取证性|易被外部观察|"
    r"权属映射|权利映射|成套映射|终端拆解"
)

BUREAUCRATIC_BLOAT_RE = re.compile(
    r"建立[^，。；\n]{0,6}(?:分层|常态化|长效|多维|立体|闭环)[^，。；\n]{0,6}机制|"
    r"分层日常监控|持续监控机制|常态化(?:管理|监控|跟踪)|持续机制|"
    r"系统归集|风险管控举措|按需推进|分步落实"
)

HEADER_PACKED_RE = re.compile(r"角色\s*[／/]\s*取证")
CELL_PACKED_RE = re.compile(r"宽保护核心|互补保护|外围\s*[／/]\s*细化")
SLASH_EXCLUDE_RE = re.compile(
    r"https?://\S+|总件数／有效／审中|S/A/B|GB/T|T/CIPS|Ⅰ／Ⅱ／Ⅲ|BOM/|"
    r"[A-H]\d{2}[A-Z]\d+(?:[／/]\d+)?|"
    r"\b[A-Z](?:[／/][A-Z])+\b|"
    r"有效[／/]审中[／/]终止|公开[／/]部分公开[／/]未公开|"
    r"\b\d+(?:[／/]\d+)+\b"
)
EVIDENCE_RATING_RE = re.compile(
    r"(?:侵权)?(?:可)?取证(?:性|难度|容易程度|易得性|难易程度)?\s*[：:]?\s*(较高|较低|中等|高|中|低)"
)
EVIDENCE_DIRECTION_RE = re.compile(r"难度|容易|易得|难易")
LEGEND_INLINE_RE = re.compile(r"(高|中|低)\s*[＝=:：]\s*(较难|较易|中等|难|易)")
LEGEND_MEANING_RE = re.compile(r"较难|较易|中等|难|易")
LEGEND_HEADER_RE = re.compile(r"图例|评级|含义|定义|等级|打分")

ORIGINAL_FIELD_RE = re.compile(r"原字段|原值|原始字段|原始值|原表|原文|字段原值|导出字段|原始记录")

PARALLEL_WALL_RULES = (
    ("方面连排", re.compile(r"方面，")),
    ("场景连排", re.compile(r"场景下，")),
    ("分级连排", re.compile(r"对[ⅠⅡⅢ一二三]级")),
)

CRUTCH_WORDS = (
    ("结合", 4.5, 5),
    ("维度", 3.5, 4),
    ("围绕", 3.5, 4),
    ("归集", 2.2, 3),
    ("台账", 2.2, 3),
    ("开展", 3.5, 4),
    ("基于", 3.5, 4),
)

MD_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
MD_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$")
MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
MD_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
MD_ITALIC_NOTE_RE = re.compile(r"^\s*\*[^*]+\*\s*$")

LONG_SENTENCE_LIMIT = 85
REPEAT_CJK_WINDOW = 28
REPEAT_MIN_LOCATIONS = 3
REPEAT_TWO_PLACE_LIMIT = 2

FACT_RE = re.compile(
    r"\d+(?:[.,]\d+)*%?|"
    r"[A-Za-z]+(?:[-/][A-Za-z0-9]+)*|"
    r"[零一二三四五六七八九十百千万亿两]+(?=[年月日件项次个台套家位条处人名份])|"
    r"尚未|未曾|已经|未|没有|无|非|不|仅|至少|全部|可能|拟|待"
)


class UnsupportedFormat(ValueError):
    """Raised when the path is not a file type this scanner can read."""


@dataclass(frozen=True)
class Segment:
    location: str
    kind: str  # prose | table_header | table_row | heading | blockquote | note
    text: str
    cells: tuple[str, ...] = ()
    table: str = ""
    editable: bool = True
    role: str = "正文"


@dataclass(frozen=True)
class Finding:
    category: str
    location: str
    snippet: str
    role: str = "正文"
    action: str = ""


@dataclass(frozen=True)
class Disclosure:
    location: str
    text: str
    role: str


@dataclass
class FileReport:
    path: Path
    total_cjk: int
    crutch_stats: list[tuple[str, int, float, bool]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    disclosures: list[Disclosure] = field(default_factory=list)
    body_cjk: int = 0


def _split_md_cells(line: str) -> tuple[str, ...]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", inner))


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and bool(MD_TABLE_SEP_RE.match(stripped))


def _fence_opener(line: str) -> tuple[str, int] | None:
    match = MD_FENCE_OPEN_RE.match(line)
    if not match:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        return None
    return fence[0], len(fence)


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    char, length = fence
    stripped = line.strip()
    return len(stripped) >= length and set(stripped) == {char}


def parse_markdown_or_text(path: Path) -> list[Segment]:
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not valid UTF-8 text: {exc}") from exc

    raw_lines = raw_text.splitlines()
    segments: list[Segment] = []
    fence: tuple[str, int] | None = None
    table_counter = 0
    table_id = ""
    in_table = False
    idx = 0

    while idx < len(raw_lines):
        raw = raw_lines[idx]
        line_no = idx + 1
        stripped = raw.strip()

        if fence is not None:
            if _closes_fence(raw, fence):
                fence = None
            in_table = False
            idx += 1
            continue

        opener = _fence_opener(raw)
        if opener is not None:
            fence = opener
            in_table = False
            idx += 1
            continue

        if not stripped:
            in_table = False
            idx += 1
            continue

        if "|" in stripped and idx + 1 < len(raw_lines) and _is_table_separator(raw_lines[idx + 1]):
            table_counter += 1
            table_id = f"T#{table_counter}"
            cells = _split_md_cells(stripped)
            segments.append(Segment(f"L{line_no}", "table_header", " | ".join(cells), cells, table_id))
            in_table = True
            idx += 2
            continue

        if in_table and "|" in stripped:
            cells = _split_md_cells(stripped)
            segments.append(Segment(f"L{line_no}", "table_row", " | ".join(cells), cells, table_id))
            idx += 1
            continue

        in_table = False
        table_id = ""
        if MD_HEADING_RE.match(stripped):
            segments.append(
                Segment(f"L{line_no}", "heading", MD_HEADING_RE.sub("", stripped), role="标题")
            )
        elif stripped.startswith(">"):
            segments.append(
                Segment(
                    f"L{line_no}",
                    "blockquote",
                    stripped.lstrip("> ").strip(),
                    editable=False,
                    role="引文",
                )
            )
        elif MD_ITALIC_NOTE_RE.match(stripped):
            segments.append(Segment(f"L{line_no}", "note", stripped.strip("* ").strip(), role="注释"))
        else:
            cleaned = MD_LIST_PREFIX_RE.sub("", stripped)
            segments.append(Segment(f"L{line_no}", "prose", cleaned))
        idx += 1

    return segments


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


class _DocxReader:
    """Collect the visible body in document order for one WordprocessingML namespace."""

    def __init__(self, namespace: str) -> None:
        self.ns = namespace
        self.segments: list[Segment] = []
        self._paragraph_index = 0
        self._table_index = 0

    def _tag(self, name: str) -> str:
        return f"{{{self.ns}}}{name}"

    def _child(self, element: ET.Element, name: str) -> ET.Element | None:
        for child in element:
            if _local_name(child.tag) == name and _namespace(child.tag) == self.ns:
                return child
        return None

    def _children(self, element: ET.Element, name: str) -> list[ET.Element]:
        return [
            child
            for child in element
            if _local_name(child.tag) == name and _namespace(child.tag) == self.ns
        ]

    def _hidden_run(self, run: ET.Element) -> bool:
        properties = self._child(run, "rPr")
        if properties is None:
            return False
        for name in ("vanish", "specVanish"):
            flag = self._child(properties, name)
            if flag is None:
                continue
            value = (flag.get(self._tag("val")) or "").lower()
            if value not in {"0", "false", "off"}:
                return True
        return False

    def _inline_text(self, element: ET.Element) -> str:
        parts: list[str] = []
        for child in element:
            local = _local_name(child.tag)
            if local in {"del", "moveFrom", "delText"}:
                continue
            if local == "r":
                if self._hidden_run(child):
                    continue
                for node in child:
                    node_local = _local_name(node.tag)
                    if node_local == "t" and node.text:
                        parts.append(node.text)
                    elif node_local in {"br", "cr"}:
                        parts.append("\n")
                    elif node_local == "tab":
                        parts.append(" ")
                    elif node_local == "noBreakHyphen":
                        parts.append("-")
                continue
            if local in {"pPr", "rPr", "delText"}:
                continue
            parts.append(self._inline_text(child))
        return "".join(parts)

    def _flatten(self, text: str) -> str:
        return "\n".join(part.strip() for part in text.split("\n") if part.strip())

    def _paragraph_style(self, paragraph: ET.Element) -> str:
        properties = self._child(paragraph, "pPr")
        if properties is None:
            return ""
        style = self._child(properties, "pStyle")
        if style is None:
            return ""
        return style.get(self._tag("val"), "")

    def add_paragraph(self, paragraph: ET.Element) -> None:
        self._paragraph_index += 1
        text = self._flatten(self._inline_text(paragraph))
        if not text:
            return
        style = self._paragraph_style(paragraph)
        if style.startswith("TOC"):
            return
        if style.startswith(("Heading", "Title", "Subtitle")):
            kind, role = "heading", "标题"
        elif style in {"Quote", "BlockText", "IntenseQuote"} or text.startswith(">"):
            kind, role = "blockquote", "引文"
        elif MD_ITALIC_NOTE_RE.match(text):
            kind, role = "note", "注释"
        else:
            kind, role = "prose", "正文"
        self.segments.append(
            Segment(f"P#{self._paragraph_index}", kind, text, editable=kind != "blockquote", role=role)
        )

    def _deleted_row(self, row: ET.Element) -> bool:
        properties = self._child(row, "trPr")
        if properties is None:
            return False
        return self._child(properties, "del") is not None

    def _header_row(self, row: ET.Element, index: int) -> bool:
        if index == 1:
            return True
        properties = self._child(row, "trPr")
        if properties is None:
            return False
        return self._child(properties, "tblHeader") is not None

    def _cell_text(self, container: ET.Element) -> str:
        parts: list[str] = []
        for child in container:
            local = _local_name(child.tag)
            if local == "p":
                text = self._flatten(self._inline_text(child))
                if text:
                    parts.append(text)
            elif local == "tbl":
                self.add_table(child)
            elif local in {"tcPr", "trPr", "tblPr", "tblGrid"}:
                continue
            else:
                nested = self._cell_text(child)
                if nested:
                    parts.append(nested)
        return " ".join(parts)

    def add_table(self, table: ET.Element) -> None:
        self._table_index += 1
        table_id = f"T#{self._table_index}"
        row_index = 0
        for row in self._children(table, "tr"):
            row_index += 1
            if self._deleted_row(row):
                continue
            cells = tuple(self._cell_text(cell) for cell in self._children(row, "tc"))
            if not any(cells):
                continue
            kind = "table_header" if self._header_row(row, row_index) else "table_row"
            self.segments.append(
                Segment(f"{table_id} R#{row_index}", kind, " | ".join(cells), cells, table_id)
            )

    def walk(self, container: ET.Element) -> None:
        for child in container:
            local = _local_name(child.tag)
            if local == "p":
                self.add_paragraph(child)
            elif local == "tbl":
                self.add_table(child)
            elif local in {"del", "moveFrom"}:
                continue
            else:
                self.walk(child)


def parse_docx(path: Path) -> list[Segment]:
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise ValueError("missing word/document.xml")
        payload = archive.read("word/document.xml")

    if not payload.strip():
        raise ValueError("empty word/document.xml")

    root = ET.fromstring(payload)
    root_name = _local_name(root.tag)
    if root_name != "document":
        raise ValueError(f"invalid main document root <{root_name}>; expected w:document")

    body = None
    for child in root:
        if _local_name(child.tag) == "body":
            body = child
            break
    if body is None:
        raise ValueError("missing w:body in word/document.xml")

    reader = _DocxReader(_namespace(root.tag))
    reader.walk(body)
    return reader.segments


def _clip(text: str, limit: int = 80) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else f"{clean[:limit]}..."


def _overlaps(span: tuple[int, int], existing: list[tuple[int, int]]) -> bool:
    return any(not (span[1] <= start or span[0] >= end) for start, end in existing)


def _metaphor_snippets(text: str) -> list[str]:
    snippets: list[str] = []
    matched_spans: list[tuple[int, int]] = []
    for match in EXPLICIT_METAPHOR_RE.finditer(text):
        matched_spans.append(match.span())
        snippets.append(f"{match.group(0)} ({_clip(text, 55)})")

    for match in SEDIMENT_RE.finditer(text):
        if _overlaps(match.span(), matched_spans):
            continue
        window = text[max(0, match.start() - 6) : match.end() + 6]
        if SEDIMENT_CONTEXT_RE.search(window):
            continue
        snippets.append(f"沉淀 ({_clip(window, 30)})")

    for match in BARE_BASE_RE.finditer(text):
        if _overlaps(match.span(), matched_spans):
            continue
        window = text[max(0, match.start() - 8) : match.end() + 4]
        if "\n" in window or MECHANICAL_BASE_CONTEXT_RE.search(window):
            continue
        snippets.append(f"底座 ({_clip(window, 30)})")

    return snippets


def _evidence_snippets(text: str, legend: dict[str, str]) -> list[str]:
    snippets: list[str] = []
    for match in EVIDENCE_RATING_RE.finditer(text):
        token = match.group(0)
        if EVIDENCE_DIRECTION_RE.search(token):
            continue
        rating = match.group(1)
        if legend:
            if rating in legend:
                continue
            snippets.append(f"方向不明取证评级: {token}（图例未覆盖该值，向委托人核对）")
            continue
        snippets.append(
            f"方向不明取证评级: {token}（写清是难度高=较难还是易得性高=较易；无图例时向委托人核对）"
        )
    return snippets


def _syntax_snippets(text: str) -> list[str]:
    snippets: list[str] = []
    for label, pattern in PARALLEL_WALL_RULES:
        count = len(pattern.findall(text))
        if count >= 3:
            snippets.append(f"{label}×{count}: {_clip(text, 60)}")

    without_urls = URL_RE.sub("", text)
    for sentence in re.split(r"[。！？]", without_urls):
        part = sentence.strip()
        if not part:
            continue
        cjk_count = len(CJK_RE.findall(part))
        if cjk_count > LONG_SENTENCE_LIMIT:
            snippets.append(f"长句({cjk_count}字): {_clip(part, 65)}")
    return snippets


def _category_hits(text: str, legend: dict[str, str], *, syntax: bool) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for snippet in _metaphor_snippets(text):
        hits.append(("伪专业隐喻", snippet))
    for match in TRANSLATIONESE_RE.finditer(text):
        hits.append(("英文直译腔", f"{match.group(0)} ({_clip(text, 55)})"))
    for match in BUREAUCRATIC_BLOAT_RE.finditer(text):
        hits.append(("空泛管理堆砌", f"{match.group(0)} ({_clip(text, 55)})"))
    for snippet in _evidence_snippets(text, legend):
        hits.append(("表头或格内压缩失义", snippet))
    if syntax:
        for snippet in _syntax_snippets(text):
            hits.append(("句式与高频套词", snippet))
    return hits


def _split_inline_code(text: str) -> tuple[str, list[str]]:
    literals: list[str] = []

    def replace(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        if value:
            literals.append(value)
        return " " * len(match.group(0))

    return INLINE_CODE_RE.sub(replace, text), literals


def _protection_reason(seg: Segment) -> str:
    return seg.role or "正文"


def _protected_columns(segments: list[Segment]) -> dict[str, set[int]]:
    protected: dict[str, set[int]] = defaultdict(set)
    for seg in segments:
        if seg.kind != "table_header" or not seg.table:
            continue
        for index, cell in enumerate(seg.cells):
            if ORIGINAL_FIELD_RE.search(cell):
                protected[seg.table].add(index)
    return protected


def _collect_legend(segments: list[Segment]) -> dict[str, str]:
    legend: dict[str, str] = {}
    legend_tables: set[str] = set()
    for seg in segments:
        for match in LEGEND_INLINE_RE.finditer(seg.text):
            legend.setdefault(match.group(1), match.group(2))
        if (
            seg.kind == "table_header"
            and seg.table
            and any(LEGEND_HEADER_RE.search(cell) for cell in seg.cells)
        ):
            legend_tables.add(seg.table)

    for seg in segments:
        if seg.kind not in {"table_header", "table_row"} or seg.table not in legend_tables:
            continue
        for index, cell in enumerate(seg.cells):
            key = cell.strip()
            if key not in {"高", "中", "低"}:
                continue
            for other_index, other in enumerate(seg.cells):
                if other_index == index:
                    continue
                meaning = LEGEND_MEANING_RE.search(other)
                if meaning:
                    legend.setdefault(key, meaning.group(0))
    return legend


def _scan_segment(
    seg: Segment,
    *,
    editable: bool,
    role: str,
    legend: dict[str, str],
    syntax: bool,
    column: int = 0,
    findings: list[Finding],
    disclose,
) -> None:
    suffix = f"（第{column}列）" if column else ""
    masked, literals = _split_inline_code(seg.text)
    for category, snippet in _category_hits(masked, legend, syntax=syntax):
        if editable:
            findings.append(
                Finding(category, seg.location, f"{snippet}{suffix}", role, CATEGORY_ACTIONS[category])
            )
        else:
            disclose(seg.location, f"{snippet}{suffix}", role)

    for literal in literals:
        for category, snippet in _category_hits(literal, legend, syntax=False):
            disclose(seg.location, f"{snippet}{suffix}", "原字段值（行内代码）")


def _location_key(location: str):
    parts = re.split(r"(\d+)", location)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts if part)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _fact_signature(text: str) -> tuple[str, ...]:
    return tuple(FACT_RE.findall(text))


def _fact_preview(text: str, limit: int = 42) -> str:
    facts = FACT_RE.findall(text)
    if not facts:
        return "无数字或型号差异"
    return _clip("/".join(facts), limit)


def _check_repetitions(
    segments: list[Segment],
    protected: dict[str, set[int]],
    findings: list[Finding],
    disclose,
) -> None:
    units: list[tuple[str, str]] = []
    for seg in segments:
        if seg.kind in ("blockquote", "table_header"):
            continue
        if seg.kind == "table_row":
            blocked = protected.get(seg.table, set())
            for index, cell in enumerate(seg.cells):
                if cell and index not in blocked:
                    units.append((seg.location, cell))
        elif seg.editable:
            units.append((seg.location, seg.text))

    window_locations: dict[str, set[str]] = defaultdict(set)
    unit_data: list[tuple[str, str, str, list[int]]] = []

    for location, raw in units:
        cleaned = URL_RE.sub("", raw)
        cjk_chars: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(cleaned):
            if CJK_RE.match(char):
                cjk_chars.append(char)
                positions.append(index)
        cjk_text = "".join(cjk_chars)
        if len(cjk_text) < REPEAT_CJK_WINDOW:
            continue
        unit_data.append((location, cleaned, cjk_text, positions))
        seen_local: set[str] = set()
        for start in range(len(cjk_text) - REPEAT_CJK_WINDOW + 1):
            window = cjk_text[start : start + REPEAT_CJK_WINDOW]
            if window not in seen_local:
                seen_local.add(window)
                window_locations[window].add(location)

    repeated = {
        window: frozenset(locations)
        for window, locations in window_locations.items()
        if len(locations) >= REPEAT_MIN_LOCATIONS
    }

    groups: dict[tuple[frozenset[str], str], dict[str, str]] = {}
    if repeated:
        for location, cleaned, cjk_text, positions in unit_data:
            index = 0
            length = len(cjk_text)
            while index <= length - REPEAT_CJK_WINDOW:
                locations = repeated.get(cjk_text[index : index + REPEAT_CJK_WINDOW])
                if locations is None:
                    index += 1
                    continue
                end = index + 1
                while (
                    end <= length - REPEAT_CJK_WINDOW
                    and repeated.get(cjk_text[end : end + REPEAT_CJK_WINDOW]) == locations
                ):
                    end += 1
                end_cjk = end - 1 + REPEAT_CJK_WINDOW
                slice_text = cleaned[positions[index] : positions[end_cjk - 1] + 1]
                key = (locations, FACT_RE.sub("·", slice_text))
                bucket = groups.setdefault(key, {})
                if location not in bucket or len(slice_text) > len(bucket[location]):
                    bucket[location] = slice_text
                index = end

    verbatim: dict[str, set[str]] = defaultdict(set)
    for location, cleaned, _cjk_text, _positions in unit_data:
        verbatim[_normalize(cleaned)].add(location)

    reported: set[tuple[str, str]] = set()
    for (locations, _mask), bucket in groups.items():
        ordered = sorted(locations, key=_location_key)
        preview = ", ".join(ordered[:5])
        if len(ordered) > 5:
            preview += f" 等{len(ordered)}处"
        slices = [bucket.get(location, "") for location in ordered]
        signatures = [_fact_signature(text) for text in slices]
        if len(set(signatures)) == 1:
            key = (frozenset(ordered), _normalize(slices[0]))
            if key in reported:
                continue
            reported.add(key)
            findings.append(
                Finding(
                    "跨段长句复读",
                    f"{len(ordered)}x [{preview}]",
                    _clip(slices[0], 65),
                    "跨段",
                    CATEGORY_ACTIONS["跨段长句复读"],
                )
            )
            continue

        differences = " ｜ ".join(
            f"{location}: {_fact_preview(text)}" for location, text in zip(ordered, slices)
        )
        disclose(
            f"{len(ordered)}x 相近句式 [{preview}]",
            f"事实不同，不可并作删除重复 ｜ {differences}",
            "相近句式",
        )

    for text, locations in verbatim.items():
        if len(locations) != REPEAT_TWO_PLACE_LIMIT:
            continue
        ordered = sorted(locations, key=_location_key)
        key = (frozenset(ordered), text)
        if key in reported:
            continue
        reported.add(key)
        findings.append(
            Finding(
                "跨段长句复读",
                f"{REPEAT_TWO_PLACE_LIMIT}x [{', '.join(ordered)}]",
                f"{_clip(text, 65)}（两处逐字相同）",
                "跨段",
                "两处逐字相同，先确认用途再决定是否合并",
            )
        )


def analyze_file(path: Path) -> FileReport:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        segments = parse_docx(path)
    elif suffix in {".md", ".markdown", ".txt"}:
        segments = parse_markdown_or_text(path)
    else:
        raise UnsupportedFormat(f"unsupported file extension: {suffix or '(none)'}")

    findings: list[Finding] = []
    disclosures: list[Disclosure] = []
    seen: set[tuple[str, str, str]] = set()

    def disclose(location: str, text: str, role: str) -> None:
        key = (location, text, role)
        if key in seen:
            return
        seen.add(key)
        disclosures.append(Disclosure(location, text, role))

    protected = _protected_columns(segments)
    legend = _collect_legend(segments)

    for seg in segments:
        if seg.kind in {"table_header", "table_row"}:
            blocked = protected.get(seg.table, set())
            for index, cell in enumerate(seg.cells):
                if not cell:
                    continue
                is_protected = seg.kind == "table_row" and index in blocked
                role = (
                    "表头"
                    if seg.kind == "table_header"
                    else ("原表值" if is_protected else "表值·来源需判定")
                )
                cell_segment = Segment(seg.location, seg.kind, cell, table=seg.table)
                column = index + 1 if seg.kind == "table_row" else 0
                _scan_segment(
                    cell_segment,
                    editable=not is_protected,
                    role=role,
                    legend=legend,
                    syntax=False,
                    column=column,
                    findings=findings,
                    disclose=disclose,
                )
                if seg.kind == "table_header":
                    if HEADER_PACKED_RE.search(cell):
                        findings.append(
                            Finding(
                                "表头或格内压缩失义",
                                seg.location,
                                f"压缩表头: {_clip(cell, 65)}",
                                role,
                                CATEGORY_ACTIONS["表头或格内压缩失义"],
                            )
                        )
                elif not is_protected:
                    if CELL_PACKED_RE.search(cell):
                        findings.append(
                            Finding(
                                "表头或格内压缩失义",
                                seg.location,
                                f"拼栏格值: {_clip(cell, 55)}（第{column}列）",
                                role,
                                CATEGORY_ACTIONS["表头或格内压缩失义"],
                            )
                        )
                    cleaned = SLASH_EXCLUDE_RE.sub("", cell)
                    if len(re.findall(r"[／/]", cleaned)) >= 2:
                        findings.append(
                            Finding(
                                "表头或格内压缩失义",
                                seg.location,
                                f"多斜杠拼栏: {_clip(cell, 55)}（第{column}列）",
                                role,
                                CATEGORY_ACTIONS["表头或格内压缩失义"],
                            )
                        )
            continue

        _scan_segment(
            seg,
            editable=seg.editable,
            role=_protection_reason(seg),
            legend=legend,
            syntax=seg.kind == "prose",
            findings=findings,
            disclose=disclose,
        )

    _check_repetitions(segments, protected, findings, disclose)

    total_cjk = len(CJK_RE.findall("\n".join(seg.text for seg in segments)))
    body_segments = [
        seg for seg in segments if seg.kind in {"prose", "heading"} and seg.editable
    ]
    body_text = "\n".join(_split_inline_code(seg.text)[0] for seg in body_segments)
    body_cjk = len(CJK_RE.findall(body_text))

    crutch_stats: list[tuple[str, int, float, bool]] = []
    for word, max_per_10k, min_abs in CRUTCH_WORDS:
        count = len(re.findall(word, body_text))
        per_10k = (count * 10000.0 / body_cjk) if body_cjk else 0.0
        exceeded = per_10k > max_per_10k and count > min_abs
        crutch_stats.append((word, count, per_10k, exceeded))
        if exceeded:
            findings.append(
                Finding(
                    "句式与高频套词",
                    "全文密度",
                    f"高频套词「{word}」{count}次（{per_10k:.2f}/万字，阈值>{max_per_10k}/万字且>{min_abs}次）",
                    "正文",
                    "减少同词堆砌；引文与原表值不参与该统计",
                )
            )

    return FileReport(
        path=path,
        total_cjk=total_cjk,
        crutch_stats=crutch_stats,
        findings=findings,
        disclosures=disclosures,
        body_cjk=body_cjk,
    )


def print_report(report: FileReport) -> None:
    print(f"=== {report.path} (CJK={report.total_cjk}, 正文={report.body_cjk}) ===")
    by_cat: dict[str, list[Finding]] = defaultdict(list)
    for item in report.findings:
        by_cat[item.category].append(item)

    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        note = "（阈值：3处及以上；另提示两处逐字相同）" if cat == "跨段长句复读" else ""
        print(f"[{cat}] {len(items)} 处{note}")
        for item in items:
            extra = f"｜{item.role}｜建议：{item.action}" if item.action else ""
            print(f"  - {item.location}: {item.snippet}{extra}")

    if report.disclosures:
        print(f"[出处保留与相近句式] {len(report.disclosures)} 处（不计入候选问题）")
        for item in report.disclosures:
            print(f"  · {item.location}: {item.text}（{item.role}）")

    crutch_summary = ", ".join(
        f"{w}={c}({d:.1f}/10k{'!' if ex else ''})" for w, c, d, ex in report.crutch_stats
    )
    print(f"[套词统计] {crutch_summary}")
    counts = Counter(item.category for item in report.findings)
    total = len(report.findings)
    print(
        f"SUMMARY total={total} "
        + " ".join(f"{cat}={counts.get(cat, 0)}" for cat in CATEGORY_ORDER)
        + f" protected={len(report.disclosures)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="One or more .md, .txt, or .docx files to scan")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any candidate issue is found")
    args = parser.parse_args(argv)

    reports: list[FileReport] = []
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.is_file():
            print(f"ERROR: File not found: {path}", file=sys.stderr)
            return 2
        try:
            reports.append(analyze_file(path))
        except UnsupportedFormat as exc:
            print(f"UNCHECKED: {path}: {exc}", file=sys.stderr)
            return 2
        except (OSError, BadZipFile, ET.ParseError, ValueError) as exc:
            print(f"ERROR: Failed to scan {path}: {exc}", file=sys.stderr)
            return 2

    any_findings = False
    for report in reports:
        print_report(report)
        if report.findings:
            any_findings = True

    if args.strict and any_findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
