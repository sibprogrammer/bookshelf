from dataclasses import replace
from re import findall

import pytest
from PIL import Image

from src.metadata import Metadata
from src.similarity import find_similar_books
from src.store import Book, BookFile, Category


def make_book(book_id, title, *, category_id=1, **metadata):
    return Book(
        title=title,
        directory=f"/Category{category_id}",
        files=(
            BookFile(f"/Category{category_id}/{title}.pdf", "pdf", f"{book_id:064x}"),
        ),
        id=book_id,
        category=Category(category_id, f"Category{category_id}"),
        metadata=Metadata(title=title, **metadata),
    )


def test_subjects_rank_above_author_and_category_alone_does_not_qualify():
    source = make_book(1, "PostgreSQL", subjects="Databases", author="Jane Smith")
    subject = make_book(2, "Storage", subjects=" databases ", category_id=2)
    author = make_book(3, "Networks", author="JANE SMITH;")
    unrelated = make_book(4, "Gardening")
    results = find_similar_books(source, [source, unrelated, author, subject])
    assert [result.book for result in results] == [subject, author]
    assert [result.reason for result in results] == [
        "Shared subject: Databases",
        "Same author",
    ]


def test_keywords_match_across_fields_and_ignore_common_words():
    source = make_book(1, "Practical PostgreSQL Guide")
    relevant = make_book(2, "Storage", description="An introduction to POSTGRESQL.")
    unrelated = make_book(3, "A Practical Gardening Guide")
    results = find_similar_books(source, [source, unrelated, relevant])
    assert [result.book for result in results] == [relevant]
    assert results[0].reason == "Shared keywords: postgresql"


def test_rare_keywords_rank_above_frequent_keywords():
    source = make_book(1, "PostgreSQL Databases")
    rare = make_book(2, "PostgreSQL")
    frequent = make_book(3, "Databases")
    others = [make_book(i, f"Databases Topic{i}") for i in range(4, 15)]
    results = find_similar_books(source, [source, frequent, rare, *others])
    assert results[0].book == rare
    assert results[1].book == frequent


def test_category_boosts_an_existing_match():
    source = make_book(1, "Source", author="Jane Smith")
    elsewhere = make_book(2, "Alpha", author="Jane Smith", category_id=2)
    same_category = make_book(3, "Zulu", author="Jane Smith")
    results = find_similar_books(source, [source, elsewhere, same_category])
    assert [result.book for result in results] == [same_category, elsewhere]


def test_close_title_ranks_above_matching_description():
    description = (
        "Replication storage transactions indexing concurrency queries tuning "
        "connections memory caching monitoring backups recovery partitioning"
    )
    source = make_book(1, "PostgreSQL Performance", description=description)
    close_title = make_book(2, "PostgreSQL Performance Analysis")
    matching_description = make_book(3, "Database Operations", description=description)
    results = find_similar_books(source, [source, matching_description, close_title])
    assert [result.book for result in results] == [close_title, matching_description]
    assert results[0].reason == "Similar title"


def test_close_title_qualifies_even_with_long_unrelated_descriptions():
    source = make_book(
        1,
        "PostgreSQL Performance",
        description=" ".join(f"source{i}" for i in range(200)),
    )
    candidate = make_book(
        2,
        "PostgreSQL Performance Analysis",
        category_id=2,
        description=" ".join(f"candidate{i}" for i in range(200)),
    )
    results = find_similar_books(source, [source, candidate])
    assert [result.book for result in results] == [candidate]
    assert results[0].reason == "Similar title"


def test_shared_category_outweighs_a_stronger_description_keyword_match():
    source = make_book(1, "Practical Guide", description="PostgreSQL")
    elsewhere = make_book(2, "PostgreSQL", category_id=2)
    same_category = make_book(3, "Storage", description="PostgreSQL")
    results = find_similar_books(source, [source, elsewhere, same_category])
    assert [result.book for result in results] == [same_category, elsewhere]


def test_multiple_authors_and_subjects_are_matched_as_whole_values():
    source = make_book(
        1,
        "Source",
        author="Smith, Jane; Émile Dupont",
        subjects="Databases; Distributed systems",
    )
    author = make_book(2, "Networks", author="ÉMILE DUPONT")
    subject = make_book(3, "Storage", subjects="Cooking,distributed SYSTEMS|Systems")
    different_author = make_book(4, "Gardening", author="Jane Doe")
    results = find_similar_books(source, [source, different_author, author, subject])
    assert {result.book.id for result in results} == {author.id, subject.id}
    assert next(result for result in results if result.book == author).reason == (
        "Same author"
    )


def test_title_and_filename_both_contribute_keywords():
    source = make_book(1, "PostgreSQL", description="")
    source = replace(source, title="Storage", metadata=Metadata(title="Storage"))
    filename_match = make_book(2, "PostgreSQL")
    title_match = make_book(3, "Storage")
    assert {
        result.book.id
        for result in find_similar_books(source, [source, filename_match, title_match])
    } == {2, 3}


@pytest.mark.parametrize("author", [None, "Unknown", "Anonymous", "n/a", "Various"])
def test_missing_or_placeholder_metadata_and_generic_titles_do_not_match(author):
    source = make_book(1, "The Complete Guide, 2nd Edition", author=author)
    unrelated = make_book(
        2, "An Introduction and Practical Handbook, 2nd Edition", author=author
    )
    assert find_similar_books(source, [source, unrelated]) == []


def test_weak_keyword_overlap_does_not_fill_slots():
    source = make_book(1, "PostgreSQL Replication")
    irrelevant = make_book(
        2,
        "Gardening Horticulture Botany Flowers Vegetables Landscape Plants Soil",
        description="This book was typeset using PostgreSQL.",
    )
    assert find_similar_books(source, [source, irrelevant]) == []


