"""Check deliverable DOCX packages for tool provenance, local paths, and review artifacts."""

import argparse
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import zlib


class FieldRole(str, Enum):
    BODY = "body"
    METADATA_IDENTITY = "metadata_identity"
    METADATA_TOPIC = "metadata_topic"
    XML_COMMENT = "xml_comment"
    PACKAGE_META = "package_meta"
    IMAGE_IDENTITY = "image_identity"
    IMAGE_TOPIC = "image_topic"


DOC_BUILDER_PATTERN = r"(?<![a-z0-9-])(?:python[_-]docx|docx-js|pandoc)(?![a-z0-9-])"
DOC_BUILDER_RE = re.compile(DOC_BUILDER_PATTERN, re.IGNORECASE)

TOOL_IDENTITY_CORE_PATTERN = (
    r"(?<![a-z0-9-])(?:"
    r"codex|chatgpt|openai|deepseek(?: harness)?|claude(?: code)?|anthropic|gemini|"
    r"gpt-?\d[\w.-]*|o[134]-(?:mini|preview|pro)|kimi|moonshot|qwen|"
    r"doubao|wenxin[- ]?yiyan|ernie[- ]?bot|glm-?\d[\w.-]*|"
    r"cursor\s+(?:ide|ai)|(?:github\s*)?copilot|windsurf|antigravity|manus|"
    r"ai assistant"
    r")(?![a-z0-9-])|"
    r"(?<![a-z0-9-])ai\s*助手|"
    r"通义(?:千问)?|豆包|文心一言|智谱|人工智能助手"
)
TOOL_IDENTITY_PATTERN = rf"{TOOL_IDENTITY_CORE_PATTERN}|(?<![a-z0-9-])cursor(?![a-z0-9-])"
GENERIC_AI_PATTERN = r"大(?:语言)?模型|(?<![a-z0-9-])ai(?![a-z0-9-])|人工智能"
BARE_AI_VALUES = {"ai", "人工智能", "大模型", "大语言模型"}

IDENTITY_RE = re.compile(rf"(?:{DOC_BUILDER_PATTERN}|{TOOL_IDENTITY_PATTERN})", re.IGNORECASE)
DRAWING_OR_COMMENT_IDENTITY_RE = re.compile(
    rf"(?:{DOC_BUILDER_PATTERN}|{TOOL_IDENTITY_CORE_PATTERN})",
    re.IGNORECASE,
)
PRODUCER_PATTERN = rf"(?:{DOC_BUILDER_PATTERN}|{TOOL_IDENTITY_PATTERN}|{GENERIC_AI_PATTERN})"

METADATA_CREDIT_RE = re.compile(
    rf"(?:(?:由|经|通过|使用|借助)\s*{PRODUCER_PATTERN}\s*(?:自动|辅助)?\s*"
    rf"(?:生成|撰写|起草|编辑|修订|修改|润色|整理|校对|审阅|处理|制作|输出|完成)|"
    rf"\b(?:generated|written|edited|revised|created|built|exported|polished|processed)\s+(?:by|with)\s+{PRODUCER_PATTERN}|"
    rf"(?:最后修改者|生成器|生成工具|生成模型|处理工具|修订者|编辑者)\s*[:：]\s*{PRODUCER_PATTERN}|"
    rf"作者\s*[:：]\s*(?:{DOC_BUILDER_PATTERN}|{TOOL_IDENTITY_PATTERN}|(?:{GENERIC_AI_PATTERN})(?![\u4e00-\u9fffA-Za-z0-9_-])))",
    re.IGNORECASE,
)

