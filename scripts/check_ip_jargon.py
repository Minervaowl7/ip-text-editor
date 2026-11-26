"""Read-only candidate scanner for IP jargon, translationese, and AI-style bloat."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
URL_RE = re.compile(r"https?://\S+")

CATEGORY_ORDER = (
    "伪专业隐喻",
    "英文直译腔",
    "空泛管理堆砌",
    "表头或格内压缩失义",
    "句式与高频套词",
    "跨段长句复读",
)

EXPLICIT_METAPHOR_RE = re.compile(
    r"有效底座|权利底座|保护底座|质量体检|综合体检|评分画像|质量画像|权利活度|待激活|"
    r"抓手|赋能|沉淀|颗粒度|赛道|窗口管控|节点触发|性价比|留痕|长周期保护支撑"
)
BARE_BASE_RE = re.compile(r"底座")
MECHANICAL_BASE_CONTEXT_RE = re.compile(
    r"预对准|安装|固定|支撑|机台|设备|卡盘|吸盘|横梁|基台|与基台|上固定|传感器"
)

TRANSLATIONESE_RE = re.compile(
    r"前向被引|后向引|可逆向|易逆向|可取证性|易被外部观察|易观察|"
    r"权属映射|权利映射|成套映射|终端拆解"
)

BUREAUCRATIC_BLOAT_RE = re.compile(
    r"建立[^，。；\n]{0,6}(?:分层|常态化|长效|多维|立体|闭环)[^，。；\n]{0,6}机制|"
    r"分层日常监控|持续监控机制|常态化(?:管理|监控|跟踪)|持续机制|"
    r"系统归集|风险管控举措|按需推进|分步落实"
)

HEADER_PACKED_RE = re.compile(r"角色[／/]取证")
CELL_PACKED_RE = re.compile(r"宽保护核心|互补保护|外围[／/]细化")
SLASH_EXCLUDE_RE = re.compile(
    r"https?://\S+|总件数／有效／审中|S/A/B|GB/T|T/CIPS|Ⅰ／Ⅱ／Ⅲ|BOM/|\b\d+(?:[／/]\d+)+\b"
)
EVIDENCE_AMBIGUOUS_RE = re.compile(r"取证.{0,3}[高低]|侵权可取证性[高中低]")

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

MD_TABLE_SEP_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")
MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
MD_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
MD_ITALIC_NOTE_RE = re.compile(r"^\s*\*[^*]+\*\s*$")

LONG_SENTENCE_LIMIT = 85
REPEAT_CJK_WINDOW = 28
REPEAT_MIN_LOCATIONS = 3


@dataclass(frozen=True)
class Segment:
    location: str
    kind: str  # prose | table_header | table_row | heading | blockquote | note
    text: str
    cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    category: str
    location: str
    snippet: str


@dataclass
class FileReport:
    path: Path
    total_cjk: int
    crutch_stats: list[tuple[str, int, float, bool]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _split_md_cells(line: str) -> tuple[str, ...]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", inner))


def parse_markdown_or_text(path: Path) -> list[Segment]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    segments: list[Segment] = []
    in_fence = False
    in_table = False

    for idx, raw in enumerate(raw_lines, 1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            in_table = False
            continue
        if in_fence or not stripped:
            in_table = False
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            if MD_TABLE_SEP_RE.match(stripped):
                if segments and segments[-1].kind == "table_row":
                    prev = segments[-1]
                    segments[-1] = Segment(prev.location, "table_header", prev.text, prev.cells)
                in_table = True
                continue
            cells = _split_md_cells(stripped)
            next_is_sep = idx < len(raw_lines) and bool(MD_TABLE_SEP_RE.match(raw_lines[idx].strip()))
            kind = "table_header" if (next_is_sep and not in_table) else "table_row"
            in_table = True
            segments.append(Segment(f"L{idx}", kind, " | ".join(cells), cells))
            continue

        in_table = False
        if MD_HEADING_RE.match(stripped):
            segments.append(Segment(f"L{idx}", "heading", MD_HEADING_RE.sub("", stripped)))
        elif stripped.startswith(">"):
            segments.append(Segment(f"L{idx}", "blockquote", stripped.lstrip("> ").strip()))
        elif MD_ITALIC_NOTE_RE.match(stripped):
            segments.append(Segment(f"L{idx}", "note", stripped.strip("* ").strip()))
        else:
            cleaned = MD_LIST_PREFIX_RE.sub("", stripped)
            segments.append(Segment(f"L{idx}", "prose", cleaned))
    return segments


def _docx_paragraph_text(p_elem: ET.Element) -> str:
    deleted = {
        node
        for tag in ("del", "moveFrom")
        for parent in p_elem.iter(f"{W_NS}{tag}")
        for node in parent.iter()
    }
    parts: list[str] = []
    for node in p_elem.iter():
        if node in deleted:
            continue
        local = node.tag.rsplit("}", 1)[-1]
        if local == "t" and node.text:
            parts.append(node.text)
        elif local == "tab":
            parts.append(" ")
    return "".join(parts).strip()


def _docx_paragraph_style(p_elem: ET.Element) -> str:
    p_pr = p_elem.find(f"{W_NS}pPr")
    if p_pr is None:
        return ""
    p_style = p_pr.find(f"{W_NS}pStyle")
    if p_style is None:
        return ""
    return p_style.attrib.get(f"{W_NS}val", "")


def _iter_docx_body_blocks(container: ET.Element):
    for child in container:
        local = child.tag.rsplit("}", 1)[-1]
        if local in {"p", "tbl"}:
            yield local, child
        elif local == "sdt":
            sdt_content = child.find(f"{W_NS}sdtContent")
            if sdt_content is not None:
                yield from _iter_docx_body_blocks(sdt_content)


def parse_docx(path: Path) -> list[Segment]:
    with ZipFile(path) as archive:
        if "word/document.xml" not in archive.namelist():
            raise ValueError("Missing word/document.xml")
        root = ET.fromstring(archive.read("word/document.xml"))

    body = root.find(f"{W_NS}body")
    if body is None:
        return []

    segments: list[Segment] = []
    p_idx = 0
    t_idx = 0
    for kind_tag, elem in _iter_docx_body_blocks(body):
        if kind_tag == "p":
            p_idx += 1
            text = _docx_paragraph_text(elem)
            if not text:
                continue
            style = _docx_paragraph_style(elem)
            if style.startswith("TOC"):
                continue
            if style.startswith(("Heading", "Title", "Subtitle")):
                kind = "heading"
            elif style in {"Quote", "BlockText"} or text.startswith(">"):
                kind = "blockquote"
            elif MD_ITALIC_NOTE_RE.match(text):
                kind = "note"
            else:
                kind = "prose"
            segments.append(Segment(f"P#{p_idx}", kind, text))
        elif kind_tag == "tbl":
            t_idx += 1
            for r_idx, tr in enumerate(elem.findall(f"{W_NS}tr"), 1):
                cells_list: list[str] = []
                for tc in tr.findall(f"{W_NS}tc"):
                    cell_paras = [
                        _docx_paragraph_text(p)
                        for p in tc.findall(f"{W_NS}p")
                    ]
                    cells_list.append(" ".join(cp for cp in cell_paras if cp))
                cells = tuple(cells_list)
                if not any(cells):
                    continue
                row_kind = "table_header" if r_idx == 1 else "table_row"
                segments.append(Segment(f"T#{t_idx} R#{r_idx}", row_kind, " | ".join(cells), cells))
    return segments


def _clip(text: str, limit: int = 80) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else f"{clean[:limit]}..."


def _overlaps(span: tuple[int, int], existing: list[tuple[int, int]]) -> bool:
    return any(not (span[1] <= s or span[0] >= e) for s, e in existing)


def _check_metaphors(seg: Segment, findings: list[Finding]) -> None:
    matched_spans: list[tuple[int, int]] = []
    for match in EXPLICIT_METAPHOR_RE.finditer(seg.text):
        matched_spans.append(match.span())
        findings.append(Finding("伪专业隐喻", seg.location, f"{match.group(0)} ({_clip(seg.text, 55)})"))

    for match in BARE_BASE_RE.finditer(seg.text):
        if _overlaps(match.span(), matched_spans):
            continue
        start = max(0, match.start() - 8)
        end = min(len(seg.text), match.end() + 4)
        window = seg.text[start:end]
        if not MECHANICAL_BASE_CONTEXT_RE.search(window):
            findings.append(Finding("伪专业隐喻", seg.location, f"底座 ({_clip(window, 30)})"))


def _check_regex_category(seg: Segment, pattern: re.Pattern[str], category: str, findings: list[Finding]) -> None:
    for match in pattern.finditer(seg.text):
        findings.append(Finding(category, seg.location, f"{match.group(0)} ({_clip(seg.text, 55)})"))


def _check_tables_and_evidence(seg: Segment, findings: list[Finding]) -> None:
    if seg.kind == "table_header":
        if HEADER_PACKED_RE.search(seg.text):
            findings.append(Finding("表头或格内压缩失义", seg.location, f"压缩表头: {_clip(seg.text, 65)}"))

    if seg.kind in ("table_header", "table_row"):
        for cell in seg.cells:
            if not cell:
                continue
            if CELL_PACKED_RE.search(cell):
                findings.append(Finding("表头或格内压缩失义", seg.location, f"拼栏格值: {_clip(cell, 55)}"))
            cleaned = SLASH_EXCLUDE_RE.sub("", cell)
            if len(re.findall(r"[／/]", cleaned)) >= 2:
                findings.append(Finding("表头或格内压缩失义", seg.location, f"多斜杠拼栏: {_clip(cell, 55)}"))

    for match in EVIDENCE_AMBIGUOUS_RE.finditer(seg.text):
        findings.append(
            Finding("表头或格内压缩失义", seg.location, f"方向不明取证评级: {match.group(0)} ({_clip(seg.text, 45)})")
        )


def _check_syntax_and_walls(seg: Segment, findings: list[Finding]) -> None:
    if seg.kind != "prose":
        return

    for label, pat in PARALLEL_WALL_RULES:
        count = len(pat.findall(seg.text))
        if count >= 3:
            findings.append(Finding("句式与高频套词", seg.location, f"{label}×{count}: {_clip(seg.text, 60)}"))

    no_url = URL_RE.sub("", seg.text)
    for sent in re.split(r"[。！？]", no_url):
        part = sent.strip()
        if not part:
            continue
        cjk_count = len(CJK_RE.findall(part))
        if cjk_count > LONG_SENTENCE_LIMIT:
            findings.append(
                Finding("句式与高频套词", seg.location, f"长句({cjk_count}字): {_clip(part, 65)}")
            )


def _check_repetitions(segments: list[Segment], findings: list[Finding]) -> None:
    units: list[tuple[str, str]] = []
    for seg in segments:
        if seg.kind in ("blockquote", "table_header"):
            continue
        if seg.kind == "table_row":
            for cell in seg.cells:
                if cell:
                    units.append((seg.location, cell))
        else:
            units.append((seg.location, seg.text))

    window_locs: dict[str, set[str]] = defaultdict(set)
    unit_cjk_data: list[tuple[str, str, str, list[int]]] = []

    for loc, raw in units:
        cleaned = URL_RE.sub("", raw)
        cjk_chars: list[str] = []
        orig_positions: list[int] = []
        for idx, ch in enumerate(cleaned):
            if CJK_RE.match(ch):
                cjk_chars.append(ch)
                orig_positions.append(idx)
        cjk_str = "".join(cjk_chars)
        if len(cjk_str) < REPEAT_CJK_WINDOW:
            continue
        unit_cjk_data.append((loc, cleaned, cjk_str, orig_positions))
        seen_local: set[str] = set()
        for start in range(len(cjk_str) - REPEAT_CJK_WINDOW + 1):
            win = cjk_str[start : start + REPEAT_CJK_WINDOW]
            if win not in seen_local:
                seen_local.add(win)
                window_locs[win].add(loc)

    repeated_keys = {k: frozenset(v) for k, v in window_locs.items() if len(v) >= REPEAT_MIN_LOCATIONS}
    if not repeated_keys:
        return

    grouped_spans: dict[frozenset[str], tuple[str, list[str], int]] = {}
    for loc, cleaned, cjk_str, orig_pos in unit_cjk_data:
        i = 0
        n = len(cjk_str)
        while i <= n - REPEAT_CJK_WINDOW:
            win = cjk_str[i : i + REPEAT_CJK_WINDOW]
            locs = repeated_keys.get(win)
            if locs is None:
                i += 1
                continue
            j = i + 1
            while j <= n - REPEAT_CJK_WINDOW and repeated_keys.get(cjk_str[j : j + REPEAT_CJK_WINDOW]) == locs:
                j += 1
            end_cjk = j - 1 + REPEAT_CJK_WINDOW
            orig_slice = cleaned[orig_pos[i] : orig_pos[end_cjk - 1] + 1]
            cjk_len = end_cjk - i
            if locs not in grouped_spans or cjk_len > grouped_spans[locs][2]:
                ordered_locs = sorted(locs, key=lambda x: (len(x), x))
                grouped_spans[locs] = (orig_slice, ordered_locs, cjk_len)
            i = j

    for locs, (snippet, ordered_locs, _) in sorted(
        grouped_spans.items(), key=lambda item: (-len(item[0]), -item[1][2])
    ):
        loc_preview = ", ".join(ordered_locs[:5])
        if len(ordered_locs) > 5:
            loc_preview += f" 等{len(ordered_locs)}处"
        findings.append(
            Finding("跨段长句复读", f"{len(ordered_locs)}x [{loc_preview}]", _clip(snippet, 65))
        )


def analyze_file(path: Path) -> FileReport:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        segments = parse_docx(path)
    elif suffix in {".md", ".markdown", ".txt"}:
        segments = parse_markdown_or_text(path)
    else:
        raise ValueError(f"Unsupported file extension: {suffix}")

    findings: list[Finding] = []
    for seg in segments:
        _check_metaphors(seg, findings)
        _check_regex_category(seg, TRANSLATIONESE_RE, "英文直译腔", findings)
        _check_regex_category(seg, BUREAUCRATIC_BLOAT_RE, "空泛管理堆砌", findings)
        _check_tables_and_evidence(seg, findings)
        _check_syntax_and_walls(seg, findings)

    _check_repetitions(segments, findings)

    full_text = "\n".join(seg.text for seg in segments)
    total_cjk = len(CJK_RE.findall(full_text))
    crutch_stats: list[tuple[str, int, float, bool]] = []
    for word, max_per_10k, min_abs in CRUTCH_WORDS:
        count = len(re.findall(word, full_text))
        per_10k = (count * 10000.0 / total_cjk) if total_cjk else 0.0
        exceeded = per_10k > max_per_10k and count > min_abs
        crutch_stats.append((word, count, per_10k, exceeded))
        if exceeded:
            findings.append(
                Finding(
                    "句式与高频套词",
                    "全文密度",
                    f"高频套词「{word}」{count}次（{per_10k:.2f}/万字，阈值>{max_per_10k}/万字且>{min_abs}次）",
                )
            )

    return FileReport(path=path, total_cjk=total_cjk, crutch_stats=crutch_stats, findings=findings)


def print_report(report: FileReport) -> None:
    print(f"=== {report.path} (CJK={report.total_cjk}) ===")
    by_cat: dict[str, list[Finding]] = defaultdict(list)
    for item in report.findings:
        by_cat[item.category].append(item)

    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        print(f"[{cat}] {len(items)} 处")
        for item in items:
            print(f"  - {item.location}: {item.snippet}")

    crutch_summary = ", ".join(
        f"{w}={c}({d:.1f}/10k{'!' if ex else ''})" for w, c, d, ex in report.crutch_stats
    )
    print(f"[套词统计] {crutch_summary}")
    counts = Counter(item.category for item in report.findings)
    total = len(report.findings)
    print(f"SUMMARY total={total} " + " ".join(f"{cat}={counts.get(cat, 0)}" for cat in CATEGORY_ORDER))


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
