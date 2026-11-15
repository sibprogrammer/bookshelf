"""Render and cache one 333 x 500 PNG cover for each logical book."""

import hashlib
import logging
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .store import BookFile, check_cancelled

SIZE = (333, 500)
logger = logging.getLogger(__name__)


class CoverError(Exception):
    """The book has no readable, non-empty page."""


def preferred_sources(files: list[BookFile] | tuple[BookFile, ...]) -> list[BookFile]:
    return sorted(files, key=lambda file: (file.extension != "pdf", file.file_path))


def render_cover(path: Path, cancel: Event | None = None) -> Image.Image:
    """Render the first visibly non-white page, including image-only pages."""
    check_cancelled(cancel)
    before = path.stat()
    try:
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
                    scale = min(
                        2 * SIZE[0] / page.rect.width, 2 * SIZE[1] / page.rect.height
                    )
                    pixmap = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale),
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    image = Image.frombytes(
                        "RGB", (pixmap.width, pixmap.height), pixmap.samples
                    )
                    ink = image.convert("L").point(
                        lambda value: 255 if value < 250 else 0
                    )
                    if ink.getbbox() is None:
                        continue
                    fitted = ImageOps.contain(image, SIZE, Image.Resampling.LANCZOS)
                    cover = Image.new("RGB", SIZE, "white")
                    cover.paste(
                        fitted,
                        (
                            (SIZE[0] - fitted.width) // 2,
                            (SIZE[1] - fitted.height) // 2,
                        ),
                    )
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
    raise CoverError("Book has no non-empty pages")


class Covers:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, file: BookFile) -> Path:
        key = hashlib.sha256(
            f"v1:{SIZE[0]}x{SIZE[1]}:{file.extension}:{file.sha256}".encode()
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
        return next(
            (
                self.path(file)
                for file in preferred_sources(files)
                if self._valid(self.path(file))
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
