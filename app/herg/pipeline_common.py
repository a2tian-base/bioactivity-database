from __future__ import annotations

from datetime import datetime, timezone
import json


class JsonlLogger:
    def __init__(self, path: str | None) -> None:
        self._handle = None
        if path:
            self._handle = open(path, "a", encoding="utf-8")

    def log(self, payload: dict[str, object]) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def payload_snippet(payload: object, limit: int = 2000) -> str:
    try:
        serialized = json.dumps(payload, ensure_ascii=True, default=str)
    except Exception:
        return "<unserializable>"
    if len(serialized) <= limit:
        return serialized
    return serialized[:limit] + "..."


def log_jsonl(
    logger: JsonlLogger,
    source_name: str,
    external_key: str,
    reason: str,
    payload: object,
    limit: int = 2000,
) -> None:
    logger.log(
        {
            "source_name": source_name,
            "external_key": external_key,
            "reason": reason,
            "payload_snippet": payload_snippet(payload, limit=limit),
        }
    )


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def duration_seconds(started_at: str, finished_at: str) -> float:
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    return (finished - started).total_seconds()


def write_stats(path: str | None, payload: dict[str, object]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)
