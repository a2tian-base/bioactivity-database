from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Optional, Sequence, TypeVar


T = TypeVar("T")


def env_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return default


def log(message: str) -> None:
    print(message, flush=True)


def chunked(values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def http_get_json(
    url: str,
    params: Optional[Dict[str, object]],
    timeout_seconds: int,
    retries: int,
    label: str = "API",
) -> Dict:
    if params:
        encoded = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{encoded}" if encoded else url
    else:
        full_url = url

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(full_url, timeout=timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if attempt < retries and retryable:
                sleep_seconds = 2**attempt
                log(f"HTTP {exc.code} from {label}, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                sleep_seconds = 2**attempt
                log(f"Network error from {label}, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise
