"""Bounded PDF, DOCX and plain-text uploads with versioned local storage."""

import os
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from docx import Document
from docx.oxml.exceptions import InvalidXmlError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_RESUME_CHARACTERS = 100000
MAX_PDF_PAGES = 20
MAX_EXPANDED_BYTES = 10 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000


def extract_pdf(content):
    if not content.startswith(b"%PDF-"):
        raise ValueError("This file is not a valid PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported; upload an unencrypted copy")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF resumes must contain at most 20 pages")
        pieces = []
        total = 0
        stream_bytes = 0
        for page in reader.pages:
            stream = page.get_contents()
            if stream is not None:
                stream_bytes += len(stream.get_data())
                if stream_bytes > MAX_EXPANDED_BYTES:
                    raise ValueError("PDF content is too large to safely extract")
            text = page.extract_text() or ""
            if not text.strip():
                raise ValueError("A PDF page has no readable text; scanned/image-only pages need OCR. Upload a text-based PDF or DOCX")
            total += len(text) + 1
            if total > MAX_RESUME_CHARACTERS:
                raise ValueError("Extracted resume text exceeds 100,000 characters")
            pieces.append(text)
    except (PyPdfError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError("Could not read this PDF; upload a valid, unencrypted text-based PDF") from exc
    return validate_text("\n".join(pieces))


def extract_docx(content):
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded DOCX contents are too large to safely extract")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("Encrypted Word documents are not supported")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("This file is not a valid DOCX document")
        document = Document(BytesIO(content))
        pieces = []
        total = 0

        def collect(container, depth=0):
            nonlocal total
            if depth > 10:
                raise ValueError("DOCX tables are too deeply nested")
            for block in container.iter_inner_content():
                if isinstance(block, Paragraph):
                    total += len(block.text) + 1
                    if total > MAX_RESUME_CHARACTERS:
                        raise ValueError("Extracted resume text exceeds 100,000 characters")
                    pieces.append(block.text)
                elif isinstance(block, Table):
                    for row in block.rows:
                        for cell in row.cells:
                            collect(cell, depth + 1)

        collect(document)
        seen_parts = set()
        for section in document.sections:
            for container in (section.header, section.first_page_header, section.even_page_header,
                              section.footer, section.first_page_footer, section.even_page_footer):
                if container.part.partname not in seen_parts:
                    seen_parts.add(container.part.partname)
                    collect(container)
    except (BadZipFile, XMLSyntaxError, InvalidXmlError, KeyError, TypeError, NotImplementedError, RecursionError) as exc:
        raise ValueError("Could not read this Word file; upload a valid .docx document") from exc
    return validate_text("\n".join(pieces))


def read_upload(upload):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in (".pdf", ".docx", ".txt"):
        raise ValueError("Upload a PDF, DOCX, or UTF-8 TXT resume, or paste its text below")
    content = upload.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Resume files must be at most 2 MB")
    if suffix == ".pdf":
        text = extract_pdf(content)
    elif suffix == ".docx":
        text = extract_docx(content)
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("The text file must use UTF-8 encoding") from exc
    return validate_text(text), content, suffix


def validate_text(text):
    text = text.strip()
    if not text or len(text) > MAX_RESUME_CHARACTERS or "\x00" in text:
        raise ValueError("Provide nonempty resume text of at most 100,000 characters, without NUL bytes")
    return text


def save_version(conn, user, text, source_name, now, upload_dir, content=None, suffix=None):
    text = validate_text(text)
    if text == user["resume_text"]:
        return False
    file_name = None
    destination = None
    if content is not None:
        upload_dir = Path(upload_dir).resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_name = uuid.uuid4().hex + suffix
        destination = upload_dir / file_name
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=upload_dir, delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    try:
        with conn:
            timestamp = now.isoformat()
            conn.execute("INSERT INTO resume_versions(user_id,resume_text,source_name,file_name,created_at) VALUES(?,?,?,?,?)",
                         (user["id"], text, source_name, file_name, timestamp))
            conn.execute("UPDATE users SET resume_text=?,resume_updated_at=? WHERE id=?", (text, timestamp, user["id"]))
            conn.execute("UPDATE scores SET is_stale=1 WHERE user_id=?", (user["id"],))
    except BaseException:
        # Remove only the newly created upload if its DB transaction did not succeed.
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    return True