BODY_CREDIT_RE = re.compile(
    rf"(?:\b(?:generated|created|built|exported)\s+(?:by|with)\s+{DOC_BUILDER_PATTERN}|"
    rf"(?:由|经|通过|使用)\s*{DOC_BUILDER_PATTERN}\s*(?:自动|辅助)?\s*(?:生成|制作|导出|输出)|"
    rf"(?:本(?:文|报告|稿|文档|材料|内容|版本|修订稿|件)|以上内容|上述内容)\s*(?:系|是)?\s*"
    rf"(?:由|经|通过|使用|借助)\s*{PRODUCER_PATTERN}\s*(?:自动|辅助)?\s*"
    rf"(?:生成|撰写|起草|编辑|修订|修改|润色|整理|校对|审阅|处理|制作|输出|完成)|"
    rf"\bthis\s+(?:document|report|draft|file|text|memo|summary)\s+(?:was|is)\s+"
    rf"(?:generated|written|edited|revised|created|polished|processed)\s+(?:by|with)\s+{PRODUCER_PATTERN}|"
    rf"(?:由|经|通过|使用|借助)\s*{PRODUCER_PATTERN}\s*(?:自动|辅助)?\s*(?:润色|校对|去AI味|去异味)|"
    rf"(?:最后修改者|生成器|生成工具|生成模型|处理工具|修订者|编辑者)\s*[:：]\s*{PRODUCER_PATTERN}(?:\s|$|[，。；,.;)\]]))",
    re.IGNORECASE,
)

WEB_URL_RE = re.compile(r"https?://[^\s)>\"'`]+", re.IGNORECASE)
FILE_URI_RE = re.compile(r"(?<![a-z0-9_])file:/", re.IGNORECASE)
UNC_PATH_PATTERN = (
    r"(?<![\\A-Za-z0-9])\\\\(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9][A-Za-z0-9._-]{0,62})"
    r"\\(?![\\])[A-Za-z0-9._\-\u4e00-\u9fff]+\\(?![\\])[^\s\\]+|"
    r"(?<![\\A-Za-z0-9])\\\\(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9][A-Za-z0-9_-]*[.-][A-Za-z0-9._-]+)"
    r"\\(?![\\])[A-Za-z0-9._\-\u4e00-\u9fff]+"
)
DRIVE_PATH_PATTERN = (
    r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\(?![\\]|(?:r|n|t)(?:\\|$|\s))[^\s\\]|/(?!/)[^\s/]+(?:/|\.[A-Za-z0-9]{1,8}\b))"
)
AGENT_DIR_PATTERN = r"\.(?:codex|claude|cursor|gemini|antigravity|windsurf)[/\\]"
SESSION_ID_PATTERN = (
    r"\b(?:session|thread|tool[ _-]?call)(?:[ _-]?(?:id)?[ _-]?[0-9a-f]{8}-[0-9a-f]{4}|"
    r"[ _-]?id\s*[:=]\s*[A-Za-z0-9._-]{6,}(?!\s*\())\b|"
    r"(?:会话|工具调用)(?:标识|编号|\s*ID)\s*[:：=]\s*[A-Za-z0-9._-]{4,}(?!\s*\()"
)
NON_URI_LOCAL_RE = re.compile(
    rf"(?:{UNC_PATH_PATTERN}|{DRIVE_PATH_PATTERN}|{AGENT_DIR_PATTERN}|{SESSION_ID_PATTERN})",
    re.IGNORECASE,
)
UNIX_LOCAL_RE = re.compile(
    r"(?<![\w/])(?:/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|/private/(?:var|tmp)/|"
    r"/var/folders/|/mnt/(?:data|[a-z])/|/tmp/[A-Za-z0-9._-]+/|/workspaces?/[A-Za-z0-9._-]+/)"
)

IDENTITY_FIELDS = {
    "author", "creator", "creatortool", "lastmodifiedby", "modifiedby", "lastauthor",
    "initials", "person", "displayname", "application", "generator", "producer",
    "createdby", "generatedby", "generatedwith", "processingtool",
}
SOURCE_PATH_FIELDS = {
    "sourcepath", "originpath", "localpath", "workspacepath",
    "workpath", "workdir", "workingdir", "workingdirectory",
}
PNG_TOPIC_KEYWORDS = {"title", "description", "disclaimer", "warning", "copyright"}
XMP_TOPIC_TAGS = {"title", "description", "subject", "rights", "headline"}
XMP_IDENTITY_TAGS = {
    "creatortool", "software", "producer", "creator", "author",
    "authorsposition", "history", "softwareagent", "contributor", "publisher", "credit",
}
XMP_PACKET_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"

