import json

import pytest

from src.config import load_config


def test_config_defaults_and_relative_paths(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"books_dir": "./library"}')
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(path)
    assert config.books_dir == tmp_path / "library"
    assert config.database_path == tmp_path / "data" / "bookshelf.db"


def test_explicit_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "books_dir": str(tmp_path / "absolute-books"),
                "database_path": "index.db",
            }
        )
    )
    config = load_config(path)
    assert config.books_dir == tmp_path / "absolute-books"
    assert config.database_path == tmp_path / "index.db"


@pytest.mark.parametrize(
    "contents",
    [
        "{}",
        '{"books_dir":" "}',
        '{"books_dir":"books","book_dir":"typo"}',
        '{"books_dir":"books"} {}',
        '{"books_dir":"books"} invalid',
        '{"books_dir":"books","database_path":""}',
        "{",
        "null",
        "[]",
        '{"books_dir":42}',
        '{"books_dir":"books","database_path":null}',
    ],
)
def test_invalid_config(tmp_path, contents):
    path = tmp_path / "config.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        load_config(path)


def test_missing_config(tmp_path):
    with pytest.raises(ValueError, match="Copy config.json.sample"):
        load_config(tmp_path / "missing.json")
