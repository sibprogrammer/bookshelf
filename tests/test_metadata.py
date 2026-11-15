from dataclasses import replace
from html import escape
from zipfile import ZipFile

import pymupdf
import pytest

from src import covers as covers_module
from src.covers import Covers
from src.indexer import Indexer
from src.metadata import Metadata, find_isbns, merge_metadata, read_metadata
from src.store import BookFile, Store

SHA256 = "a" * 64


def pdf(path, *, title="", author="", pages=3, text="Page content", xmp=None):
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page().insert_text((50, 50), text)
        document.set_metadata({"title": title, "author": author})
        if xmp:
            document.set_xml_metadata(xmp)
        document.save(path)
    return path


def epub(path, metadata):
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            """<container>
          <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>""",
        )
        archive.writestr(
            "OPS/book.opf",
            f"""
          <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
            <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{metadata}</metadata>
            <manifest><item id="page" href="page.xhtml"
              media-type="application/xhtml+xml"/></manifest>
            <spine><itemref idref="page"/></spine></package>""",
        )
        archive.writestr(
            "OPS/page.xhtml",
            """
          <html xmlns="http://www.w3.org/1999/xhtml"><head><title>Page</title></head>
          <body><p>Page content</p></body></html>""",
        )
    return path


def xmp(contents):
    return f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
      <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
        <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
          {contents}</rdf:Description></rdf:RDF></x:xmpmeta>"""


def test_pdf_reads_metadata_page_count_and_labeled_isbns(tmp_path):
    source = pdf(
        tmp_path / "Filename.pdf",
        title=" PDF title ",
        author=" PDF Author ",
        pages=4,
        text="ISBN-13: 978-0-13-235088-4\nISBN-10: 0-13-235088-2",
        xmp=xmp("""<dc:publisher>Example Press</dc:publisher>
                   <dc:date>2022-05-06</dc:date><dc:language>en</dc:language>
                   <dc:subject>Programming</dc:subject>
                   <dc:description>A useful book.</dc:description>"""),
    )
    assert read_metadata(source) == Metadata(
        title="PDF title",
        author="PDF Author",
        publisher="Example Press",
        published_date="2022-05-06",
        language="en",
        description="A useful book.",
        subjects="Programming",
        isbn="9780132350884",
        page_count=4,
    )


def test_pdf_xmp_fills_missing_fields_and_avoids_text_scan(tmp_path, monkeypatch):
    source = pdf(
        tmp_path / "Book.pdf",
        xmp=xmp("""
        <dc:title><rdf:Alt><rdf:li>XMP title</rdf:li></rdf:Alt></dc:title>
        <dc:creator><rdf:Seq><rdf:li>XMP author</rdf:li></rdf:Seq></dc:creator>
        <dc:identifier>urn:isbn:9780132350884</dc:identifier>"""),
    )

    def should_not_extract_text(*args, **kwargs):
        pytest.fail("Embedded ISBN should avoid page-text scanning")

    monkeypatch.setattr(pymupdf.Page, "get_text", should_not_extract_text)
    metadata = read_metadata(source)
    assert metadata.title == "XMP title"
    assert metadata.author == "XMP author"
    assert metadata.isbn == "9780132350884"
    assert metadata.page_count == 3


def test_epub_reads_opf_metadata_and_author_roles(tmp_path):
    source = epub(
        tmp_path / "Book.epub",
        """
        <dc:title>EPUB title</dc:title>
        <dc:creator id="author">Jane Author</dc:creator>
        <dc:creator id="illustrator">An Illustrator</dc:creator>
        <meta refines="#author" property="role">aut</meta>
        <meta refines="#illustrator" property="role">ill</meta>
        <dc:publisher>A &amp; B Press</dc:publisher><dc:date>2021</dc:date>
        <dc:language>en-GB</dc:language>
        <dc:description>
          &lt;p&gt;A &lt;b&gt;good&lt;/b&gt; book.&lt;/p&gt;</dc:description>
        <dc:subject>Software</dc:subject><dc:subject>Engineering</dc:subject>
        <dc:identifier>urn:isbn:9780201616224</dc:identifier>
        <dc:identifier>020161622X</dc:identifier>
        <dc:identifier>urn:uuid:unrelated</dc:identifier>""",
    )
    assert read_metadata(source) == Metadata(
        title="EPUB title",
        author="Jane Author",
        publisher="A & B Press",
        published_date="2021",
        language="en-GB",
        description="A good book.",
        subjects="Software; Engineering",
        isbn="9780201616224",
    )


@pytest.mark.parametrize(
    "pdf_title,pdf_author,expected_title,expected_author",
    [
        ("PDF title", "PDF author", "PDF title", "PDF author"),
        ("PDF title", None, "PDF title", "EPUB author"),
        (None, "PDF author", "EPUB title", "PDF author"),
        (None, None, "EPUB title", "EPUB author"),
    ],
)
def test_missing_fields_are_filled_individually(
    pdf_title,
    pdf_author,
    expected_title,
    expected_author,
):
    sources = [
        (
            "epub",
            Metadata(
                title="EPUB title",
                author="EPUB author",
                publisher="EPUB publisher",
                page_count=999,
            ),
        ),
        ("pdf", Metadata(title=pdf_title, author=pdf_author, page_count=42)),
    ]
    merged = merge_metadata(sources, "Filename")
    assert merged.title == expected_title
    assert merged.author == expected_author
    assert merged.publisher == "EPUB publisher"
    assert merged.page_count == 42


def test_filename_fallback_and_no_epub_page_estimate():
    metadata = merge_metadata([("epub", Metadata(page_count=999))], "A book.with.dots")
    assert metadata.title == "A book.with.dots"
    assert metadata.page_count is None
    assert merge_metadata([], "Filename").title == "Filename"


def test_isbn_checksums_and_labels():
    assert find_isbns(
        "ISBN-13: 978-0-13-235088-4; ISBN-10: 0-201-61622-x", labeled=True
    ) == [
        "9780132350884",
        "020161622X",
    ]
    assert find_isbns("ISBN: 9780132350885; ISBN: 0132350883", labeled=True) == []
    assert find_isbns("Random number 0132350882", labeled=True) == []
    assert find_isbns("ISBN: 9780132350884; ISBN: 9780132350884", labeled=True) == [
        "9780132350884",
    ]


@pytest.mark.parametrize("name", ["bad.pdf", "bad.epub"])
def test_invalid_files_have_empty_metadata(tmp_path, name):
    source = tmp_path / name
    source.write_text("not a book")
    assert read_metadata(source) == Metadata()


def test_metadata_titles_do_not_control_group_identity_or_change_ids(store):
    first = BookFile(
        "/File one.pdf",
        "pdf",
        SHA256,
        metadata=Metadata(title="Shared title", author="Author"),
    )
    second = BookFile(
        "/File two.epub", "epub", "b" * 64, metadata=Metadata(title="Shared title")
    )
    assert store.sync_files([first, second]) == 2
    original = store.books()
    first_id = original[0].id
    changed = replace(
        first, metadata=Metadata(title="A changed title", author="New author")
    )
    assert store.sync_files([changed, second]) == 2
    book = store.book(first_id)
    assert book.title == "A changed title"
    assert book.metadata.author == "New author"
    assert book.files[0].id == original[0].files[0].id
    assert store.books()[0].id == first_id
    assert Store(store.path).book(first_id) == book
    assert store.sync_files([replace(changed, metadata=Metadata()), second]) == 2
    assert store.book(first_id).title == "File one"


def test_indexing_persists_metadata_and_rebuild_reuses_covers(
    library,
    store,
    tmp_path,
    monkeypatch,
):
    pdf(library / "Filename.pdf", title="PDF title", pages=5)
    epub(
        library / "Filename.epub",
        """
        <dc:title>EPUB title</dc:title><dc:creator>EPUB author</dc:creator>
        <dc:publisher>EPUB publisher</dc:publisher>
        <dc:identifier>urn:isbn:9780201616224</dc:identifier>""",
    )
    indexer = Indexer(store, library)
    assert indexer.reindex() == 1
    book = store.books()[0]
    assert book.title == "PDF title"
    assert book.metadata.author == "EPUB author"
    assert book.metadata.publisher == "EPUB publisher"
    assert book.metadata.page_count == 5
    assert book.files[1].metadata.isbn == "9780201616224"
    image = indexer.covers.find(book.files)
    before = image.stat().st_mtime_ns

    def should_not_render(*args, **kwargs):
        pytest.fail("Existing cover should not be rendered again")

    monkeypatch.setattr(covers_module, "render_cover", should_not_render)
    rebuilt = Store(tmp_path / "rebuilt.db")
    second_indexer = Indexer(rebuilt, library)
    second_indexer.covers = Covers(indexer.covers.directory)
    assert second_indexer.reindex() == 1
    assert rebuilt.books()[0].metadata == book.metadata
    assert image.stat().st_mtime_ns == before


def test_details_show_available_metadata_safely(app, client):
    metadata = Metadata(
        title="<Book & title>",
        author="<Author>",
        publisher="A & B Press",
        published_date="2024",
        language="en",
        description="A description.",
        subjects="Programming",
        isbn="9780132350884",
        page_count=7,
    )
    app.extensions["store"].sync_files(
        [
            BookFile("/Filename.pdf", "pdf", SHA256, metadata=metadata),
            BookFile(
                "/Filename.epub",
                "epub",
                "b" * 64,
                metadata=Metadata(isbn="9780201616224"),
            ),
        ]
    )
    book = app.extensions["store"].books()[0]
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    for value in [
        "<Author>",
        "A & B Press",
        "2024",
        "A description.",
        "Programming",
        "9780132350884",
    ]:
        assert escape(value).encode() in response.data
    assert b">Pages</dt>" in response.data
    assert b">7</dd>" in response.data
    assert b">ISBN</dt>" in response.data
    assert response.data.count(b">ISBN</dt>") == 1
    assert b"9780201616224" not in response.data
    assert b">Language</dt>" not in response.data
    assert b">en</dd>" not in response.data
    assert book.metadata.language == "en"
    assert b"<Author>" not in response.data
    assert b"<Book & title>" not in response.data
    assert b">Filename</h1>" in response.data
    assert escape(metadata.title).encode() not in response.data
    assert b">Filename</h3>" in client.get("/").data
    assert b">Filename</h3>" in client.get("/library").data
    app.extensions["store"].sync_files(
        [BookFile("/Unknown.epub", "epub", "c" * 64, metadata=Metadata(language="fr"))]
    )
    unknown = app.extensions["store"].books()[0]
    response = client.get(f"/books/{unknown.id}")
    assert b">Unknown</h1>" in response.data
    assert b">Pages</dt>" not in response.data
    assert b">Author</dt>" not in response.data
    assert b'id="metadata-heading"' in response.data


def test_bad_xmp_does_not_discard_pdf_info_and_page_count(tmp_path):
    source = tmp_path / "Bad XMP.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Page content")
        document.set_metadata({"title": "Info title", "author": "Info author"})
        broken = document.get_new_xref()
        document.update_object(broken, "<< /Type /Metadata >>")
        document.xref_set_key(document.pdf_catalog(), "Metadata", f"{broken} 0 R")
        document.save(source)
    metadata = read_metadata(source)
    assert metadata.title == "Info title"
    assert metadata.author == "Info author"
    assert metadata.page_count == 1


def test_display_titles_and_sorting_use_filenames_while_metadata_is_retained(store):
    store.sync_files(
        [
            BookFile(
                "/Z filename.pdf", "pdf", SHA256, metadata=Metadata(title="A metadata")
            ),
            BookFile(
                "/A filename.pdf",
                "pdf",
                "b" * 64,
                metadata=Metadata(title="Z metadata"),
            ),
        ]
    )
    for books in [store.books(), store.recent_books()]:
        assert [book.file_title for book in books] == ["A filename", "Z filename"]
        assert [book.metadata.title for book in books] == ["Z metadata", "A metadata"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Teiva Harsanyi;", "Teiva Harsanyi"),
        (" Teiva Harsanyi; ,.!? ", "Teiva Harsanyi"),
        ("Anne-Marie O’Neill…；", "Anne-Marie O’Neill"),
        ("J. R. R. Tolkien", "J. R. R. Tolkien"),
        ("Doe, Jane.;", "Doe, Jane"),
        ("Alice; Bob;", "Alice; Bob"),
        (" ;.,: ", None),
        (None, None),
    ],
)
def test_author_names_have_no_trailing_punctuation(value, expected):
    metadata = Metadata(author=value, publisher="Example Press, Inc.")
    assert metadata.author == expected
    assert metadata.publisher == "Example Press, Inc."


def test_authors_are_cleaned_before_combining_epub_creators(tmp_path):
    source = epub(
        tmp_path / "Book.epub",
        """
        <dc:creator>Alice;</dc:creator><dc:creator>Bob,.;</dc:creator>
        <dc:creator>Alice</dc:creator>""",
    )
    assert read_metadata(source).author == "Alice; Bob"


def test_cleaned_author_is_persisted_for_pdf_and_database(tmp_path, store):
    import json

    source = pdf(tmp_path / "Book.pdf", author="Teiva Harsanyi;")
    metadata = read_metadata(source)
    assert metadata.author == "Teiva Harsanyi"
    store.sync_files([BookFile(str(source), "pdf", SHA256, metadata=metadata)])
    with store.connect() as connection:
        row = connection.execute("SELECT metadata FROM book_files").fetchone()
        assert json.loads(row["metadata"])["author"] == "Teiva Harsanyi"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0101-01-01T00:00:00+00:00; 2022-10-20", "2022-10-20"),
        ("2022-10-20T23:30:00-05:00", "2022-10-20"),
        ("2022-10-20", "2022-10-20"),
        ("2021", "2021"),
        ("2021-06", "2021-06"),
        ("2020; 2022-10-20", "2022-10-20"),
        ("2022-02-30; 2024-02-29T00:00:00Z", "2024-02-29"),
        ("0001-01-01T00:00:00Z", None),
        ("0101-01-01T00:00:00+00:00", None),
        ("2022-02-30", None),
        ("not a date", None),
        (None, None),
    ],
)
def test_publication_dates_keep_only_valid_date_values(value, expected):
    assert Metadata(published_date=value).published_date == expected


def test_publication_date_is_cleaned_before_storage_and_display(app, client):
    import json

    metadata = Metadata(published_date="0101-01-01T00:00:00+00:00; 2022-10-20")
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.epub", "epub", SHA256, metadata=metadata)])
    with store.connect() as connection:
        row = connection.execute("SELECT metadata FROM book_files").fetchone()
        assert json.loads(row["metadata"])["published_date"] == "2022-10-20"
    page = client.get(f"/books/{store.books()[0].id}")
    assert b'<time datetime="2022-10-20">20 Oct 2022</time>' in page.data
    assert b"0101-01-01" not in page.data


def test_only_first_isbn_is_persisted_and_displayed(app, client):
    import json

    metadata = Metadata(isbn="9785932862056; 9780321635365")
    assert metadata.isbn == "9785932862056"
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256, metadata=metadata)])
    with store.connect() as connection:
        row = connection.execute("SELECT metadata FROM book_files").fetchone()
        assert json.loads(row["metadata"])["isbn"] == "9785932862056"
    page = client.get(f"/books/{store.books()[0].id}")
    assert b">9785932862056</dd>" in page.data
    assert b"9780321635365" not in page.data


@pytest.mark.parametrize(
    "pdf_isbn,epub_isbn,expected",
    [
        ("9780132350884", "9780201616224", "9780132350884"),
        (None, "9780201616224", "9780201616224"),
        ("9780132350884", None, "9780132350884"),
        (None, None, None),
    ],
)
def test_book_details_show_only_first_available_isbn(
    app, client, pdf_isbn, epub_isbn, expected
):
    store = app.extensions["store"]
    store.sync_files(
        [
            BookFile("/Book.pdf", "pdf", SHA256, metadata=Metadata(isbn=pdf_isbn)),
            BookFile("/Book.epub", "epub", "b" * 64, metadata=Metadata(isbn=epub_isbn)),
        ]
    )
    response = client.get(f"/books/{store.books()[0].id}")
    assert response.status_code == 200
    assert response.data.count(b">ISBN</dt>") == (1 if expected else 0)
    if expected:
        assert f">{expected}</dd>".encode() in response.data
    assert b">PDF</button>" in response.data
    assert b">EPUB</button>" in response.data
