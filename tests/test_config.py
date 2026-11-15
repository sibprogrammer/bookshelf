import json

import pytest

from src import config as config_module
from src.config import load_config
from src.main import create_app


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


def test_config_defaults_and_relative_paths(config_file, tmp_path, monkeypatch):
    config_file.write_text('{"books_dir": "./library"}')
    monkeypatch.chdir(tmp_path.parent)
    config = load_config()
    assert config.books_dir == tmp_path / "library"
    assert config.database_path == tmp_path / "data" / "bookshelf.db"


def test_explicit_config(config_file, tmp_path):
    config_file.write_text(
        json.dumps(
            {
                "books_dir": str(tmp_path / "absolute-books"),
                "database_path": "index.db",
            }
        )
    )
    config = load_config()
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
def test_invalid_config(config_file, contents):
    config_file.write_text(contents)
    with pytest.raises(ValueError):
        load_config()


def test_missing_config(config_file):
    with pytest.raises(ValueError, match="Copy config.json.sample"):
        load_config()


def test_app_ignores_config_path_environment_variable(
    config_file, tmp_path, monkeypatch
):
    config_file.write_text('{"books_dir": "./library"}')
    alternate = tmp_path / "alternate.json"
    alternate.write_text('{"books_dir": "./other", "database_path": "other.db"}')
    monkeypatch.setenv("BOOKSHELF_CONFIG", str(alternate))
    app = create_app()
    assert app.extensions["store"].path == tmp_path / "data" / "bookshelf.db"
    assert app.extensions["indexer"].root == tmp_path / "library"
