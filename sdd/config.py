import os
from pathlib import Path


def load_env(path=".env"):
    """Small .env reader; existing environment always wins. Never logs values."""
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def runtime():
    from .db import Database
    from .execution import Executor
    from .evaluators import JevBackend

    load_env()
    db = Database(os.getenv("DATABASE_URL", "sqlite:///sdd.db"))
    backends = {}
    if os.getenv("TYPESAFE_API_KEY"):
        backends["jev"] = JevBackend(os.environ["TYPESAFE_API_KEY"])
    return db, Executor(db, backends)
