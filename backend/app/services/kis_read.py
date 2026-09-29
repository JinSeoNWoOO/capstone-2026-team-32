"""Retry helpers for idempotent KIS read APIs."""

import os
import time
from typing import Any

import requests

from backend.app.services.kis_auth import REQUEST_TIMEOUT


def _positive_number_from_env(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(value, maximum)


READ_MAX_ATTEMPTS = int(_positive_number_from_env("KIS_READ_MAX_ATTEMPTS", 3, 5))
READ_RETRY_BASE_SECONDS = _positive_number_from_env(
    "KIS_READ_RETRY_BASE_SECONDS", 0.7, 10
)


class KisReadError(RuntimeError):
    """A KIS read request failed after bounded retries."""


def get_kis_json(
    url: str,
    *,
    headers: dict[str, str | None],
    params: dict[str, str | None],
    failure_message: str,
) -> dict[str, Any]:
    """Call a side-effect-free KIS GET endpoint with exponential backoff."""
    last_message = failure_message

    for attempt in range(1, READ_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("KIS response is not a JSON object")
            if data.get("rt_cd") == "0":
                return data
            last_message = data.get("msg1") or failure_message
        except (requests.RequestException, ValueError):
            last_message = failure_message

        if attempt < READ_MAX_ATTEMPTS:
            time.sleep(READ_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    raise KisReadError(f"{last_message} ({READ_MAX_ATTEMPTS}회 시도 후 실패)")