PARAGRAPH_TEXT_TAGS = {"t", "delText", "instrText", "delInstrText"}
PARAGRAPH_TEXT_TAGS_LOWER = {tag.lower() for tag in PARAGRAPH_TEXT_TAGS}
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REVISION_TAGS = {
    "ins", "del", "moveFrom", "moveTo",
    "rPrChange", "pPrChange", "tblPrChange", "tcPrChange",
}
RELATIONSHIP_TAG = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


@dataclass(frozen=True, order=True)
class Finding:
    part: str
    location: str
    category: str

    def format(self) -> str:
        return f"REVIEW {self.part} [{self.location}] ({self.category})"

    def to_dict(self) -> dict[str, str]:
        return {
            "part": self.part,
            "location": self.location,
            "category": self.category,
        }


def has_local_path(value: str) -> bool:
    if FILE_URI_RE.search(value):
        return True
    stripped = WEB_URL_RE.sub(" ", value)
    return bool(NON_URI_LOCAL_RE.search(stripped) or UNIX_LOCAL_RE.search(stripped))


def has_tool_identity(value: str, role: FieldRole) -> bool:
    if role == FieldRole.BODY:
        return bool(BODY_CREDIT_RE.search(value))
    if METADATA_CREDIT_RE.search(value):
        return True
    if role in {FieldRole.METADATA_IDENTITY, FieldRole.PACKAGE_META, FieldRole.IMAGE_IDENTITY}:
        if IDENTITY_RE.search(value):
            return True
    elif role == FieldRole.XML_COMMENT:
        if DRAWING_OR_COMMENT_IDENTITY_RE.search(value):
            return True
    elif DOC_BUILDER_RE.search(value):
        return True
    if role in {FieldRole.METADATA_IDENTITY, FieldRole.IMAGE_IDENTITY} and value.strip().lower() in BARE_AI_VALUES:
        return True
    return False


def classify_text(
    value: str,
    contains: list[str],
    *,
    role: FieldRole,
    extra_local: bool = False,
) -> list[str]:
    categories: list[str] = []
    if extra_local or has_local_path(value):
        categories.append("local-path")
    if has_tool_identity(value, role):
        categories.append("tool-identity")
    for index, marker in enumerate(contains, 1):
        if marker in value:
            categories.append(f"contains#{index}")
    return categories


def extract_xmp_xml(text: str) -> str | None:
    for start_tag, end_tag in (("<x:xmpmeta", "</x:xmpmeta>"), ("<rdf:RDF", "</rdf:RDF>")):
        start = text.find(start_tag)
        if start != -1:
            end = text.find(end_tag, start)
            if end != -1:
                return text[start : end + len(end_tag)]
    return None


def walk_xmp_element(
    element: ET.Element,
    inherited_role: FieldRole,
    out: list[tuple[FieldRole, str]],
) -> None:
    tag_name = element.tag.rsplit("}", 1)[-1].lower()
    if tag_name in XMP_TOPIC_TAGS:
        elem_role = FieldRole.IMAGE_TOPIC
    elif tag_name in XMP_IDENTITY_TAGS:
        elem_role = FieldRole.IMAGE_IDENTITY
    else:
        elem_role = inherited_role

    for key, value in element.attrib.items():
        attr_name = key.rsplit("}", 1)[-1].lower()
        if attr_name in XMP_TOPIC_TAGS:
            attr_role = FieldRole.IMAGE_TOPIC
        elif attr_name in XMP_IDENTITY_TAGS:
            attr_role = FieldRole.IMAGE_IDENTITY
        else:
            attr_role = elem_role
        if value:
            out.append((attr_role, value))

    if element.text and element.text.strip():
        out.append((elem_role, element.text))

    for child in element:
        walk_xmp_element(child, elem_role, out)
        if child.tail and child.tail.strip():
            out.append((elem_role, child.tail))


