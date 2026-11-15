import pytest

from src.config import Config
from src.main import create_app
from src.store import Store


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data" / "bookshelf.db")


@pytest.fixture
def app(library, tmp_path):
    app = create_app(Config(library, tmp_path / "data" / "bookshelf.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()
