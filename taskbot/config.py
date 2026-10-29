"""Settings from environment variables (or a .env file in the working directory)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass
class Config:
    token: str
    allowed_users: frozenset[int]
    db_path: str = "tasks.sqlite3"
    tz: ZoneInfo = ZoneInfo("Europe/Rome")
    done_days: int = 7
    transcriber: str = "off"
    whisper_model: str = "small"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini-transcribe"

    @classmethod
    def from_env(cls) -> Config:
        _load_dotenv()
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN is missing (see .env.example)")
        return cls(
            token=token,
            allowed_users=_ids(os.environ.get("ALLOWED_USERS", "")),
            db_path=os.environ.get("DB_PATH", "tasks.sqlite3"),
            tz=ZoneInfo(os.environ.get("TIMEZONE", "Europe/Rome")),
            done_days=int(os.environ.get("DONE_DAYS", "7")),
            transcriber=os.environ.get("TRANSCRIBER", "off"),
            whisper_model=os.environ.get("WHISPER_MODEL", "small"),
            openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini-transcribe"),
        )
