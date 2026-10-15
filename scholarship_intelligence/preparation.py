"""Prepare saved HTML or text PDFs with stable source evidence locations."""

from dataclasses import asdict, dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from scrapy.http import HtmlResponse

from scholarship_intelligence.settings import (
    PREPARED_SNAPSHOT_DIR,
    PROJECT_ROOT,
    RAW_SNAPSHOT_DIR,
)
from scholarship_intelligence.snapshots import RawSnapshot


PREPARATION_VERSION = "html-v2"
PDF_PREPARATION_VERSION = "pdf-text-v1"
_VERIFIED_PREPARATIONS = {}
# Configurable starting bounds for local PDF parsing, not crawler URL budgets.
MAX_PDF_PAGES = 200
MAX_PDF_PAGE_STREAM_BYTES = 10 * 1024 * 1024

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
TEXT_BLOCKS = {"p", "li", "dt", "dd", "blockquote", "pre"}
BLOCK_TAGS = HEADINGS | TEXT_BLOCKS | {
    "table",
    "div",
    "section",
    "article",
    "ul",
    "ol",
    "main",
    "header",
    "footer",
}


@dataclass(frozen=True)
class SourceLink:
    text: str
    url: str


@dataclass(frozen=True)
class TableCell:
    text: str
    is_header: bool
    rowspan: str | None
    colspan: str | None


@dataclass(frozen=True)
class ContentBlock:
    block_id: str
    kind: str
    text: str
    source_xpath: str
    # Reflects source HTML heading levels, not inferred eligibility relationships.
    heading_path: tuple[str, ...]
    links: tuple[SourceLink, ...]
    table_rows: tuple[tuple[TableCell, ...], ...] = ()


@dataclass(frozen=True)
class PreparedHTML:
    snapshot_id: str
    source_url: str
    content_hash: str
    preparation_version: str
    source_encoding: str
    raw_file_path: str
    prepared_file_path: str
    blocks: tuple[ContentBlock, ...]


@dataclass(frozen=True)
class PDFBlock:
    block_id: str
    kind: str
    text: str
    page_number: int
    source_xpath: None
    heading_path: tuple[str, ...]
    links: tuple[SourceLink, ...]
    table_rows: tuple = ()


@dataclass(frozen=True)
class PreparedPDF:
    snapshot_id: str
    source_url: str
    content_hash: str
    preparation_version: str
    source_encoding: None
    raw_file_path: str
    prepared_file_path: str
    blocks: tuple[PDFBlock, ...]
    page_count: int
    parser_version: str
    extraction_method: str
    blank_pages: tuple[int, ...]
    preparation_notes: tuple[str, ...]


def _tag(node):
    return node.tag.lower() if isinstance(node.tag, str) else ""