def parse_xmp_fields(text: str) -> list[tuple[FieldRole, str]] | None:
    xmp_xml = extract_xmp_xml(text)
    if xmp_xml is None:
        return None
    try:
        root = ET.fromstring(xmp_xml)
    except ET.ParseError:
        return None
    fields: list[tuple[FieldRole, str]] = []
    walk_xmp_element(root, FieldRole.IMAGE_IDENTITY, fields)
    return fields


def png_texts(data: bytes) -> list[tuple[FieldRole, str]]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return []
    texts: list[tuple[FieldRole, str]] = []
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            break
        payload = data[start:end]
        offset = end + 4
        if chunk_type == b"tEXt":
            sep = payload.find(b"\x00")
            keyword = payload[:sep].decode("utf-8", "replace") if sep != -1 else ""
            role = FieldRole.IMAGE_TOPIC if keyword.strip().lower() in PNG_TOPIC_KEYWORDS else FieldRole.IMAGE_IDENTITY
            texts.append((role, payload.replace(b"\x00", b": ").decode("utf-8", "replace")))
        elif chunk_type == b"zTXt":
            sep = payload.find(b"\x00")
            if sep != -1 and sep + 2 <= len(payload) and payload[sep + 1] == 0:
                keyword = payload[:sep].decode("utf-8", "replace")
                try:
                    value = zlib.decompress(payload[sep + 2 :]).decode("utf-8", "replace")
                except zlib.error:
                    continue
                role = FieldRole.IMAGE_TOPIC if keyword.strip().lower() in PNG_TOPIC_KEYWORDS else FieldRole.IMAGE_IDENTITY
                texts.append((role, f"{keyword}: {value}"))
        elif chunk_type == b"iTXt":
            sep1 = payload.find(b"\x00")
            if sep1 == -1 or sep1 + 3 > len(payload):
                continue
            sep2 = payload.find(b"\x00", sep1 + 3)
            if sep2 == -1:
                continue
            sep3 = payload.find(b"\x00", sep2 + 1)
            if sep3 == -1:
                continue
            raw_text = payload[sep3 + 1 :]
            if payload[sep1 + 1] == 1:
                if payload[sep1 + 2] != 0:
                    continue
                try:
                    raw_text = zlib.decompress(raw_text)
                except zlib.error:
                    continue
            keyword = payload[:sep1].decode("utf-8", "replace")
            translated = payload[sep2 + 1 : sep3].decode("utf-8", "replace")
            body_text = raw_text.decode("utf-8", "replace")
            lower_kw = keyword.strip().lower()
            if lower_kw == "xml:com.adobe.xmp" or "<x:xmpmeta" in body_text or "<rdf:RDF" in body_text:
                xmp_entries = parse_xmp_fields(body_text)
                if xmp_entries is not None:
                    header_text = f"{keyword} {translated}".strip()
                    if header_text:
                        texts.append((FieldRole.IMAGE_IDENTITY, header_text))
                    texts.extend(xmp_entries)
                    continue
            role = FieldRole.IMAGE_TOPIC if lower_kw in PNG_TOPIC_KEYWORDS else FieldRole.IMAGE_IDENTITY
            texts.append((role, f"{keyword} {translated} {body_text}"))
        elif chunk_type == b"IEND":
            break
    return texts


def jpeg_texts(data: bytes) -> list[tuple[FieldRole, str]]:
    if not data.startswith(b"\xff\xd8"):
        return []
    texts: list[tuple[FieldRole, str]] = []
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x00 or marker == 0x01 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        seg_len = int.from_bytes(data[offset : offset + 2], "big")
        if seg_len < 2 or offset + seg_len > len(data):
            break
        payload = data[offset + 2 : offset + seg_len]
        offset += seg_len
        if marker in (0xFE, 0xE1):
            if marker == 0xE1:
                if payload.startswith(XMP_PACKET_HEADER):
                    xmp_raw = payload[len(XMP_PACKET_HEADER) :]
                    encoding = "utf-16" if xmp_raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
                    xmp_entries = parse_xmp_fields(xmp_raw.decode(encoding, "replace"))
                    if xmp_entries is not None:
                        texts.extend(xmp_entries)
                        continue
                elif b"<x:xmpmeta" in payload or b"<rdf:RDF" in payload:
                    xmp_entries = parse_xmp_fields(payload.decode("utf-8", "replace"))
                    if xmp_entries is not None:
                        texts.extend(xmp_entries)
                        continue
            texts.append((FieldRole.IMAGE_IDENTITY, payload.decode("utf-8", "replace")))
            if b"\x00" in payload:
                for slice_bytes in (payload, payload[1:]):
                    texts.append((FieldRole.IMAGE_IDENTITY, slice_bytes.decode("utf-16le", "replace")))
                    texts.append((FieldRole.IMAGE_IDENTITY, slice_bytes.decode("utf-16be", "replace")))
    return texts


