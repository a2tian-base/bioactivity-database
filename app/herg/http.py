from __future__ import annotations

import csv
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Optional

from .config import HttpConfig


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _build_url(url: str, params: Optional[Dict[str, object]]) -> str:
    if not params:
        return url
    encoded = urllib.parse.urlencode(params, doseq=True)
    return f"{url}?{encoded}" if encoded else url


def _request(
    url: str,
    config: HttpConfig,
    *,
    data: bytes | None = None,
    headers: Optional[Dict[str, str]] = None,
    method: str | None = None,
) -> urllib.request.Request:
    request_headers = {"User-Agent": config.user_agent}
    if headers:
        request_headers.update(headers)
    return urllib.request.Request(url, data=data, headers=request_headers, method=method)


def get_json(
    url: str,
    params: Optional[Dict[str, object]],
    config: HttpConfig,
    label: str = "API",
) -> Dict:
    full_url = _build_url(url, params)

    for attempt in range(config.http_retries + 1):
        try:
            req = _request(full_url, config)
            with urllib.request.urlopen(req, timeout=config.request_timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if attempt < config.http_retries and exc.code in RETRYABLE_STATUS:
                time.sleep(2**attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise
        except http.client.RemoteDisconnected:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise

    return {}


def post_json(
    url: str,
    payload: dict,
    config: HttpConfig,
    label: str = "API",
) -> Dict:
    encoded_payload = json.dumps(payload).encode("utf-8")

    for attempt in range(config.http_retries + 1):
        try:
            req = _request(
                url,
                config,
                data=encoded_payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=config.request_timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if attempt < config.http_retries and exc.code in RETRYABLE_STATUS:
                time.sleep(2**attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise
        except http.client.RemoteDisconnected:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise

    return {}


def get_csv_rows(url: str, config: HttpConfig, label: str = "API") -> Iterable[dict]:
    for attempt in range(config.http_retries + 1):
        try:
            req = _request(url, config)
            with urllib.request.urlopen(req, timeout=config.request_timeout_seconds) as response:
                reader = csv.DictReader((line.decode("utf-8", "ignore") for line in response))
                for row in reader:
                    yield row
            return
        except urllib.error.HTTPError as exc:
            if attempt < config.http_retries and exc.code in RETRYABLE_STATUS:
                time.sleep(2**attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise
        except http.client.RemoteDisconnected:
            if attempt < config.http_retries:
                time.sleep(2**attempt)
                continue
            raise