def _clean_text(text):
    """Normalize whitespace while keeping meaningful line breaks."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _read_text_raw(node):
    """Collect text without stripping whitespace between inline elements."""
    parts = [node.text or ""]
    break_tags = BLOCK_TAGS | {"tr", "th", "td", "caption"}

    for child in node:
        tag = _tag(child)
        if not tag:
            # Ignore comment contents, but keep following text.
            parts.append(child.tail or "")
            continue

        if tag == "br":
            parts.append("\n")
        else:
            if tag in break_tags:
                parts.append("\n")

            if tag == "img" and child.get("alt"):
                parts.append(child.get("alt"))
            else:
                parts.append(_read_text_raw(child))

            if tag in break_tags:
                parts.append("\n")

        parts.append(child.tail or "")

    return "".join(parts)


def _read_text(node):
    """Normalize only after collecting the complete block."""
    return _clean_text(_read_text_raw(node))


def _remove_node(node):
    """Remove navigation/script content without losing following text."""
    parent = node.getparent()
    if parent is None:
        return

    tail = node.tail or ""
    previous = node.getprevious()
    if previous is None:
        parent.text = (parent.text or "") + tail
    else:
        previous.tail = (previous.tail or "") + tail

    parent.remove(node)


def _write_prepared_file(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if target.read_bytes() != content:
            raise ValueError(
                "Prepared content differs for the same source/version. "
                "Inspect the file or increment PREPARATION_VERSION."
            )
        return

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".prepared-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)

        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def prepare_html_snapshot(
    snapshot: RawSnapshot,
    http_status: int,
    *,
    project_root: Path = PROJECT_ROOT,
) -> PreparedHTML:
    """Prepare a successful HTML response; reject PDFs and failed responses."""
    if not 200 <= http_status < 300:
        raise ValueError("Only successful HTTP responses can be prepared.")

    root = Path(project_root).resolve()
    raw_directory = (
        root / RAW_SNAPSHOT_DIR.relative_to(PROJECT_ROOT)
    ).resolve()
    raw_path = (root / snapshot.raw_file_path).resolve()

    if not raw_path.is_relative_to(raw_directory):
        raise ValueError("Raw snapshot path is outside the snapshot directory.")

    content = raw_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != snapshot.content_hash:
        raise ValueError("Raw snapshot failed its integrity check.")

    content_type = (snapshot.content_type or "").lower()
    if (
        content.lstrip().startswith(b"%PDF-")
        or "application/pdf" in content_type
    ):
        raise ValueError("Use prepare_pdf_snapshot or prepare_snapshot for PDFs.")

    if content_type and not any(
        kind in content_type
        for kind in ("text/html", "application/xhtml+xml")
    ):
        raise ValueError("The response does not declare HTML content.")

    headers = {}
    if snapshot.content_type:
        headers["Content-Type"] = snapshot.content_type

    response = HtmlResponse(
        url=snapshot.source_url,
        body=content,
        headers=headers,
    )
    document = response.selector.root
    if document is None:
        raise ValueError("HTML could not be parsed.")

    # Record locations before removing nodes, keeping original source XPaths.
    original_paths = {
        node: document.getroottree().getpath(node)
        for node in document.iter()
        if _tag(node)
    }

    removable = document.xpath(
        "//script | //style | //nav | //*[@role='navigation']"
        " | //*[contains(concat(' ', normalize-space(@class), ' '), ' menu ')]"
        " | //*[contains(concat(' ', normalize-space(@class), ' '), ' main-menu ')]"
    )
    for node in removable:
        _remove_node(node)

    blocks = []
    headings = []

    def links_for(node):
        links = []
        seen = set()
        anchors = list(node.xpath(".//a[@href]"))

        if _tag(node) == "a" and node.get("href"):
            anchors.insert(0, node)

        for anchor in anchors:
            try:
                url = response.urljoin(anchor.get("href"))
                parsed = urlsplit(url)
            except ValueError:
                continue

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or url in seen
            ):
                continue

            seen.add(url)
            links.append(SourceLink(text=_read_text(anchor), url=url))

        return tuple(links)

    def add_block(node, kind, text, rows=(), *, links=None):
        text = _clean_text(text)
        if links is None:
            links = links_for(node)
        links = tuple(dict.fromkeys(links))

        if not text and not rows and not links:
            return

        blocks.append(
            ContentBlock(
                block_id=f"b{len(blocks) + 1:06d}",
                kind=kind,
                text=text,
                source_xpath=original_paths.get(node, "/"),
                heading_path=tuple(text for _, text in headings),
                links=links,
                table_rows=rows,
            )
        )

    def walk(node):
        tag = _tag(node)
        if not tag:
            return

        if tag in HEADINGS:
            text = _read_text(node)
            if text:
                level = int(tag[1])
                while headings and headings[-1][0] >= level:
                    headings.pop()

                headings.append((level, text))
                add_block(node, "heading", text)
            return

        if tag == "table":
            rows = []
            for row in node.xpath(".//tr"):
                nearest_table = row.xpath("ancestor::table[1]")
                if not nearest_table or nearest_table[0] is not node:
                    continue

                cells = tuple(
                    TableCell(
                        text=_read_text(cell),
                        is_header=_tag(cell) == "th",
                        rowspan=cell.get("rowspan"),
                        colspan=cell.get("colspan"),
                    )
                    for cell in row.xpath("./th | ./td")
                )
                if cells:
                    rows.append(cells)

            # Keep captions, footnotes and other table text, not just cells.
            add_block(node, "table", _read_text(node), tuple(rows))
            return

        if tag in TEXT_BLOCKS:
            kind = "list_item" if tag == "li" else "paragraph"
            add_block(node, kind, _read_text(node))
            return

        # Container blocks collect only links included in their own content.
        pending = [node.text or ""]
        pending_links = list(links_for(node)) if tag == "a" else []

        def flush():
            text = _clean_text("".join(pending))
            if text or pending_links:
                add_block(
                    node,
                    "text",
                    text,
                    links=tuple(pending_links),
                )

            pending.clear()
            pending_links.clear()

        for child in node:
            child_tag = _tag(child)
            if not child_tag:
                pending.append(child.tail or "")
                continue

            contains_blocks = child_tag in BLOCK_TAGS or any(
                _tag(descendant) in BLOCK_TAGS
                for descendant in child.iterdescendants()
            )
            if contains_blocks:
                flush()
                walk(child)
            elif child_tag == "br":
                pending.append("\n")
            elif child_tag == "img":
                pending.append(child.get("alt") or "")
            else:
                pending.append(_read_text_raw(child))
                pending_links.extend(links_for(child))

            pending.append(child.tail or "")

        flush()

    bodies = document.xpath("//body")
    walk(bodies[0] if bodies else document)

    if not blocks:
        raise ValueError("No readable HTML blocks were found.")

    snapshot_id = hashlib.sha256(
        (snapshot.source_url + "\0" + snapshot.content_hash).encode("utf-8")
    ).hexdigest()

    prepared_directory = (
        root / PREPARED_SNAPSHOT_DIR.relative_to(PROJECT_ROOT)
    )
    target = (
        prepared_directory / snapshot_id / f"{PREPARATION_VERSION}.json"
    )

    prepared = PreparedHTML(
        snapshot_id=snapshot_id,
        source_url=snapshot.source_url,
        content_hash=snapshot.content_hash,
        preparation_version=PREPARATION_VERSION,
        source_encoding=response.encoding,
        raw_file_path=raw_path.relative_to(root).as_posix(),
        prepared_file_path=target.relative_to(root).as_posix(),
        blocks=tuple(blocks),
    )

    serialized = (
        json.dumps(asdict(prepared), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _write_prepared_file(target, serialized)

    return prepared


def prepare_pdf_snapshot(snapshot: RawSnapshot, http_status: int, *,
                         project_root: Path = PROJECT_ROOT) -> PreparedPDF:
    """Prepare all pages of a text PDF; do not silently skip scanned pages.

    Layout text is preserved per physical page. Table cells and heading
    hierarchies are not inferred. Image-only text and OCR are not supported.
    """
    if not 200 <= http_status < 300:
        raise ValueError("Only successful HTTP responses can be prepared.")
    root = Path(project_root).resolve()
    raw_path = (root / snapshot.raw_file_path).resolve()
    if not raw_path.is_relative_to((root / RAW_SNAPSHOT_DIR.relative_to(PROJECT_ROOT)).resolve()):
        raise ValueError("Raw snapshot path is outside the snapshot directory.")
    content = raw_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != snapshot.content_hash:
        raise ValueError("Raw snapshot failed its integrity check.")
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError("Saved response does not contain a PDF header.")

    import pypdf
    from pypdf.errors import PdfReadError

    blocks, blank_pages = [], []
    try:
        reader = pypdf.PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs require separate handling and are not prepared here.")
        count = len(reader.pages)
        if not 1 <= count <= MAX_PDF_PAGES:
            raise ValueError(f"PDF page count is outside the configured 1-{MAX_PDF_PAGES} limit.")
        for number, page in enumerate(reader.pages, 1):
            stream = page.get_contents()
            stream_bytes = stream.get_data() if stream is not None else b""
            if len(stream_bytes) > MAX_PDF_PAGE_STREAM_BYTES:
                raise ValueError(f"PDF page {number} exceeds the configured content-stream size limit.")
            text = page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False) if stream_bytes.strip() else ""
            text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip("\n")
            if not text.strip():
                if stream_bytes.strip():
                    raise ValueError(f"PDF page {number} has drawing content but no extractable text. Inspect it for scanned content; OCR or visual handling is required before extraction.")
                blank_pages.append(number)
            links = []
            annotations = page.get("/Annots")
            annotations = annotations.get_object() if annotations is not None else []
            for reference in annotations:
                annotation = reference.get_object()
                action = annotation.get("/A")
                if annotation.get("/Subtype") != "/Link" or action is None:
                    continue
                action = action.get_object()
                if action.get("/S") != "/URI":
                    continue
                url = action.get("/URI")
                if not isinstance(url, str):
                    continue
                try:
                    parsed = urlsplit(url)
                    parsed.port
                except ValueError:
                    continue
                if (parsed.scheme in {"http", "https"} and parsed.hostname
                        and parsed.username is None and parsed.password is None):
                    # The URI annotation establishes a destination, not visible
                    # anchor text. Do not manufacture a link-text quotation.
                    links.append(SourceLink(text="", url=url))
            blocks.append(PDFBlock(
                block_id=f"p{number:06d}", kind="pdf_page", text=text,
                page_number=number, source_xpath=None, heading_path=(),
                links=tuple(dict.fromkeys(links)),
            ))
    except (PdfReadError, KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("PDF parsing failed; inspect the original saved document.") from error
    if not any(block.text.strip() for block in blocks):
        raise ValueError("PDF has no extractable text; OCR or visual handling may be required.")
    snapshot_id = hashlib.sha256((snapshot.source_url + "\0" + snapshot.content_hash).encode("utf-8")).hexdigest()
    # Parser upgrades get separate prepared files, preserving old evidence.
    version = f"{PDF_PREPARATION_VERSION}-pypdf-{pypdf.__version__}"
    target = root / PREPARED_SNAPSHOT_DIR.relative_to(PROJECT_ROOT) / snapshot_id / f"{version}.json"
    prepared = PreparedPDF(
        snapshot_id=snapshot_id, source_url=snapshot.source_url,
        content_hash=snapshot.content_hash, preparation_version=version,
        source_encoding=None, raw_file_path=raw_path.relative_to(root).as_posix(),
        prepared_file_path=target.relative_to(root).as_posix(), blocks=tuple(blocks),
        page_count=count, parser_version=pypdf.__version__, extraction_method="pypdf_layout_text",
        blank_pages=tuple(blank_pages), preparation_notes=(
            "Page numbers are 1-based physical PDF pages, not printed page labels.",
            "Layout text retains spacing; table cell relationships and headings are not inferred.",
            "No OCR is performed. Text extraction does not prove visual completeness; inspect tables, images and font decoding during source review.",
        ),
    )
    _write_prepared_file(target, (json.dumps(asdict(prepared), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return prepared


def prepare_snapshot(snapshot: RawSnapshot, http_status: int, *, project_root: Path = PROJECT_ROOT):
    """Route a saved response by PDF signature/type, preserving the HTML API."""
    root = Path(project_root).resolve()
    path = (root / snapshot.raw_file_path).resolve()
    if not path.is_relative_to((root / RAW_SNAPSHOT_DIR.relative_to(PROJECT_ROOT)).resolve()):
        raise ValueError("Raw snapshot path is outside the snapshot directory.")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != snapshot.content_hash:
        raise ValueError("Raw snapshot failed its integrity check.")
    if not 200 <= http_status < 300:
        raise ValueError("Only successful HTTP responses can be prepared.")
    key = (str(root), snapshot.source_url, snapshot.content_hash, snapshot.content_type)
    cached = _VERIFIED_PREPARATIONS.get(key)
    if cached is not None:
        if isinstance(cached, str):
            raise ValueError(cached)
        serialized = (json.dumps(asdict(cached), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if (root / cached.prepared_file_path).read_bytes() != serialized:
            raise ValueError("Previously prepared content changed; source preparation is not trusted.")
        return cached
    try:
        if content[:1024].lstrip().startswith(b"%PDF-") or "application/pdf" in (snapshot.content_type or "").lower():
            prepared = prepare_pdf_snapshot(snapshot, http_status, project_root=root)
        else:
            prepared = prepare_html_snapshot(snapshot, http_status, project_root=root)
    except ValueError as error:
        _VERIFIED_PREPARATIONS[key] = str(error)
        raise
    # In-memory reuse only: every reuse rechecks both raw bytes and exactly
    # the prepared bytes generated from them. No disk cache is assumed valid.
    if len(_VERIFIED_PREPARATIONS) >= 256:
        _VERIFIED_PREPARATIONS.pop(next(iter(_VERIFIED_PREPARATIONS)))
    _VERIFIED_PREPARATIONS[key] = prepared
    return prepared
