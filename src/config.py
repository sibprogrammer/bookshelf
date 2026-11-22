"""Bookshelf library and database configuration."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass(frozen=True)
class Config:
    books_dir: Path
    database_path: Path
    goodreads_ratings: bool = True


def load_config() -> Config:
    path = CONFIG_PATH
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            f"Config not found: {path}. Copy config.json.sample to config.json first."
        ) from error
    except (OSError, ValueError) as error:
        raise ValueError(f"Unable to read config: {error}") from error
    if not isinstance(values, dict):
        raise ValueError("Config must contain exactly one JSON object")
    unknown = values.keys() - {"books_dir", "database_path", "goodreads_ratings"}
    if unknown:
        raise ValueError(f"Unknown config key(s): {', '.join(sorted(unknown))}")
    values = {
        "database_path": "./data/bookshelf.db",
        "goodreads_ratings": True,
        **values,
    }
    for key in ("books_dir", "database_path"):
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not isinstance(values["goodreads_ratings"], bool):
        raise ValueError("goodreads_ratings must be a boolean")

    def absolute(value: str) -> Path:
        # Do not resolve symlinks here: the books root is resolved on each scan.
        return Path(os.path.abspath(path.parent / value))

    return Config(
        books_dir=absolute(values["books_dir"]),
        database_path=absolute(values["database_path"]),
        goodreads_ratings=values["goodreads_ratings"],
    )
