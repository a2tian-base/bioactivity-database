from __future__ import annotations

from dataclasses import dataclass
import os


def _env(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value else default


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "DbConfig":
        return cls(
            host=_env("DB_HOST", "localhost"),
            port=int(_env("DB_PORT", "5432")),
            dbname=_env("DB_NAME", _env("POSTGRES_DB", "herg")),
            user=_env("DB_USER", _env("POSTGRES_USER", "herg_user")),
            password=_env("DB_PASSWORD", _env("POSTGRES_PASSWORD", "change_me")),
        )


@dataclass(frozen=True)
class HttpConfig:
    request_timeout_seconds: int = 45
    http_retries: int = 4
    user_agent: str = "herg-ingest/1.0"


@dataclass(frozen=True)
class RunConfig:
    dry_run: bool = False
    max_records: int | None = None
    commit_every: int = 500
    fail_fast: bool = False
    errors_path: str | None = None
    stats_path: str | None = None


@dataclass(frozen=True)
class IdentifierRunConfig(RunConfig):
    unmatched_path: str | None = None
    conflicts_path: str | None = None
    create_missing_compounds: bool = False


@dataclass(frozen=True)
class StructureRunConfig(RunConfig):
    unmatched_path: str | None = None
    conflicts_path: str | None = None
