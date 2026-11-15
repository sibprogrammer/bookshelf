# Bookshelf

A small Python web app that recursively indexes a local directory of PDF and EPUB books in SQLite. Built with Flask and Bootstrap 5.3.9, with dependencies managed by uv.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
cp config.json.sample config.json
```

Skip the copy if you already have a `config.json`. Edit `books_dir` to point to your book directory, then start the app:

```sh
uv run flask --app src.main run
```

Open <http://127.0.0.1:5000>. **Home** shows up to 20 recently added books as cover cards linking to their details. Open **Library** for the full collection and click **Index / reindex books** to scan your books directory. The app starts with the previously saved index, or an empty table on the first run. Indexing is synchronous and may take a while for large collections.

uv provisions Python 3.14 using `.python-version`; Python 3.14 or newer is required. No Go toolchain, C compiler, Node, or frontend build is needed. Bootstrap's compiled CSS and JavaScript are bundled locally, so the UI works offline after dependencies are installed.

Use another config file with:

```sh
BOOKSHELF_CONFIG=/path/to/config.json uv run flask --app src.main run
```

Set the server address with Flask's `--host` and `--port` options.

The bundled server is intended for a local, single-process app. Keep the default loopback address: Bookshelf has no login and opens files on the server computer.

## Configuration

See [`config.json.sample`](config.json.sample):

| Key | Purpose | Default |
| --- | --- | --- |
| `books_dir` | Directory to scan recursively | Required |
| `database_path` | SQLite database file; its parent directory is created automatically | `./data/bookshelf.db` |

Relative paths resolve against the config file's directory. Unknown keys, invalid types, and empty values are rejected. `config.json`, local books, and database files are gitignored.

## Index behavior

- Only regular files with `.pdf` or `.epub` extensions are indexed, case-insensitively. Cover rendering attempts to read their contents; unreadable books remain indexed with an unavailable cover.
- Nested directories are scanned. Symlinks inside the tree are skipped. A symlink used as `books_dir` itself is resolved before each scan.
- A book has a title (the filename without its final extension), a directory, and one or more files. Files with the same title in the same directory are grouped together: `Book.pdf` and `Book.epub` appear as one book with two formats. Titles match exactly, including case; extensions are case-insensitive. Identical titles in different directories remain separate books.
- Each file stores its format (`pdf` or `epub`), absolute path, SHA-256 (lowercase hex), and `added_at`: the file's creation timestamp on disk, normalized to UTC. It is read from filesystem birth time and refreshed on each scan. Filesystems without birth time store `NULL` and display **Unavailable**. Books and files have separate IDs. Duplicate contents do not cause unrelated titles or directories to be merged.
- **Home** (`/`) shows recent books as cover cards, newest first, using the latest file creation timestamp for each book. Each card links to the details page; a book with both formats appears once. The responsive grid shows 20 books at five or four cards per row, or 18 books at three per row to complete every row. Smaller screens use one or two cards per row. Books without a creation timestamp sort last; ties sort alphabetically by title and then directory.
- Every scan recomputes SHA-256 by streaming file contents. Existing book and file IDs are preserved; adding or removing a format keeps the book's ID while at least one format remains. Books without files are removed. A successful scan of an empty directory clears the index. Counts refer to books, rather than individual files.
- Scan errors, missing directories, cancellation, or database errors leave the previous index intact. Changes are committed in one transaction after the entire scan succeeds. Overlapping scans in the app process are rejected while one is running.
- **Library** (`/library`) lists all titles alphabetically without extensions and provides the reindex action. Hover over a title to see its directory. Each library row has an open button for every available format. Click a title for a details page with the book's cover and a separate creation date, path, SHA-256, and **Open PDF locally** or **Open EPUB locally** action for each file.
- Format buttons open the selected file in the default reader on the computer running Bookshelf, then return you to the book's details page. macOS uses `open`, Windows uses `os.startfile`, and Linux requires `xdg-open` and a desktop session. No author metadata is collected yet.
- Index and open actions accept POST requests only. Cross-origin browser requests are rejected before performing either action. Filenames and paths are escaped in templates.

## Cover images

Indexing generates a **333 × 500 pixel PNG** for each book in `data/covers` (a `covers` directory beside the configured database). PyMuPDF renders PDF and EPUB pages in-process; Pillow resizes them without cropping or stretching and centers them on a white canvas. No external rendering programs are needed.

PDF is preferred when both formats exist. The first visually non-empty page is used, including scanned or image-only pages; leading blank pages are skipped. EPUB pages are read in spine order. If the PDF cannot supply a cover, the EPUB is tried. Password-protected, invalid, or entirely blank books show **Cover unavailable**; the Library shows a warning and the server logs contain the reasons.

Covers are cached by source format and SHA-256. Unchanged covers are reused, and identical source contents can share an image. Images are written atomically; unused generated PNGs are removed after a successful index commit. Changed files or a change in available formats select the appropriate new cover. The details page serves the cached image without rendering the book again.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check
uv run ruff format --check
```

The Flask application factory and routes are in `src/main.py`; `src/__init__.py` only marks the package. Jinja templates are in `src/templates`, and static assets are in `src/static`. Bootstrap's version, download sources, and license are recorded in `src/static/vendor/bootstrap`.

To update Python dependencies, run `uv lock --upgrade` and rerun the checks. Commit `uv.lock` for reproducible installs.
