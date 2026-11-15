"""Read bibliographic metadata from local PDF and EPUB files."""

import logging
import re
import unicodedata
from dataclasses import dataclass, fields
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

import pymupdf

logger = logging.getLogger(__name__)
DC = "http://purl.org/dc/elements/1.1/"
OPF = "http://www.idpf.org/2007/opf"
SEPARATOR = r"[\s\-\u00ad\u2010-\u2015]*"
ISBN_PATTERN = re.compile(
    rf"(?<!\w)(?:97[89](?:{SEPARATOR}\d){{10}}|"
    rf"\d(?:{SEPARATOR}\d){{8}}{SEPARATOR}[\dXx])(?!\w)"
)
ISBN_LABEL = re.compile(r"\b(?:e[- ]?)?ISBN(?:[- ]?(?:10|13))?\s*[:：]?\s*", re.I)


@dataclass(frozen=True)
class Metadata:
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    description: str | None = None
    subjects: str | None = None
    isbn: str | None = None
    page_count: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "author", clean_author(self.author))
        object.__setattr__(
            self, "published_date", clean_published_date(self.published_date)
        )
        object.__setattr__(self, "isbn", next(iter(find_isbns(self.isbn or "")), None))


def clean(value: str | None) -> str | None:
    return " ".join((value or "").replace("\x00", "").split()) or None


def clean_author(value: str | None) -> str | None:
    value = clean(value) or ""
    while value and (
        value[-1].isspace() or unicodedata.category(value[-1]).startswith("P")
    ):
        value = value[:-1]
    return value or None


def clean_published_date(value: str | None) -> str | None:
    candidates = []
    for candidate in re.findall(
        r"(?<!\d)\d{4}(?:-\d{2}(?:-\d{2})?)?(?![\d-])", value or ""
    ):
        parts = [int(part) for part in candidate.split("-")]
        try:
            parsed = date(
                parts[0],
                parts[1] if len(parts) > 1 else 1,
                parts[2] if len(parts) > 2 else 1,
            )
        except ValueError:
            continue
        if parsed not in {date.min, date(101, 1, 1)}:
            candidates.append(candidate)
    return max(candidates, key=len, default=None)


def joined(values) -> str | None:
    return (
        "; ".join(dict.fromkeys(value for item in values if (value := clean(item))))
        or None
    )


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def description_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = PlainText()
    parser.feed(unescape(value))
    return clean(" ".join(parser.parts))


def valid_isbn(value: str) -> bool:
    if len(value) == 13 and value.isdigit() and value.startswith(("978", "979")):
        return (
            sum(
                int(digit) * (1 if index % 2 == 0 else 3)
                for index, digit in enumerate(value)
            )
            % 10
            == 0
        )
    if len(value) == 10 and value[:9].isdigit() and value[-1] in "0123456789X":
        digits = [int(digit) if digit != "X" else 10 for digit in value]
        return sum(digit * (10 - index) for index, digit in enumerate(digits)) % 11 == 0
    return False


def find_isbns(value: str, *, labeled: bool = False) -> list[str]:
    if labeled:
        candidates = (
            ISBN_PATTERN.match(value, label.end())
            for label in ISBN_LABEL.finditer(value)
        )
    else:
        candidates = ISBN_PATTERN.finditer(value)
    found = []
    for match in candidates:
        if match is None:
            continue
        candidate = re.sub(r"[^0-9Xx]", "", match.group()).upper()
        if valid_isbn(candidate) and candidate not in found:
            found.append(candidate)
    return found


def xml_values(root: ET.Element | None, name: str) -> list[str]:
    if root is None:
        return []
    return [
        " ".join(element.itertext()) for element in root.findall(f".//{{{DC}}}{name}")
    ]


