"""Configurable Lingxing token acquisition/renewal.

The exact token endpoint and field names vary by the account's current API docs.
This module intentionally requires explicit paths and response-field mappings.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import httpx

from jx_alert.lingxing.client import LingxingError


def _render_placeholders(value, values):
    if isinstance(value, dict):
        return {k: _render_placeholders(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_placeholders(v, values) for v in value]
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("${" + key + "}", replacement)
        return value
    return value


def _json_payload(template: str, values: dict) -> dict:
    try:
        parsed = json.loads(template or "{}")
    except json.JSONDecodeError as exc:
        raise LingxingError("Lingxing token request JSON is invalid") from exc
    rendered = _render_placeholders(parsed, values)
    if not isinstance(rendered, dict):
        raise LingxingError("Lingxing token request body must be a JSON object")
    return rendered


def _read_path(data: dict, dotted_path: str):
    if not dotted_path:
        raise LingxingError("Token response JSON path is not configured")
    value = data
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise LingxingError(f"Token response is missing configured field path: {dotted_path}")
        value = value[part]
    return value


def request_token(*, base_url: str, path: str, request_json: str,
                  timeout: int = 30, method: str = "POST") -> dict:
    if not path:
        raise LingxingError("Token endpoint path is not configured")
    payload = _json_payload(request_json, {})
    with httpx.Client(timeout=timeout) as client:
        if method.upper() == "GET":
            response = client.get(base_url.rstrip("/") + "/" + path.lstrip("/"), params=payload)
        elif method.upper() == "POST":
            response = client.post(base_url.rstrip("/") + "/" + path.lstrip("/"), json=payload)
        else:
            raise LingxingError("Token method must be GET or POST")
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise LingxingError("Unexpected token response type")
    if data.get("code") not in (None, 0, "0", 200, "200"):
        raise LingxingError(
            f"Lingxing token endpoint error: code={data.get('code')}, "
            f"message={data.get('msg') or data.get('message') or 'unknown'}"
        )
    return data


class LingxingTokenProvider:
    def __init__(self, settings):
        self.settings = settings

    def _load(self) -> dict:
        path = Path(self.settings.lingxing_token_store_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self, state: dict) -> None:
        path = Path(self.settings.lingxing_token_store_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(prefix="lingxing-token-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _call(self, *, path: str, template: str, method: str,
              refresh_token: str = "", access_token: str = "") -> dict:
        s = self.settings
        values = {
            "LINGXING_APP_ID": s.lingxing_app_id,
            "LINGXING_APP_SECRET": s.lingxing_app_secret,
            "LINGXING_ACCESS_TOKEN": access_token,
            "LINGXING_REFRESH_TOKEN": refresh_token,
        }
        payload = _json_payload(template, values)
        with httpx.Client(timeout=s.lingxing_timeout) as client:
            if method.upper() == "GET":
                response = client.get(s.lingxing_base_url.rstrip("/") + "/" + path.lstrip("/"),
                                      params=payload)
            elif method.upper() == "POST":
                response = client.post(s.lingxing_base_url.rstrip("/") + "/" + path.lstrip("/"),
                                       json=payload)
            else:
                raise LingxingError("Token method must be GET or POST")
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or data.get("code") not in (None, 0, "0", 200, "200"):
            code = data.get("code") if isinstance(data, dict) else "invalid-response"
            raise LingxingError(f"Lingxing token endpoint failed: code={code}")
        return data

    def get_access_token(self) -> str:
        s = self.settings
        if s.lingxing_access_token:
            return s.lingxing_access_token
        if not s.lingxing_app_id or not s.lingxing_app_secret:
            raise LingxingError("LINGXING_APP_ID and LINGXING_APP_SECRET are required")

        state = self._load()
        now = int(time.time())
        access_is_valid = (
            bool(state.get("access_token"))
            and int(state.get("expires_at", 0)) > now + 300
        )
        cached_refresh_token = state.get("refresh_token") or s.lingxing_refresh_token
        refresh_expires_at = int(state.get("refresh_expires_at", 0) or 0)
        # Lingxing refresh tokens expire after two hours and are single-use.
        # Renew before their own expiry, even if the access token remains valid.
        refresh_is_due = bool(
            cached_refresh_token
            and s.lingxing_refresh_path
            and refresh_expires_at <= now + 900
        )

        if access_is_valid and not refresh_is_due:
            return state["access_token"]

        data = None
        if cached_refresh_token and s.lingxing_refresh_path:
            if not s.lingxing_refresh_response_path:
                raise LingxingError(
                    "LINGXING_REFRESH_RESPONSE_PATH is required because Lingxing rotates refresh tokens."
                )
            try:
                data = self._call(
                    path=s.lingxing_refresh_path,
                    template=s.lingxing_refresh_request_json,
                    method=s.lingxing_refresh_method,
                    refresh_token=cached_refresh_token,
                    access_token=state.get("access_token", ""),
                )
            except LingxingError:
                # A refresh token may have expired while the service was offline.
                # Fall back to a fresh AppID/AppSecret token instead of becoming stuck.
                data = None

        if data is None:
            if not s.lingxing_token_path:
                raise LingxingError(
                    "LINGXING_TOKEN_PATH is not configured. Copy the exact token endpoint "
                    "and request fields from your Lingxing OpenAPI documentation."
                )
            data = self._call(
                path=s.lingxing_token_path,
                template=s.lingxing_token_request_json,
                method=s.lingxing_token_method,
            )

        access_token = _read_path(data, s.lingxing_token_response_path)
        refresh_token = (
            _read_path(data, s.lingxing_refresh_response_path)
            if s.lingxing_refresh_response_path else cached_refresh_token
        )
        expires_in = _read_path(data, s.lingxing_expires_in_response_path)
        try:
            expires_at = now + int(expires_in)
        except (TypeError, ValueError) as exc:
            raise LingxingError("Configured token expiry field must contain TTL seconds") from exc
        if not isinstance(access_token, str) or not access_token:
            raise LingxingError("Configured access-token response field is empty or not a string")
        new_state = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "refresh_expires_at": now + 7200 if refresh_token else 0,
        }
        self._save(new_state)
        return access_token