def test_programming_language_names_remain_distinct():
    source = make_book(1, "C++ Guide")
    relevant = make_book(2, "C++ Handbook")
    other_languages = [make_book(3, "C# Guide"), make_book(4, "C Guide")]
    assert [
        result.book
        for result in find_similar_books(source, [source, relevant, *other_languages])
    ] == [relevant]


def test_limit_and_alphabetical_ties_are_stable():
    source = make_book(1, "Source", author="Jane Smith")
    candidates = [
        make_book(i, title, author="Jane Smith")
        for i, title in enumerate(
            ["Zulu", "Theta", "Gamma", "Delta", "Beta", "Alpha"], start=2
        )
    ]
    results = find_similar_books(source, [source, *candidates])
    assert [result.book.title for result in results] == [
        "Alpha",
        "Beta",
        "Delta",
        "Gamma",
        "Theta",
    ]
    assert find_similar_books(source, [*reversed(candidates), source]) == results
    assert find_similar_books(source, [source]) == []
    assert find_similar_books(source, []) == []


def test_duplicate_source_copies_are_excluded_by_hash_isbn_or_title_and_author():
    source = make_book(1, "PostgreSQL", author="Jane Smith", isbn="9780132350884")
    hash_copy = make_book(2, "PostgreSQL Archive")
    hash_copy = replace(
        hash_copy, files=(replace(hash_copy.files[0], sha256=source.files[0].sha256),)
    )
    isbn_copy = make_book(3, "PostgreSQL Reprint", isbn="0132350882")
    title_copy = make_book(4, "POSTGRESQL!", author="JANE SMITH", category_id=2)
    assert find_similar_books(source, [source, hash_copy, isbn_copy, title_copy]) == []


def test_suggested_books_are_deduplicated_and_remaining_slots_are_filled():
    source = make_book(1, "Source", author="Jane Smith")
    candidates = [
        make_book(i, title, author="Jane Smith")
        for i, title in enumerate(["Alpha", "Beta", "Delta", "Gamma", "Theta"], 2)
    ]
    duplicate = replace(candidates[0], id=8, directory="/Archive")
    results = find_similar_books(source, [source, *candidates, duplicate])
    assert len(results) == 5
    assert len({result.book.files[0].sha256 for result in results}) == 5


def test_store_and_details_show_five_cards_with_covers_and_badges(app, client):
    store = app.extensions["store"]
    store.sync_files(
        [
            BookFile(
                f"/Books/Book{i}.pdf",
                "pdf",
                f"{i:064x}",
                metadata=Metadata(author="Jane Smith"),
            )
            for i in range(7)
        ]
        + [
            BookFile(
                "/Books/Book1.epub",
                "epub",
                "f" * 64,
                metadata=Metadata(author="Jane Smith"),
            )
        ]
    )
    source, candidate, *_ = store.books()
    store.set_favorite(candidate.id, True)
    store.set_finished(candidate.id, True)
    suggestions = store.similar_books(source)
    assert len(suggestions) == 5
    assert suggestions[0].book == store.book(candidate.id)
    assert len(suggestions[0].book.files) == 2
    covers = app.extensions["indexer"].covers
    covers.directory.mkdir(parents=True)
    Image.new("RGB", (333, 500), "navy").save(covers.path(candidate.files[0]))

    response = client.get(f"/books/{source.id}")
    assert response.status_code == 200
    assert response.data.index(b"</footer>") < response.data.index(
        b'id="similar-heading"'
    )
    section = response.data.split(b'<section class="mt-5"', 1)[1]
    assert section.count(b'class="home-book-card card ') == 5
    assert [int(value) for value in findall(rb'href="/books/(\d+)"', section)] == [
        suggestion.book.id for suggestion in suggestions
    ]
    assert b"Same author" not in section
    assert section.count(b"Cover unavailable") == 4
    assert f'src="/books/{candidate.id}/cover"'.encode() in section
    assert b'loading="lazy"' in section
    assert section.count(b'class="book-favorite-badge"') == 1
    assert section.count(b'class="book-finished-badge"') == 1
    assert b"/open" not in section
    assert b"Same author" not in client.get("/").data


def test_suggestions_are_available_without_match_descriptions_on_open_failure(
    app, client
):
    store = app.extensions["store"]
    subject = "<script>alert('x')</script>"
    store.sync_files(
        [
            BookFile(
                f"/Books/Book{i}.pdf",
                "pdf",
                f"{i:064x}",
                metadata=Metadata(subjects=subject),
            )
            for i in range(2)
        ]
    )
    source = store.books()[0]
    for response in (
        client.get(f"/books/{source.id}"),
        client.post(f"/books/{source.id}/files/{source.files[0].id}/open"),
    ):
        assert response.status_code in {200, 404}
        assert b"See More" in response.data
        section = response.data.split(b'<section class="mt-5"', 1)[1]
        assert section.count(b'class="home-book-card card ') == 1
        assert b"Shared subject:" not in section
        assert b"<script>alert" not in response.data


@pytest.mark.parametrize("count", [1, 2])
def test_details_hide_suggestions_without_relevant_metadata(app, client, count):
    store = app.extensions["store"]
    store.sync_files(
        [BookFile(f"/Books/Book{i}.pdf", "pdf", f"{i:064x}") for i in range(count)]
    )
    source = store.books()[0]
    response = client.get(f"/books/{source.id}")
    assert response.status_code == 200
    assert b"See More" not in response.data
    assert b'class="home-book-card card ' not in response.data