def read_pdf(path: Path) -> Metadata:
    with pymupdf.open(path) as document:
        if document.needs_pass:
            return Metadata()
        info = document.metadata or {}
        xmp = None
        try:
            raw = document.get_xml_metadata()
            if raw:
                xmp = ET.fromstring(raw)
        except ET.ParseError, RuntimeError, ValueError, pymupdf.mupdf.FzErrorBase:
            logger.warning("Unable to read XMP metadata from %s", path)
        identifiers = []
        if xmp is not None:
            for element in xmp.iter():
                local_name = element.tag.rsplit("}", 1)[-1].lower()
                if local_name in {"isbn", "isbn10", "isbn13", "identifier"}:
                    identifiers.extend(find_isbns(" ".join(element.itertext())))
        identifiers.extend(find_isbns(info.get("keywords") or "", labeled=True))
        if not identifiers:
            numbers = list(range(min(10, document.page_count)))
            numbers += [
                n for n in range(max(10, document.page_count - 3), document.page_count)
            ]
            for number in numbers:
                try:
                    text = document[number].get_text()
                except RuntimeError, ValueError, pymupdf.mupdf.FzErrorBase:
                    logger.warning(
                        "Unable to read text on page %s of %s", number + 1, path
                    )
                    continue
                identifiers.extend(find_isbns(text, labeled=True))
                if identifiers:
                    break
        return Metadata(
            title=clean(info.get("title")) or joined(xml_values(xmp, "title")),
            author=clean_author(info.get("author"))
            or joined(clean_author(value) for value in xml_values(xmp, "creator")),
            publisher=joined(xml_values(xmp, "publisher")),
            published_date=joined(xml_values(xmp, "date")),
            language=joined(xml_values(xmp, "language")),
            description=description_text(
                joined(xml_values(xmp, "description")) or info.get("subject")
            ),
            subjects=joined(xml_values(xmp, "subject")) or clean(info.get("keywords")),
            isbn=next(iter(identifiers), None),
            page_count=document.page_count,
        )


def read_epub(path: Path) -> Metadata:
    with ZipFile(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            return Metadata()
        package = ET.fromstring(archive.read(rootfile.get("full-path")))
        metadata = package.find("{*}metadata")
        if metadata is None:
            return Metadata()
        roles = {}
        for element in metadata.findall("{*}meta"):
            if element.get("property") == "role":
                roles.setdefault(
                    element.get("refines", "").removeprefix("#"), []
                ).append(clean(element.text))
        authors = []
        for element in metadata.findall(f"{{{DC}}}creator"):
            author_roles = roles.get(element.get("id"), [])
            if role := element.get(f"{{{OPF}}}role"):
                author_roles = [role]
            if not author_roles or "aut" in author_roles:
                authors.append(" ".join(element.itertext()))
        identifiers = []
        for identifier in xml_values(metadata, "identifier"):
            identifiers.extend(find_isbns(identifier))
        return Metadata(
            title=next(
                (
                    value
                    for item in xml_values(metadata, "title")
                    if (value := clean(item))
                ),
                None,
            ),
            author=joined(clean_author(value) for value in authors),
            publisher=joined(xml_values(metadata, "publisher")),
            published_date=joined(xml_values(metadata, "date")),
            language=joined(xml_values(metadata, "language")),
            description=description_text(joined(xml_values(metadata, "description"))),
            subjects=joined(xml_values(metadata, "subject")),
            isbn=next(iter(identifiers), None),
        )


def read_metadata(path: Path) -> Metadata:
    try:
        return read_pdf(path) if path.suffix.lower() == ".pdf" else read_epub(path)
    except (
        RuntimeError,
        ValueError,
        ET.ParseError,
        BadZipFile,
        KeyError,
        pymupdf.mupdf.FzErrorBase,
    ) as error:
        logger.warning("Metadata unavailable for %s: %s", path, error)
        return Metadata()


def merge_metadata(
    sources: list[tuple[str, Metadata]], fallback_title: str
) -> Metadata:
    ordered = sorted(sources, key=lambda source: source[0] != "pdf")
    values = {}
    for item in fields(Metadata):
        candidates = [
            metadata
            for extension, metadata in ordered
            if item.name != "page_count" or extension == "pdf"
        ]
        values[item.name] = next(
            (
                value
                for metadata in candidates
                if (value := getattr(metadata, item.name)) is not None
            ),
            None,
        )
    values["title"] = values["title"] or fallback_title
    return Metadata(**values)
