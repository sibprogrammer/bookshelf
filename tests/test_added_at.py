from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.indexer import Indexer, file_added_at
from src.store import BookFile, Store

SHA256 = "a" * 64


@pytest.mark.parametrize("birthtime", [0, 1_600_000_000, 1_700_000_000.125])
def test_added_date_uses_creation_time(birthtime):
    info = SimpleNamespace(st_birthtime=birthtime, st_mtime=1, st_ctime=2)
    assert file_added_at(info) == datetime.fromtimestamp(birthtime, UTC)


def test_missing_creation_time_is_unknown():
    # Modification time and inode change time do not describe file creation.
    assert file_added_at(SimpleNamespace(st_mtime=1, st_ctime=2)) is None


def test_reindex_reads_and_refreshes_creation_time(library, store, monkeypatch):
    pdf = library / "Book.pdf"
    epub = library / "Book.epub"
    pdf.write_text("PDF")
    epub.write_text("EPUB")
    birthtimes = {pdf.name: 1_600_000_000, epub.name: 1_700_000_000}
    original_lstat = Path.lstat

    def metadata(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path.name not in birthtimes:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_ino=info.st_ino,
            st_dev=info.st_dev,
            st_birthtime=birthtimes[path.name],
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    indexer = Indexer(store, library)
    assert indexer.reindex() == 1
    original = store.books()[0]
    assert [file.added_at for file in original.files] == [
        datetime.fromtimestamp(birthtimes[pdf.name], UTC),
        datetime.fromtimestamp(birthtimes[epub.name], UTC),
    ]
    pdf.write_text("Changed PDF contents")
    assert indexer.reindex() == 1
    updated = store.books()[0]
    assert updated.files[0].sha256 != original.files[0].sha256
    assert [file.added_at for file in updated.files] == [
        file.added_at for file in original.files
    ]
    birthtimes[pdf.name] += 86400
    assert indexer.reindex() == 1
    refreshed = store.books()[0]
    assert refreshed.id == original.id
    assert refreshed.files[0].id == original.files[0].id
    assert refreshed.files[0].added_at == datetime.fromtimestamp(
        birthtimes[pdf.name], UTC
    )
    assert refreshed.files[1] == original.files[1]
    assert Store(store.path).books() == store.books()


def test_dates_are_stored_in_utc_and_support_ordering_and_ranges(store):
    # These dates use different offsets and straddle midnight in UTC.
    earlier = datetime(2025, 1, 2, 1, tzinfo=timezone(timedelta(hours=2)))
    later = datetime(2025, 1, 1, 20, tzinfo=timezone(timedelta(hours=-5)))
    store.sync_files(
        [
            BookFile("/later.pdf", "pdf", SHA256, added_at=later),
            BookFile("/earlier.epub", "epub", SHA256, added_at=earlier),
            BookFile("/unknown.pdf", "pdf", SHA256),
        ]
    )
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT file_path, added_at FROM book_files "
            "WHERE added_at IS NOT NULL ORDER BY added_at"
        ).fetchall()
        assert [(row["file_path"], row["added_at"]) for row in rows] == [
            ("/earlier.epub", "2025-01-01T23:00:00.000000+00:00"),
            ("/later.pdf", "2025-01-02T01:00:00.000000+00:00"),
        ]
        selected = connection.execute(
            "SELECT file_path FROM book_files WHERE added_at >= ? AND added_at < ?",
            ("2025-01-02T00:00:00.000000+00:00", "2025-01-03T00:00:00.000000+00:00"),
        ).fetchall()
        assert [row["file_path"] for row in selected] == ["/later.pdf"]


def test_details_show_creation_date_for_each_format(app, client):
    store = app.extensions["store"]
    pdf_date = datetime(2020, 3, 4, 12, 30, 45, tzinfo=UTC)
    epub_date = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    store.sync_files(
        [
            BookFile("/Book.pdf", "pdf", SHA256, added_at=pdf_date),
            BookFile("/Book.epub", "epub", SHA256, added_at=epub_date),
            BookFile("/Unknown.pdf", "pdf", SHA256),
        ]
    )
    book, unknown = store.books()
    page = client.get(f"/books/{book.id}")
    assert page.status_code == 200
    assert page.data.count(b">Added</dt>") == 2
    assert b"04 Mar 2020" in page.data
    assert b"06 May 2024" in page.data
    assert b"12:30:45" not in page.data
    assert b"07:08:09" not in page.data
    for date in [pdf_date, epub_date]:
        assert f'<time datetime="{date.date().isoformat()}">'.encode() in page.data
    page = client.get(f"/books/{unknown.id}")
    assert page.status_code == 200
    assert b"Unavailable" in page.data
    assert b"<time " not in page.data
