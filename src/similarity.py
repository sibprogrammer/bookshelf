import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from math import fsum, log, sqrt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Book


STOP_WORDS = frozenset(
    """a about above after again against all also am an and any are as at be
    because been before being below between both but by can could did do does
    doing down during each few for from further had has have having he her here
    hers herself him himself his how i if in into is it its itself just me more
    most my myself no nor not now of off on once only or other our ours ourselves
    out over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up
    us very was we were what when where which while who whom why will with would
    you your yours yourself yourselves book books edition editions introduction
    introductory guide guides handbook manual complete practical volume volumes
    revised updated new first second third fourth fifth press copyright unknown
    anonymous various none untitled
    """.split()
)
UNKNOWN_VALUES = {"unknown", "anonymous", "various", "none", "n a"}
MIN_KEYWORD_SIMILARITY = 0.18
MIN_TITLE_SIMILARITY = 0.25
TITLE_WEIGHT = 6
CATEGORY_BOOST = 3


@dataclass(frozen=True)
class SimilarBook:
    book: Book
    reason: str


def _normalize(value: str) -> str:
    return " ".join(
        re.findall(
            r"[^\W_]+(?:\+\+|#)?", unicodedata.normalize("NFKC", value).casefold()
        )
    )


def _tokens(value: str | None) -> set[str]:
    return {
        word
        for word in _normalize(value or "").split()
        if word not in STOP_WORDS
        and not word.isdecimal()
        and not re.fullmatch(r"\d+(?:st|nd|rd|th)", word)
        and (len(word) > 1 or word in {"c", "r"})
    }


def _authors(book: Book) -> set[str]:
    return {
        name
        for author in (book.metadata.author or "").split(";")
        if (name := _normalize(author)) and name not in UNKNOWN_VALUES
    }


def _subjects(book: Book) -> dict[str, str]:
    return {
        _normalize(subject): subject.strip()
        for subject in re.split(r"[;,|\n]", book.metadata.subjects or "")
        if _tokens(subject) and _normalize(subject) not in UNKNOWN_VALUES
    }


def _title_tokens(book: Book) -> set[str]:
    return _tokens(book.title) | _tokens(book.file_title)


def _terms(book: Book) -> Counter:
    terms = Counter(_tokens(book.metadata.description))
    terms.update({term: 3 for term in _title_tokens(book)})
    terms.update({term: 3 for term in _tokens(book.metadata.subjects)})
    return terms


def _identities(book: Book) -> set[tuple[str, ...]]:
    identities = {("file", file.sha256) for file in book.files}
    if isbn := book.metadata.isbn:
        identities.add(("isbn", "978" + isbn[:9] if len(isbn) == 10 else isbn[:12]))
    if authors := _authors(book):
        identities.add(
            ("title_author", _normalize(book.title), ";".join(sorted(authors)))
        )
    return identities


def find_similar_books(book: Book, books: list[Book]) -> list[SimilarBook]:
    candidates = [candidate for candidate in books if candidate.id != book.id]
    documents = [_terms(book), *(_terms(candidate) for candidate in candidates)]
    frequencies = Counter(term for document in documents for term in document)
    idf = {
        term: 1 + log((len(documents) + 1) / (count + 1))
        for term, count in frequencies.items()
    }

    def vector(terms):
        weighted = {term: count * idf[term] for term, count in terms.items()}
        norm = sqrt(fsum(weight**2 for weight in weighted.values()))
        return (
            {term: weight / norm for term, weight in weighted.items()} if norm else {}
        )

    source_vector = vector(documents[0])
    source_title_vector = vector(Counter(_title_tokens(book)))
    source_subjects = _subjects(book)
    source_authors = _authors(book)
    source_identities = _identities(book)
    ranked = []
    for candidate, terms in zip(candidates, documents[1:], strict=True):
        identities = _identities(candidate)
        if source_identities & identities:
            continue
        subjects = _subjects(candidate)
        shared_subjects = source_subjects.keys() & subjects.keys()
        same_author = bool(source_authors & _authors(candidate))
        candidate_vector = vector(terms)
        shared_terms = source_vector.keys() & candidate_vector.keys()
        contributions = {
            term: source_vector[term] * candidate_vector[term]
            for term in sorted(shared_terms)
        }
        keyword_similarity = fsum(contributions.values())
        title_vector = vector(Counter(_title_tokens(candidate)))
        title_similarity = fsum(
            weight * source_title_vector.get(term, 0)
            for term, weight in title_vector.items()
        )
        if (
            not shared_subjects
            and not same_author
            and keyword_similarity < MIN_KEYWORD_SIMILARITY
            and title_similarity < MIN_TITLE_SIMILARITY
        ):
            continue

        subject_score = (
            4 * len(shared_subjects) / len(source_subjects.keys() | subjects.keys())
            if shared_subjects
            else 0
        )
        author_score = 2 if same_author else 0
        keyword_score = 3 * keyword_similarity
        title_score = TITLE_WEIGHT * title_similarity
        reasons = []
        if title_similarity >= MIN_TITLE_SIMILARITY:
            reasons.append((title_score, "Similar title"))
        if shared_subjects:
            label = source_subjects[min(shared_subjects)]
            reasons.append((subject_score, f"Shared subject: {label}"))
        if same_author:
            reasons.append((author_score, "Same author"))
        if keyword_similarity >= MIN_KEYWORD_SIMILARITY:
            keywords = sorted(
                contributions, key=lambda term: (-contributions[term], term)
            )
            reasons.append(
                (keyword_score, f"Shared keywords: {', '.join(keywords[:2])}")
            )
        reason = max(reasons, key=lambda item: item[0])[1]
        score = title_score + subject_score + author_score + keyword_score
        if candidate.category.id == book.category.id:
            score += CATEGORY_BOOST
        ranked.append((score, candidate, reason, identities))

    ranked.sort(
        key=lambda item: (
            -item[0],
            item[1].file_title.casefold(),
            item[1].directory,
            item[1].id,
        )
    )
    results = []
    seen = set()
    for _score, candidate, reason, identities in ranked:
        if identities & seen:
            continue
        results.append(SimilarBook(candidate, reason))
        seen.update(identities)
        if len(results) == 5:
            break
    return results
