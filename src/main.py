"""Flask application factory and routes for Bookshelf."""

import sqlite3
import stat
import subprocess
from datetime import date
from io import BytesIO
from pathlib import Path

import pymupdf
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from .config import Config, load_config
from .covers import CoverError, preferred_sources, render_cover
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

    @app.template_filter("published_date")
    def format_published_date(value: str) -> str:
        parts = [int(part) for part in value.split("-")]
        if len(parts) == 1:
            return value
        published = date(parts[0], parts[1], parts[2] if len(parts) == 3 else 1)
        return published.strftime("%d %b %Y" if len(parts) == 3 else "%b %Y")

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

    def render_categories(status=200, message="", error=""):
        warning = (
            f"Covers unavailable for {len(indexer.cover_errors)} book(s). "
            "See the server logs for details."
            if indexer.cover_errors
            else ""
        )
        return render_template(
            "categories.html",
            categories=store.categories(),
            book_counts=store.category_book_counts(),
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

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        books = store.search_books(query)
        return render_template(
            "search.html",
            search_query=query,
            books=books,
            cover_book_ids={
                book.id for book in books if indexer.covers.find(book.files) is not None
            },
        )

    @app.get("/favorites")
    def favorites():
        books = store.favorite_books()
        return render_template(
            "favorites.html",
            books=books,
            cover_book_ids={
                book.id for book in books if indexer.covers.find(book.files) is not None
            },
        )

    @app.get("/want-to-read")
    def want_to_read():
        books = store.want_to_read_books()
        return render_template(
            "want_to_read.html",
            books=books,
            cover_book_ids={
                book.id for book in books if indexer.covers.find(book.files) is not None
            },
        )

    @app.get("/finished")
    def finished():
        books = store.finished_books()
        return render_template(
            "finished.html",
            books=books,
            cover_book_ids={
                book.id for book in books if indexer.covers.find(book.files) is not None
            },
        )

    @app.get("/categories")
    def categories():
        count = request.args.get("indexed", type=int)
        message = (
            f"Index complete. {count} book(s) indexed."
            if count is not None and count >= 0
            else ""
        )
        return render_categories(message=message)

    @app.get("/categories/<int:category_id>")
    def category_details(category_id: int):
        if not 0 < category_id <= 2**63 - 1:
            abort(404)
        category = store.category(category_id)
        if category is None:
            abort(404)
        books = store.books(category_id=category.id)
        return render_template(
            "category.html",
            category=category,
            books=books,
            cover_book_ids={
                book.id for book in books if indexer.covers.find(book.files) is not None
            },
        )

    def render_details(book: Book, *, error=""):
        similar_books = store.similar_books(book)
        preview_file = next(iter(preferred_sources(book.files)), None)
        return render_template(
            "details.html",
            book=book,
            error=error,
            cover_available=indexer.covers.find(book.files) is not None,
            preview_format=preview_file.extension.upper() if preview_file else None,
            preview_start=0 if preview_file and preview_file.extension == "epub" else 1,
            similar_books=similar_books,
            cover_book_ids={
                suggestion.book.id
                for suggestion in similar_books
                if indexer.covers.find(suggestion.book.files) is not None
            },
        )

    @app.post("/index")
    def reindex():
        try:
            count = indexer.reindex()
        except (OSError, sqlite3.Error, IndexInProgress, ScanCancelled) as error:
            app.logger.exception("Indexing failed")
            return render_categories(
                status=409 if isinstance(error, IndexInProgress) else 500,
                error=f"Indexing failed; the existing index was kept. {error}",
            )
        return redirect(url_for("categories", indexed=count), code=303)

    @app.get("/books/<int:book_id>")
    def details(book_id: int):
        return render_details(find_book(book_id))

    @app.post("/books/<int:book_id>/favorite")
    def set_favorite(book_id: int):
        book = find_book(book_id)
        favorite = request.form.get("favorite")
        if favorite not in {"0", "1"}:
            abort(400)
        if not store.set_favorite(book.id, favorite == "1"):
            abort(404)
        return redirect(url_for("details", book_id=book.id), code=303)

    @app.post("/books/<int:book_id>/want-to-read")
    def set_want_to_read(book_id: int):
        book = find_book(book_id)
        want_to_read = request.form.get("want_to_read")
        if want_to_read not in {"0", "1"}:
            abort(400)
        if not store.set_want_to_read(book.id, want_to_read == "1"):
            abort(404)
        return redirect(url_for("details", book_id=book.id), code=303)

    @app.post("/books/<int:book_id>/finished")
    def set_finished(book_id: int):
        book = find_book(book_id)
        finished = request.form.get("finished")
        if finished not in {"0", "1"}:
            abort(400)
        if not store.set_finished(book.id, finished == "1"):
            abort(404)
        return redirect(url_for("details", book_id=book.id), code=303)

    @app.get("/books/<int:book_id>/cover")
    def cover(book_id: int):
        book = find_book(book_id)
        path = indexer.covers.find(book.files)
        if path is None:
            abort(404)
        return send_file(path, mimetype="image/png", conditional=True, max_age=0)

    @app.get("/books/<int:book_id>/preview/<int:page_number>")
    def preview_page(book_id: int, page_number: int):
        book = find_book(book_id)
        file = next(iter(preferred_sources(book.files)), None)
        if file is None or not Path(file.file_path).is_file():
            abort(404)
        try:
            with pymupdf.open(file.file_path) as document:
                if document.needs_pass or (
                    file.extension == "pdf" and not document.is_pdf
                ):
                    abort(422)
                if document.is_reflowable:
                    # Keep EPUB pagination identical in the inline and larger views.
                    document.layout(width=499.5, height=750, fontsize=11)
                page_count = document.page_count
                first_page = 0 if file.extension == "epub" else 1
                if not first_page <= page_number <= page_count:
                    abort(404)
                width, height = (
                    (1332, 2000) if request.args.get("size") == "large" else (666, 1000)
                )
                if page_number == 0:
                    output = BytesIO()
                    render_cover(Path(file.file_path), size=(width, height)).save(
                        output, format="PNG"
                    )
                    image = output.getvalue()
                else:
                    page = document.load_page(page_number - 1)
                    if page.rect.is_empty or page.rect.is_infinite:
                        abort(422)
                    scale = min(width / page.rect.width, height / page.rect.height)
                    image = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale),
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    ).tobytes("png")
        except OSError, RuntimeError, ValueError, CoverError:
            app.logger.exception("Unable to preview book %s", file.file_path)
            abort(422)
        response = send_file(BytesIO(image), mimetype="image/png")
        response.headers["X-Page-Count"] = str(page_count)
        response.headers["Cache-Control"] = "no-store"
        return response

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
