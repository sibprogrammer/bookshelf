"""Flask application factory and routes for Bookshelf."""

import sqlite3
import stat
import subprocess
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from .config import Config, load_config
from .indexer import Indexer, IndexInProgress
from .opener import open_local_book
from .store import Book, ScanCancelled, Store


def create_app(config: Config | None = None) -> Flask:
    if config is None:
        config = load_config()
    app = Flask(__name__)
    app.config["BOOKSHELF_CONFIG"] = config
    store = Store(config.database_path)
    indexer = Indexer(store, config.books_dir)
    app.extensions.update(store=store, indexer=indexer, open_book=open_local_book)

    @app.before_request
    def protect_local_actions():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Keep other websites from triggering scans or local reader launches.
            site = request.headers.get("Sec-Fetch-Site")
            origin = request.headers.get("Origin")
            if site not in {None, "same-origin", "none"} or (
                origin is not None and origin != request.host_url.rstrip("/")
            ):
                abort(403)

    @app.after_request
    def response_headers(response):
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def render_library(status=200, message="", error="", warning=""):
        return render_template(
            "library.html",
            books=store.books(),
            message=message,
            error=error,
            warning=warning,
        ), status

    def find_book(book_id: int) -> Book:
        if not 0 < book_id <= 2**63 - 1:
            abort(404)
        book = store.book(book_id)
        if book is None:
            abort(404)
        return book

    @app.get("/")
    def home():
        books = store.recent_books()
        random_books = store.random_books()
        return render_template(
            "home.html",
            books=books,
            random_books=random_books,
            cover_book_ids={
                book.id
                for book in books + random_books
                if indexer.covers.find(book.files) is not None
            },
        )

    @app.get("/library")
    def library():
        count = request.args.get("indexed", type=int)
        message = (
            f"Index complete. {count} book(s) indexed."
            if count is not None and count >= 0
            else ""
        )
        warning = (
            f"Covers unavailable for {len(indexer.cover_errors)} book(s). "
            "See the server logs for details."
            if indexer.cover_errors
            else ""
        )
        return render_library(message=message, warning=warning)

    def render_details(book: Book, *, error=""):
        return render_template(
            "details.html",
            book=book,
            error=error,
            cover_available=indexer.covers.find(book.files) is not None,
        )

    @app.post("/index")
    def reindex():
        try:
            count = indexer.reindex()
        except (OSError, sqlite3.Error, IndexInProgress, ScanCancelled) as error:
            app.logger.exception("Indexing failed")
            return render_library(
                status=409 if isinstance(error, IndexInProgress) else 500,
                error=f"Indexing failed; the existing index was kept. {error}",
            )
        return redirect(url_for("library", indexed=count), code=303)

    @app.get("/books/<int:book_id>")
    def details(book_id: int):
        return render_details(find_book(book_id))

    @app.get("/books/<int:book_id>/cover")
    def cover(book_id: int):
        book = find_book(book_id)
        path = indexer.covers.find(book.files)
        if path is None:
            abort(404)
        return send_file(path, mimetype="image/png", conditional=True, max_age=0)

    @app.post("/books/<int:book_id>/files/<int:file_id>/open")
    def open_book(book_id: int, file_id: int):
        book = find_book(book_id)
        file = next((file for file in book.files if file.id == file_id), None)
        if file is None:
            abort(404)

        def failed(message: str, status: int):
            return render_details(book, error=message), status

        try:
            info = Path(file.file_path).stat()
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError(file.file_path)
        except FileNotFoundError:
            return failed(
                "Book file not found. Reindex your books directory to update the list.",
                404,
            )
        except OSError:
            app.logger.exception("Unable to access book %s", file.file_path)
            return failed("Unable to access this book file.", 500)
        try:
            app.extensions["open_book"](file.file_path)
        except OSError, subprocess.SubprocessError:
            app.logger.exception("Unable to open book %s", file.file_path)
            return failed(
                "Unable to open this book. Check that a default reader is installed "
                "for this file type.",
                500,
            )
        return redirect(url_for("details", book_id=book.id), code=303)

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        app.logger.error("Database error: %s", error)
        return render_template(
            "error.html",
            title="Unable to load your library",
            error="Unable to load the book index. Please try again.",
        ), 500

    return app
