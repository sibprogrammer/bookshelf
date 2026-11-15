"""Render and cache one 333 x 500 PNG cover for each logical book."""

import hashlib
import logging
import os
import posixpath
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .store import BookFile, check_cancelled

SIZE = (333, 500)
logger = logging.getLogger(__name__)


class CoverError(Exception):
    """The book has no readable, non-empty page."""


def preferred_sources(files: list[BookFile] | tuple[BookFile, ...]) -> list[BookFile]:
    return sorted(files, key=lambda file: (file.extension != "pdf", file.file_path))


def _artwork(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as source:
        artwork = source.convert("RGBA")
    image = Image.new("RGB", artwork.size, "white")
    image.paste(artwork, mask=artwork.getchannel("A"))
    return image


def _non_empty(image: Image.Image) -> bool:
    return (
        image.convert("L").point(lambda value: 255 if value < 250 else 0).getbbox()
        is not None
    )


def _epub_cover(path: Path, cancel: Event | None) -> Image.Image | None:
    """Read EPUB 3 cover-image or EPUB 2 cover metadata, outside the spine too."""
    try:
        with ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None or not (opf := rootfile.get("full-path")):
                return None
            package = ET.fromstring(archive.read(opf))
            items = package.findall("{*}manifest/{*}item")
            cover_ids = {
                meta.get("content")
                for meta in package.findall("{*}metadata/{*}meta")
                if meta.get("name") == "cover" and meta.get("content")
            }
            candidates = [
                item
                for item in items
                if "cover-image" in item.get("properties", "").split()
            ]
            candidates.extend(item for item in items if item.get("id") in cover_ids)
            for item in candidates:
                check_cancelled(cancel)
                href = urlsplit(item.get("href", ""))
                if href.scheme or href.netloc or not href.path:
                    continue
                name = posixpath.normpath(
                    posixpath.join(posixpath.dirname(opf), unquote(href.path))
                )
                try:
                    image = _artwork(archive.read(name))
                    if _non_empty(image):
                        return image
                except KeyError, OSError, ValueError:
                    continue  # A broken cover declaration must not hide readable pages.
    except BadZipFile, ET.ParseError, KeyError, RuntimeError, ValueError:
        pass
    return None


def _page_image(page: pymupdf.Page, reflowable: bool) -> Image.Image:
    if reflowable:
        blocks = page.get_text("dict")["blocks"]
        if len(blocks) == 1 and blocks[0]["type"] == 1:
            # EPUB CSS can shrink a full-size cover to a tiny image on the page.
            # Use its original pixels, preserving the first page in reading order.
            try:
                return _artwork(blocks[0]["image"])
            except OSError, ValueError:
                pass  # Fall back to rendering if the embedded image cannot be read.
    scale = min(2 * SIZE[0] / page.rect.width, 2 * SIZE[1] / page.rect.height)
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _first_page_image(path: Path, cancel: Event | None) -> Image.Image:
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise CoverError("Book is password protected")
        if document.is_reflowable:
            document.layout(width=SIZE[0] * 1.5, height=SIZE[1] * 1.5, fontsize=11)
        for chapter in range(document.chapter_count):
            for number in range(document.chapter_page_count(chapter)):
                check_cancelled(cancel)
                page = document.load_page((chapter, number))
                if page.rect.is_empty or page.rect.is_infinite:
                    continue
                image = _page_image(page, document.is_reflowable)
                if not _non_empty(image):
                    continue
                return image
    raise CoverError("Book has no non-empty pages")


def render_cover(
    path: Path, cancel: Event | None = None, *, size: tuple[int, int] = SIZE
) -> Image.Image:
    """Use the declared EPUB artwork, falling back to the first non-empty page."""
    check_cancelled(cancel)
    before = path.stat()
    try:
        image = _epub_cover(path, cancel) if path.suffix.lower() == ".epub" else None
        if image is None:
            image = _first_page_image(path, cancel)
        fitted = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
        cover = Image.new("RGB", size, "white")
        cover.paste(
            fitted,
            ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2),
        )
        check_cancelled(cancel)
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise OSError(f"File changed while rendering cover: {path}")
        return cover
    except (RuntimeError, ValueError) as error:
        raise CoverError(f"Unable to render book: {error}") from error


class Covers:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, file: BookFile) -> Path:
        version = "v3" if file.extension == "epub" else "v1"
        return self._path(file, version)

    def _path(self, file: BookFile, version: str) -> Path:
        key = hashlib.sha256(
            f"{version}:{SIZE[0]}x{SIZE[1]}:{file.extension}:{file.sha256}".encode()
        ).hexdigest()
        return self.directory / f"{key}.png"

    @staticmethod
    def _valid(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                if image.format != "PNG" or image.size != SIZE:
                    return False
                image.verify()
            return True
        except OSError, ValueError, UnidentifiedImageError:
            return False

    def find(self, files: tuple[BookFile, ...]) -> Path | None:
        # Keep existing thumbnails visible until reindexing upgrades the cache.
        # ensure() still checks only the current version, so it refreshes old covers.
        return next(
            (
                path
                for file in preferred_sources(files)
                for version in (
                    ("v3", "v2", "v1") if file.extension == "epub" else ("v1",)
                )
                if self._valid(path := self._path(file, version))
            ),
            None,
        )

    def ensure(self, files: list[BookFile], cancel: Event | None = None) -> Path:
        """Prefer PDF, falling back to EPUB if the PDF cannot supply a cover."""
        self.directory.mkdir(parents=True, exist_ok=True)
        failures = []
        for file in preferred_sources(files):
            check_cancelled(cancel)
            target = self.path(file)
            if self._valid(target):
                return target
            try:
                image = render_cover(Path(file.file_path), cancel)
            except CoverError as error:
                failures.append(f"{file.file_path}: {error}")
                continue
            temporary = None
            try:
                with NamedTemporaryFile(
                    dir=self.directory, suffix=".tmp", delete=False
                ) as output:
                    temporary = Path(output.name)
                    image.save(output, format="PNG")
                check_cancelled(cancel)
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return target
        raise CoverError("; ".join(failures))

    def prune(self, active: set[str]) -> None:
        if not self.directory.exists():
            return
        try:
            paths = list(self.directory.iterdir())
        except OSError:
            logger.warning("Unable to list cover cache", exc_info=True)
            return
        for path in paths:
            if (
                re.fullmatch(r"[0-9a-f]{64}\.png", path.name)
                and path.name not in active
            ):
                try:
                    path.unlink()
                except OSError:
                    logger.warning(
                        "Unable to remove unused cover %s", path, exc_info=True
                    )
