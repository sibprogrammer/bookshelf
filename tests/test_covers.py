import hashlib
from io import BytesIO
from threading import Event
from zipfile import ZIP_STORED, ZipFile

import pymupdf
import pytest
from PIL import Image

from src import covers as covers_module
from src.covers import CoverError, Covers, render_cover
from src.indexer import Indexer, hash_file
from src.store import BookFile, ScanCancelled


def make_pdf(path, color=(1, 0, 0), *, blanks=0, size=(600, 1000)):
    with pymupdf.open() as document:
        for _ in range(blanks):
            document.new_page(width=size[0], height=size[1])
        page = document.new_page(width=size[0], height=size[1])
        if color is not None:
            page.draw_rect(page.rect, color=color, fill=color)
        document.save(path)
    return path


def make_epub(path, *, page_body=None, image_size=(300, 500)):
    image = BytesIO()
    Image.new("RGB", image_size, "red").save(image, "PNG")
    declared = BytesIO()
    Image.new("RGB", (300, 500), "blue").save(declared, "PNG")
    files = {
        "META-INF/container.xml": """<?xml version="1.0"?>
        <container version="1.0"
          xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles><rootfile full-path="OPS/book.opf"
          media-type="application/oebps-package+xml"/></rootfiles>
        </container>""",
        "OPS/book.opf": """<?xml version="1.0"?>
        <package xmlns="http://www.idpf.org/2007/opf" version="3.0"
          unique-identifier="id">
          <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
            <dc:identifier id="id">urn:uuid:cover-test</dc:identifier>
            <dc:title>Cover test</dc:title><dc:language>en</dc:language>
          </metadata>
          <manifest>
            <item id="blank" href="blank.xhtml" media-type="application/xhtml+xml"/>
            <item id="page" href="page.xhtml" media-type="application/xhtml+xml"/>
            <item id="red" href="red.png" media-type="image/png"/>
            <item id="blue" href="blue.png" media-type="image/png"
              properties="cover-image"/>
          </manifest>
          <spine><itemref idref="blank"/><itemref idref="page"/></spine>
        </package>""",
        "OPS/blank.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml">
          <head><title>Blank</title></head><body></body></html>""",
        "OPS/page.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml">
          <head><title>First non-empty page</title></head>
          <body><img src="red.png" width="300" height="500" alt="Red page"/>
          </body></html>""",
        "OPS/red.png": image.getvalue(),
        "OPS/blue.png": declared.getvalue(),
    }
    if page_body is not None:
        files["OPS/page.xhtml"] = f"""<html xmlns="http://www.w3.org/1999/xhtml">
          <head><title>First non-empty page</title></head>
          <body>{page_body}</body></html>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_pdf_skips_blank_pages_and_fits_without_cropping(tmp_path):
    source = make_pdf(tmp_path / "landscape.pdf", blanks=2, size=(1000, 600))
    image = render_cover(source)
    assert image.size == (333, 500)
    assert image.mode == "RGB"
    assert image.getpixel((166, 250)) == (255, 0, 0)
    assert image.getpixel((166, 0)) == (255, 255, 255)
    assert image.getpixel((166, 499)) == (255, 255, 255)
    assert image.getpixel((0, 250)) == (255, 0, 0)
    assert image.getpixel((332, 250)) == (255, 0, 0)


def test_image_only_page_counts_as_non_empty(tmp_path):
    source = tmp_path / "scanned.pdf"
    picture = BytesIO()
    Image.new("RGB", (300, 500), "blue").save(picture, "PNG")
    with pymupdf.open() as document:
        document.new_page()
        page = document.new_page(width=300, height=500)
        page.insert_image(page.rect, stream=picture.getvalue())
        document.save(source)
    assert render_cover(source).getpixel((166, 250)) == (0, 0, 255)


def test_epub_uses_first_non_empty_page_in_reading_order(tmp_path):
    source = make_epub(tmp_path / "book.epub")
    image = render_cover(source)
    assert image.size == (333, 500)
    assert image.getpixel((166, 250)) == (255, 0, 0)


@pytest.mark.parametrize("style", ["height:90vh", "width:60px;height:72px"])
def test_epub_image_only_cover_uses_artwork_instead_of_css_size(tmp_path, style):
    source = make_epub(
        tmp_path / "book.epub",
        page_body=f'<img src="red.png" style="{style}"/>',
        image_size=(750, 900),
    )
    image = render_cover(source)
    assert image.size == (333, 500)
    assert image.getpixel((0, 250)) == (255, 0, 0)
    assert image.getpixel((332, 250)) == (255, 0, 0)
    assert image.getpixel((166, 51)) == (255, 0, 0)
    assert image.getpixel((166, 448)) == (255, 0, 0)
    assert image.getpixel((166, 0)) == (255, 255, 255)
    assert image.getpixel((166, 499)) == (255, 255, 255)


def test_epub_page_with_text_and_image_keeps_page_layout(tmp_path):
    source = make_epub(
        tmp_path / "book.epub",
        page_body='<p>Book title</p><img src="red.png" width="60" height="100"/>',
    )
    image = render_cover(source)
    assert image.getpixel((166, 250)) == (255, 255, 255)
    assert image.convert("L").point(lambda value: 255 if value < 100 else 0).getbbox()
    assert any(red > 200 and green < 50 for red, green, _ in image.get_flattened_data())


def test_reindex_replaces_old_epub_cover_cache(library, store):
    source = make_epub(library / "Book.epub")
    indexer = Indexer(store, library)
    old_key = hashlib.sha256(
        f"v1:333x500:epub:{hash_file(source)}".encode()
    ).hexdigest()
    indexer.covers.directory.mkdir()
    old_cover = indexer.covers.directory / f"{old_key}.png"
    Image.new("RGB", (333, 500), "blue").save(old_cover)
    indexer.reindex()
    cover = indexer.covers.find(store.books()[0].files)
    assert cover != old_cover
    assert not old_cover.exists()
    with Image.open(cover) as image:
        assert image.getpixel((166, 250)) == (255, 0, 0)


@pytest.mark.parametrize("kind", ["blank", "invalid", "encrypted"])
def test_unrenderable_books_report_cover_error(tmp_path, kind):
    source = tmp_path / "book.pdf"
    if kind == "invalid":
        source.write_text("not a PDF")
    elif kind == "blank":
        make_pdf(source, color=None, blanks=2)
    else:
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Secret")
            document.save(
                source,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner",
                user_pw="secret",
            )
    with pytest.raises(CoverError):
        render_cover(source)


def test_indexing_prefers_pdf_caches_and_serves_png(app, client, library, monkeypatch):
    pdf = make_pdf(library / "Book.pdf", (0, 0, 1))
    make_epub(library / "Book.epub")
    response = client.post("/index")
    assert response.status_code == 303
    indexer = app.extensions["indexer"]
    assert indexer.cover_errors == []
    book = app.extensions["store"].books()[0]
    path = indexer.covers.find(book.files)
    assert path.parent == indexer.store.path.parent / "covers"
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (333, 500)
        assert image.getpixel((166, 250)) == (0, 0, 255)
    assert len(list(path.parent.glob("*.png"))) == 1
    response = client.get(f"/books/{book.id}")
    assert f'src="/books/{book.id}/cover"'.encode() in response.data
    assert f'alt="Cover of {book.file_title}"'.encode() in response.data
    response = client.get(f"/books/{book.id}/cover")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == path.read_bytes()
    assert (
        client.get(
            f"/books/{book.id}/cover",
            headers={
                "If-None-Match": response.headers["ETag"],
            },
        ).status_code
        == 304
    )
    before = path.stat().st_mtime_ns

    def should_not_render(*args):
        pytest.fail("An unchanged cover should be cached")

    monkeypatch.setattr(covers_module, "render_cover", should_not_render)
    assert indexer.reindex() == 1
    assert path.stat().st_mtime_ns == before
    assert indexer.covers.path(book.files[0]) == path
    assert pdf.exists()


def test_pdf_added_after_epub_replaces_cover_and_removing_it_restores_epub(
    library, store
):
    make_epub(library / "Book.epub")
    indexer = Indexer(store, library)
    indexer.reindex()
    book_id = store.books()[0].id
    epub_cover = indexer.covers.find(store.books()[0].files)
    make_pdf(library / "Book.pdf", (0, 0, 1))
    indexer.reindex()
    assert store.books()[0].id == book_id
    pdf_cover = indexer.covers.find(store.books()[0].files)
    assert pdf_cover != epub_cover
    assert not epub_cover.exists()
    with Image.open(pdf_cover) as image:
        assert image.getpixel((166, 250)) == (0, 0, 255)
    (library / "Book.pdf").unlink()
    indexer.reindex()
    assert indexer.covers.find(store.books()[0].files) == epub_cover
    assert not pdf_cover.exists()
    with Image.open(epub_cover) as image:
        assert image.getpixel((166, 250)) == (255, 0, 0)


def test_blank_pdf_falls_back_to_epub_and_bad_cache_is_repaired(library, store):
    make_pdf(library / "Book.pdf", color=None)
    make_epub(library / "Book.epub")
    indexer = Indexer(store, library)
    indexer.reindex()
    assert indexer.cover_errors == []
    book = store.books()[0]
    cover = indexer.covers.find(book.files)
    assert cover == indexer.covers.path(book.files[1])
    indexer.covers.path(book.files[0]).write_bytes(b"corrupt cached PDF cover")
    assert indexer.covers.find(book.files) == cover
    cover.write_bytes(b"corrupt PNG")
    assert indexer.covers.find(book.files) is None
    indexer.reindex()
    with Image.open(cover) as image:
        assert image.size == (333, 500)
        assert image.getpixel((166, 250)) == (255, 0, 0)


def test_changed_pdf_gets_new_cover_and_deleted_book_covers_are_pruned(library, store):
    pdf = make_pdf(library / "Book.pdf")
    indexer = Indexer(store, library)
    indexer.reindex()
    original = indexer.covers.find(store.books()[0].files)
    pdf.unlink()
    make_pdf(pdf, (0, 0, 1))
    indexer.reindex()
    changed = indexer.covers.find(store.books()[0].files)
    assert original != changed
    assert not original.exists()
    other = indexer.covers.directory / "user-file.png"
    other.write_text("unmanaged")
    pdf.unlink()
    indexer.reindex()
    assert not changed.exists()
    assert other.exists()


def test_unrenderable_cover_does_not_discard_book_and_shows_warning(
    app, client, library
):
    (library / "Book.pdf").write_text("invalid book")
    response = client.post("/index", follow_redirects=True)
    assert response.status_code == 200
    assert b"Covers unavailable for 1 book(s)" in response.data
    book = app.extensions["store"].books()[0]
    response = client.get(f"/books/{book.id}")
    assert b"Cover unavailable" in response.data
    assert b"<img " not in response.data
    assert client.get(f"/books/{book.id}/cover").status_code == 404
    assert client.get("/books/999999/cover").status_code == 404
    assert client.get("/books/0/cover").status_code == 404


def test_cancelled_render_leaves_no_partial_image(tmp_path):
    source = make_pdf(tmp_path / "Book.pdf")
    cache = Covers(tmp_path / "covers")
    cancel = Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        cache.ensure([BookFile(str(source), "pdf", hash_file(source))], cancel)
    assert not list(cache.directory.glob("*"))


def test_cancellation_after_png_write_does_not_publish_partial_cover(
    tmp_path, monkeypatch
):
    source = make_pdf(tmp_path / "Book.pdf")
    cache = Covers(tmp_path / "covers")
    cancel = Event()
    original_save = Image.Image.save

    def save_and_cancel(image, *args, **kwargs):
        original_save(image, *args, **kwargs)
        cancel.set()

    monkeypatch.setattr(Image.Image, "save", save_and_cancel)
    with pytest.raises(ScanCancelled):
        cache.ensure([BookFile(str(source), "pdf", hash_file(source))], cancel)
    assert list(cache.directory.iterdir()) == []


def test_failed_commit_preserves_previous_index_and_cover(library, store, monkeypatch):
    pdf = make_pdf(library / "Book.pdf")
    indexer = Indexer(store, library)
    indexer.reindex()
    books = store.books()
    original_cover = indexer.covers.find(books[0].files)
    original_bytes = original_cover.read_bytes()
    pdf.unlink()
    make_pdf(pdf, (0, 0, 1))

    def fail(*args):
        raise OSError("database unavailable")

    monkeypatch.setattr(store, "sync_files", fail)
    with pytest.raises(OSError):
        indexer.reindex()
    assert store.books() == books
    assert indexer.covers.find(books[0].files) == original_cover
    assert original_cover.read_bytes() == original_bytes
