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

Open <http://127.0.0.1:5000> and click **Index / reindex books**. The app starts with the previously saved index, or an empty table on the first run. Indexing is synchronous and may take a while for large collections.

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

- Only regular files with `.pdf` or `.epub` extensions are indexed, case-insensitively. File contents are not parsed or validated as book formats.
- Nested directories are scanned. Symlinks inside the tree are skipped. A symlink used as `books_dir` itself is resolved before each scan.
- A book has a title (the filename without its final extension), a directory, and one or more files. Files with the same title in the same directory are grouped together: `Book.pdf` and `Book.epub` appear as one book with two formats. Titles match exactly, including case; extensions are case-insensitive. Identical titles in different directories remain separate books.
- Each file stores its format (`pdf` or `epub`), absolute path, and SHA-256 (lowercase hex). Books and files have separate IDs. Duplicate contents do not cause unrelated titles or directories to be merged.
- Every scan recomputes SHA-256 by streaming file contents. Existing book and file IDs are preserved; adding or removing a format keeps the book's ID while at least one format remains. Books without files are removed. A successful scan of an empty directory clears the index. Counts refer to books, rather than individual files.
- Scan errors, missing directories, cancellation, or database errors leave the previous index intact. Changes are committed in one transaction after the entire scan succeeds. Overlapping scans in the app process are rejected while one is running.
- The table lists titles alphabetically without extensions; hover over a title to see its directory. Each row has an open button for every available format. Click a title for a details page with a separate path, SHA-256, and **Open PDF locally** or **Open EPUB locally** action for each file.
- Format buttons open the selected file in the default reader on the computer running Bookshelf, then return you to the book's details page. macOS uses `open`, Windows uses `os.startfile`, and Linux requires `xdg-open` and a desktop session. No author or cover metadata is collected yet.
- Index and open actions accept POST requests only. Cross-origin browser requests are rejected before performing either action. Filenames and paths are escaped in templates.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check
uv run ruff format --check
```

The Flask application factory and routes are in `src/main.py`; `src/__init__.py` only marks the package. Jinja templates are in `src/templates`, and static assets are in `src/static`. Bootstrap's version, download sources, and license are recorded in `src/static/vendor/bootstrap`.

To update Python dependencies, run `uv lock --upgrade` and rerun the checks. Commit `uv.lock` for reproducible installs.
