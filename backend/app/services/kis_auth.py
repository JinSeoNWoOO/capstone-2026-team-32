import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")

# 이 프로토타입은 실전 주문을 지원하지 않는다.
BASE_URL = "https://openapivts.koreainvestment.com:29443"
REQUEST_TIMEOUT = 10

ACCESS_TOKEN: str | None = None
ACCESS_TOKEN_EXPIRES_AT = 0.0
TOKEN_CACHE_PATH = ENV_PATH.parent / ".kis-token-cache.json"
TOKEN_LOCK = threading.Lock()


class KisAuthError(RuntimeError):
    """KIS 인증 또는 해시키 발급 실패."""


def _require_credentials() -> None:
    missing = [
        name
        for name, value in (
            ("KIS_APP_KEY", APP_KEY),
            ("KIS_APP_SECRET", APP_SECRET),
        )
        if not value
    ]
    if missing:
        raise KisAuthError(f"필수 환경변수가 없습니다: {', '.join(missing)}")


def _app_key_fingerprint() -> str:
    return hashlib.sha256((APP_KEY or "").encode("utf-8")).hexdigest()


def _load_cached_token() -> tuple[str, float] | None:
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        token = data.get("access_token")
        expires_at = float(data.get("expires_at", 0))
        if (
            token
            and data.get("app_key_fingerprint") == _app_key_fingerprint()
            and time.time() < expires_at - 60
        ):
            return token, expires_at
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _save_cached_token(token: str, expires_at: float) -> None:
    temporary_path = TOKEN_CACHE_PATH.with_suffix(".json.tmp")
    payload = {
        "access_token": token,
        "expires_at": expires_at,
        "app_key_fingerprint": _app_key_fingerprint(),
    }
    try:
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        temporary_path.replace(TOKEN_CACHE_PATH)
    except OSError:
        # 디스크 캐시에 실패해도 현재 프로세스의 메모리 토큰은 계속 사용한다.
        pass


def get_access_token() -> str:
    global ACCESS_TOKEN, ACCESS_TOKEN_EXPIRES_AT

    if ACCESS_TOKEN and time.time() < ACCESS_TOKEN_EXPIRES_AT - 60:
        return ACCESS_TOKEN

    with TOKEN_LOCK:
        if ACCESS_TOKEN and time.time() < ACCESS_TOKEN_EXPIRES_AT - 60:
            return ACCESS_TOKEN

        cached = _load_cached_token()
        if cached:
            ACCESS_TOKEN, ACCESS_TOKEN_EXPIRES_AT = cached
            return ACCESS_TOKEN

        _require_credentials()

        try:
            response = requests.post(
                f"{BASE_URL}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": APP_KEY,
                    "appsecret": APP_SECRET,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise KisAuthError("KIS 접근 토큰 요청에 실패했습니다.") from exc

        token = data.get("access_token")
        if not token:
            message = data.get("error_description") or data.get("msg1") or "알 수 없는 오류"
            raise KisAuthError(f"KIS 접근 토큰 발급 실패: {message}")

        ACCESS_TOKEN = token
        ACCESS_TOKEN_EXPIRES_AT = time.time() + max(int(data.get("expires_in", 86400)), 0)
        _save_cached_token(ACCESS_TOKEN, ACCESS_TOKEN_EXPIRES_AT)
        return ACCESS_TOKEN


def get_hash_key(body: dict[str, Any]) -> str:
    """주문 POST 본문에 대한 KIS 해시키를 발급한다."""
    _require_credentials()

    try:
        response = requests.post(
            f"{BASE_URL}/uapi/hashkey",
            headers={
                "Content-Type": "application/json",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET,
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise KisAuthError("KIS 해시키 요청에 실패했습니다.") from exc

    hash_key = data.get("HASH")
    if not hash_key:
        raise KisAuthError("KIS 해시키 응답에 HASH 값이 없습니다.")
    return hash_key