def scan_xmp_part(
    safe_part: str,
    raw_bytes: bytes,
    contains: list[str],
    findings: set[Finding],
) -> bool:
    xmp_entries = parse_xmp_fields(raw_bytes.decode("utf-8", "replace"))
    if xmp_entries is None:
        return False
    for role, value in xmp_entries:
        for category in classify_text(value, contains, role=role):
            findings.add(Finding(safe_part, "xmp-metadata", category))
    return True


def scan_xml_part(
    safe_part: str,
    lower_part: str,
    raw_bytes: bytes,
    contains: list[str],
    findings: set[Finding],
    *,
    clean_draft: bool = False,
) -> None:
    comment_count = 0
    pi_count = 0
    p_count = 0
    p_stack: list[int] = []
    rel_count = 0
    w_comment_count = 0

    for event, element in ET.iterparse(BytesIO(raw_bytes), events=("start", "end", "comment", "pi")):
        if event == "start":
            if element.tag.rsplit("}", 1)[-1].lower() == "p":
                p_count += 1
                p_stack.append(p_count)
            continue
        if event == "comment":
            comment_count += 1
            for category in classify_text(element.text or "", contains, role=FieldRole.XML_COMMENT):
                findings.add(Finding(safe_part, f"xml-comment#{comment_count}", category))
            continue
        if event == "pi":
            pi_count += 1
            for category in classify_text(element.text or "", contains, role=FieldRole.XML_COMMENT):
                findings.add(Finding(safe_part, f"xml-pi#{pi_count}", category))
            continue

        raw_local = element.tag.rsplit("}", 1)[-1]
        field = raw_local.lower()
        current_p = p_stack.pop() if field == "p" and p_stack else p_count

        if clean_draft:
            if element.tag.startswith(f"{{{W_NS}}}") and raw_local in REVISION_TAGS:
                rev_loc = f"p#{p_stack[-1]}" if p_stack else raw_local
                findings.add(Finding(safe_part, rev_loc, "unfinished-revision"))
            elif lower_part == "word/comments.xml" and element.tag == f"{{{W_NS}}}comment":
                w_comment_count += 1
                findings.add(Finding(safe_part, f"comment#{w_comment_count}", "leftover-comment"))
            elif element.tag == f"{{{W_NS}}}highlight":
                highlight_val = element.get(f"{{{W_NS}}}val") or element.get("val") or ""
                if highlight_val.strip().lower() == "yellow":
                    hl_loc = f"p#{p_stack[-1]}" if p_stack else "highlight"
                    findings.add(Finding(safe_part, hl_loc, "review-highlight"))

        if element.tag == RELATIONSHIP_TAG and lower_part.endswith(".rels"):
            rel_count += 1
            if element.get("TargetMode") == "External":
                target = element.get("Target", "").strip()
                if target and not target.startswith("#") and not re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                    raw_id = (element.get("Id") or "").strip()
                    rel_label = (
                        raw_id
                        if raw_id and not classify_text(raw_id, contains, role=FieldRole.METADATA_IDENTITY)
                        else str(rel_count)
                    )
                    findings.add(Finding(safe_part, f"rel#{rel_label}", "relative-external-target"))

        if not (p_stack and field in PARAGRAPH_TEXT_TAGS_LOWER):
            attributes = {key.rsplit("}", 1)[-1].lower(): value for key, value in element.attrib.items()}
            values = [(field, element.text or ""), ("tail", element.tail or ""), *attributes.items()]
            if field == "property" and "name" in attributes:
                values.append((attributes["name"].lower(), "".join(element.itertext())))
            for label, value in values:
                key = re.sub(r"[\s_-]+", "", label)
                role = FieldRole.METADATA_IDENTITY if key in IDENTITY_FIELDS else FieldRole.METADATA_TOPIC
                drawing_match = (
                    field in {"docpr", "cnvpr"}
                    and label in {"name", "descr", "title"}
                    and bool(DRAWING_OR_COMMENT_IDENTITY_RE.fullmatch(value.strip()))
                )
                source_path = (
                    key in SOURCE_PATH_FIELDS
                    and bool(re.search(r"[\\/]", value))
                    and not value.strip().lower().startswith(("https://", "http://"))
                )
                categories = classify_text(
                    value,
                    contains,
                    role=role,
                    extra_local=source_path,
                )
                if drawing_match and "tool-identity" not in categories:
                    categories.append("tool-identity")
                if categories:
                    reported_field = (
                        "custom property value"
                        if field == "property" and label == attributes.get("name", "").lower()
                        else label
                    )
                    if classify_text(reported_field, contains, role=FieldRole.METADATA_IDENTITY):
                        reported_field = "xml-field"
                    for category in categories:
                        findings.add(Finding(safe_part, reported_field, category))

        if field == "p":
            nodes = [
                node for node in element.iter()
                if node.tag.rsplit("}", 1)[-1] in PARAGRAPH_TEXT_TAGS
            ]
            for excluded in (set(), {"del", "moveFrom"}, {"ins", "moveTo"}):
                skipped = {
                    child for revision in element.iter()
                    if revision.tag.rsplit("}", 1)[-1] in excluded
                    for child in revision.iter()
                }
                paragraph = "".join(node.text or "" for node in nodes if node not in skipped)
                for category in classify_text(paragraph, contains, role=FieldRole.BODY):
                    findings.add(Finding(safe_part, f"p#{current_p}", category))


