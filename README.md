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

## Existing installations

The Python rewrite reads the original SQLite schema directly; saved book IDs, paths, and hashes are preserved. Configuration contains only `books_dir` and `database_path`; server options belong to Flask. Stop the old server before starting Python on the same port. No automatic scan runs at startup.

## Index behavior

- Only regular files with `.pdf` or `.epub` extensions are indexed, case-insensitively. File contents are not parsed or validated as book formats.
- Nested directories are scanned. Symlinks inside the tree are skipped. A symlink used as `books_dir` itself is resolved before each scan.
- Each row stores `id`, `file_name`, `file_path` (absolute), and `sha256` (lowercase hex). Duplicate contents at different paths remain separate entries.
- Every scan recomputes SHA-256 by streaming file contents. Existing paths are updated without changing their IDs; new files are added and missing files are removed. A successful scan of an empty directory clears the index.
- Scan errors, missing directories, cancellation, or database errors leave the previous index intact. Changes are committed in one transaction after the entire scan succeeds. Overlapping scans in the app process are rejected while one is running.
- The table lists file names alphabetically; hover over a name to see its full path. Click a file name for its details page, including the absolute path, SHA-256, and **Open file locally** action.
- The open action and each table row's icon open the book in the default reader on the computer running Bookshelf, then return you to its details page. macOS uses `open`, Windows uses `os.startfile`, and Linux requires `xdg-open` and a desktop session. No author or cover metadata is collected yet.
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
