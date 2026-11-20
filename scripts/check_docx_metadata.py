"""Check a deliverable DOCX for tool provenance and local work references."""

import argparse
from io import BytesIO
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("document")
parser.add_argument("--contains", action="append", default=[], metavar="TEXT",
                    help="also flag this exact text in package fields; repeat for several texts")
args = parser.parse_args()
if any(not marker.strip() for marker in args.contains):
    parser.error("--contains requires non-blank text")

tool = r"(?<![a-z0-9])(?:codex|chatgpt|openai|deepseek(?: harness)?|claude(?: code)?|anthropic|gemini|gpt-\d[\w.-]*|python-docx|pandoc|ai\s*助手|人工智能助手|ai assistant)(?![a-z0-9])"
identity = re.compile(tool, re.IGNORECASE)
producer = rf"(?:{tool}|(?<![a-z0-9])ai(?![a-z0-9])|人工智能)"
credit = re.compile(
    rf"(?:由\s*{producer}\s*(?:自动)?(?:生成|撰写|编辑|修订|处理|制作)|"
    rf"(?:generated|written|edited|revised|created|processed)\s+(?:by|with)\s+{producer}|"
    rf"(?:作者|最后修改者|生成器|生成工具|生成模型|处理工具|修订者|编辑者)\s*[:：]\s*{producer})",
    re.IGNORECASE,
)
local = re.compile(
    r"file:/|\b[a-z]:[\\/](?![\\/])|\\\\[^\\\s]+\\|"
    r"(?<![\w/])/(?:Users|home|private|tmp|var/folders|mnt/data|mnt/[a-z]|workspace(?:s)?)/|"
    r"\.codex[/\\]|\b(?:session|thread|tool[ _-]?call)[ _-]?(?:id|[0-9a-f]{8}-[0-9a-f]{4})\b|"
    r"(?:会话|工具调用)(?:标识|编号|\s*ID)",
    re.IGNORECASE,
)
identity_fields = {
    "author", "creator", "lastmodifiedby", "modifiedby", "lastauthor",
    "initials", "person", "displayname", "application", "generator", "producer",
    "createdby", "generatedby", "generatedwith", "processingtool",
}
source_path_fields = {
    "sourcepath", "originpath", "localpath", "workspacepath",
    "workpath", "workdir", "workingdir", "workingdirectory",
}
relationship_tag = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
findings = set()
try:
    with ZipFile(args.document) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or not {"[Content_Types].xml", "_rels/.rels"} <= set(names):
            parser.exit(2, "Invalid or ambiguous package.\n")
        for index, item in enumerate(package.infolist(), 1):
            part = item.filename
            location = part
            if local.search(part) or identity.search(part) or any(marker in part for marker in args.contains):
                location = f"(package member {index})"
                findings.add((location, "package member name"))
            comment = item.comment.decode("utf-8", "replace")
            if local.search(comment) or identity.search(comment) or any(marker in comment for marker in args.contains):
                findings.add((location, "package member comment"))
            if "/embeddings/" in part and not item.is_dir():
                findings.add((location, "inspect embedded object separately"))
            if not part.lower().endswith((".xml", ".rels", ".svg", ".vml", ".xmp")):
                continue
            xml = BytesIO(package.read(item))
            for event, element in ET.iterparse(xml, events=("end", "comment", "pi")):
                if event != "end":
                    value = element.text or ""
                    if (credit.search(value) or local.search(value) or (event == "pi" and identity.search(value))
                            or any(marker in value for marker in args.contains)):
                        label = "XML comment" if event == "comment" else "XML processing instruction"
                        findings.add((location, label))
                    continue
                if (element.tag == relationship_tag and part.lower().endswith(".rels")
                        and element.get("TargetMode") == "External"):
                    # Relative external resources resolve against the package's location.
                    target = element.get("Target", "").strip()
                    if (target and not target.startswith("#")
                            and not re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target)):
                        findings.add((location, "relative external relationship target"))
                field = element.tag.rsplit("}", 1)[-1].lower()
                attributes = {key.rsplit("}", 1)[-1].lower(): value for key, value in element.attrib.items()}
                values = [(field, element.text or ""), ("tail", element.tail or ""), *attributes.items()]
                if field == "property" and "name" in attributes:
                    values.append((attributes["name"].lower(), "".join(element.itertext())))
                for label, value in values:
                    key = re.sub(r"[\s_-]+", "", label)
                    in_metadata = part.startswith("docProps/") or key in identity_fields
                    identifying_value = credit.search(value) or (
                        in_metadata and (identity.search(value) or value.strip().lower() in {"ai", "人工智能"})
                    ) or (
                        field in {"docpr", "cnvpr"} and label in {"name", "descr", "title"}
                        and identity.fullmatch(value.strip())
                    )
                    source_path = (
                        key in source_path_fields and re.search(r"[\\/]", value)
                        and not value.strip().lower().startswith(("https://", "http://"))
                    )
                    if (identifying_value or local.search(value) or source_path
                            or any(marker in value for marker in args.contains)):
                        reported_field = "custom property value" if (
                            field == "property" and label == attributes.get("name", "").lower()
                        ) else label
                        if identity.search(reported_field) or local.search(reported_field) or any(
                            marker in reported_field for marker in args.contains
                        ):
                            reported_field = "XML field"
                        findings.add((location, reported_field))
                if field == "p":
                    nodes = [
                        node for node in element.iter()
                        if node.tag.rsplit("}", 1)[-1] in {"t", "delText", "instrText"}
                    ]
                    for excluded in (set(), {"del", "moveFrom"}, {"ins", "moveTo"}):
                        skipped = {
                            child for revision in element.iter()
                            if revision.tag.rsplit("}", 1)[-1] in excluded
                            for child in revision.iter()
                        }
                        paragraph = "".join(node.text or "" for node in nodes if node not in skipped)
                        if credit.search(paragraph) or local.search(paragraph) or any(
                            marker in paragraph for marker in args.contains
                        ):
                            findings.add((location, "paragraph"))
        comment = package.comment.decode("utf-8", "replace")
        if identity.search(comment) or local.search(comment) or any(marker in comment for marker in args.contains):
            findings.add(("(package)", "ZIP comment"))
except (OSError, BadZipFile, ET.ParseError, RuntimeError) as error:
    parser.exit(2, f"Could not scan package ({type(error).__name__}).\n")

for part, field in sorted(findings):
    print(f"REVIEW {part}: {field}")
if findings:
    parser.exit(1)
print("PASS: no known provenance markers in scanned package fields.")