def scan_package(
    document: str | Path,
    contains: list[str],
    *,
    clean_draft: bool = False,
) -> set[Finding]:
    findings: set[Finding] = set()
    with ZipFile(document) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or not {"[Content_Types].xml", "_rels/.rels"} <= set(names):
            raise ValueError("Invalid or ambiguous package.")
        for index, item in enumerate(package.infolist(), 1):
            part = item.filename
            name_categories = classify_text(part, contains, role=FieldRole.PACKAGE_META)
            safe_part = f"(package member {index})" if name_categories else part
            for category in name_categories:
                findings.add(Finding(safe_part, "member-name", category))

            member_comment = item.comment.decode("utf-8", "replace")
            for category in classify_text(member_comment, contains, role=FieldRole.PACKAGE_META):
                findings.add(Finding(safe_part, "member-comment", category))

            if item.is_dir():
                continue
            if "/embeddings/" in part:
                findings.add(Finding(safe_part, "embedded-object", "inspect-separately"))

            lower_part = part.lower()
            if lower_part.endswith(".xmp"):
                raw_bytes = package.read(item)
                if not scan_xmp_part(safe_part, raw_bytes, contains, findings):
                    scan_xml_part(safe_part, lower_part, raw_bytes, contains, findings, clean_draft=clean_draft)
            elif lower_part.endswith((".xml", ".rels", ".svg", ".vml")):
                scan_xml_part(safe_part, lower_part, package.read(item), contains, findings, clean_draft=clean_draft)
            elif lower_part.endswith(".png"):
                for role, text in png_texts(package.read(item)):
                    for category in classify_text(text, contains, role=role):
                        findings.add(Finding(safe_part, "image-metadata", category))
            elif lower_part.endswith((".jpg", ".jpeg")):
                for role, text in jpeg_texts(package.read(item)):
                    for category in classify_text(text, contains, role=role):
                        findings.add(Finding(safe_part, "image-metadata", category))

        zip_comment = package.comment.decode("utf-8", "replace")
        for category in classify_text(zip_comment, contains, role=FieldRole.PACKAGE_META):
            findings.add(Finding("(package)", "zip-comment", category))
    return findings


