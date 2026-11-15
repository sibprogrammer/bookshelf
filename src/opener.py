"""Open an indexed file in the server computer's default reader."""

import os
import subprocess
import sys


def open_local_book(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
        return
    if sys.platform == "darwin":
        command = ["open", path]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", path]
    else:
        raise OSError(f"Opening local files is unsupported on {sys.platform}")
    subprocess.run(command, check=True, timeout=30, capture_output=True)
