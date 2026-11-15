import pytest

from src import opener


@pytest.mark.parametrize(
    "platform,command",
    [
        ("darwin", "open"),
        ("linux", "xdg-open"),
    ],
)
def test_opener_passes_path_as_single_argument(monkeypatch, platform, command):
    calls = []
    monkeypatch.setattr(opener.sys, "platform", platform)
    monkeypatch.setattr(
        opener.subprocess, "run", lambda *a, **kw: calls.append((a, kw))
    )
    path = "/books/book's & $(name).pdf"
    opener.open_local_book(path)
    assert calls[0][0] == ([command, path],)
    assert calls[0][1]["check"] is True


def test_windows_opener(monkeypatch):
    calls = []
    monkeypatch.setattr(opener.sys, "platform", "win32")
    monkeypatch.setattr(opener.os, "startfile", calls.append, raising=False)
    opener.open_local_book("C:\\books\\book.pdf")
    assert calls == ["C:\\books\\book.pdf"]