def discover_targets(inputs: list[str]) -> tuple[list[tuple[Path, str | None]], bool]:
    is_multi = len(inputs) > 1 or any(Path(raw).is_dir() for raw in inputs)
    targets: list[tuple[Path, str | None]] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            discovered = sorted(
                (
                    child
                    for child in path.rglob("*")
                    if child.is_file()
                    and child.suffix.lower() == ".docx"
                    and not child.name.startswith("~$")
                ),
                key=lambda item: str(item),
            )
            if not discovered:
                targets.append((path, "No .docx files found in directory."))
            else:
                for child in discovered:
                    targets.append((child, None))
        else:
            targets.append((path, None))
    return targets, is_multi


def scan_target(
    path: Path,
    contains: list[str],
    *,
    clean_draft: bool,
) -> tuple[list[Finding], str | None]:
    try:
        findings = sorted(scan_package(path, contains, clean_draft=clean_draft))
    except ValueError as error:
        return [], str(error)
    except (OSError, BadZipFile, ET.ParseError, RuntimeError) as error:
        return [], f"Could not scan package ({type(error).__name__})."
    return findings, None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", nargs="+")
    parser.add_argument(
        "--contains",
        action="append",
        default=[],
        metavar="TEXT",
        help="also flag this exact text in package fields; repeat for several texts",
    )
    parser.add_argument(
        "--clean-draft",
        action="store_true",
        help="also flag leftover track changes, comments, and yellow highlights",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit structured JSON output",
    )
    args = parser.parse_args()
    if any(not marker.strip() for marker in args.contains):
        parser.error("--contains requires non-blank text")

    targets, is_multi = discover_targets(args.documents)

    if not is_multi and not args.json_output:
        path, preset_error = targets[0]
        if preset_error is not None:
            parser.exit(2, f"{preset_error}\n")
        findings, error_msg = scan_target(path, args.contains, clean_draft=args.clean_draft)
        if error_msg is not None:
            parser.exit(2, f"{error_msg}\n")
        for finding in findings:
            print(finding.format())
        if findings:
            parser.exit(1)
        print("PASS: no known provenance markers in scanned package fields.")
        return

    file_records: list[dict[str, object]] = []
    has_error = False
    has_review = False

    for path, preset_error in targets:
        if preset_error is not None:
            has_error = True
            file_records.append(
                {
                    "path": str(path),
                    "status": "ERROR",
                    "findings": [],
                    "error": preset_error,
                }
            )
            if not args.json_output:
                print(f"{path}: ERROR ({preset_error})", file=sys.stderr)
            continue

        findings, error_msg = scan_target(path, args.contains, clean_draft=args.clean_draft)
        if error_msg is not None:
            has_error = True
            file_records.append(
                {
                    "path": str(path),
                    "status": "ERROR",
                    "findings": [],
                    "error": error_msg,
                }
            )
            if not args.json_output:
                print(f"{path}: ERROR ({error_msg})", file=sys.stderr)
        elif findings:
            has_review = True
            file_records.append(
                {
                    "path": str(path),
                    "status": "REVIEW",
                    "findings": [finding.to_dict() for finding in findings],
                    "error": None,
                }
            )
            if not args.json_output:
                for finding in findings:
                    print(f"{path}: {finding.format()}")
        else:
            file_records.append(
                {
                    "path": str(path),
                    "status": "PASS",
                    "findings": [],
                    "error": None,
                }
            )
            if not args.json_output:
                print(f"{path}: PASS")

    overall_status = "ERROR" if has_error else ("REVIEW" if has_review else "PASS")
    if args.json_output:
        print(json.dumps({"status": overall_status, "files": file_records}, ensure_ascii=False, indent=2))

    if has_error:
        parser.exit(2)
    if has_review:
        parser.exit(1)


if __name__ == "__main__":
    main()
