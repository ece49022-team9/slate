import os
from pathlib import Path


def home() -> Path:
    return Path(os.getenv("SLATE_HOME") or Path.home() / ".slate")
