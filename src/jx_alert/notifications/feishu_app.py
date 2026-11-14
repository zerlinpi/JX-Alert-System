"""One enterprise app bot for group mentions and individual manager messages.

The durable outbox owns long-term idempotency. Retries also reuse the API uuid.

Official contracts:
https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
https://open.feishu.cn/document/server-docs/im-v1/message/create
https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json
"""
import hashlib
import html
import json
from pathlib import Path
import re
import threading
import time

import httpx


class FeishuAppError(RuntimeError):
    """A sanitized failure that can safely be stored in notification logs."""


class FeishuAppClient:
    """Send as one app identity; cache its tenant token only in server memory."""

    _TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
    _MESSAGE_PATH = "/open-apis/im/v1/messages"
    _EXPIRED_TOKEN_CODE = 99991663

    def __init__(self, app_id: str, app_secret: str, timeout: int = 20,
                 base_url: str = "https://open.feishu.cn"):
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("FEISHU_APP_ID is required for app bot delivery")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("FEISHU_APP_SECRET is required for app bot delivery")
        if base_url.rstrip("/") not in {
            "https://open.feishu.cn", "https://open.larksuite.com"
        }:
            raise ValueError("Feishu app base URL must be an official Feishu/Lark HTTPS origin")
        if timeout <= 0:
            raise ValueError("Feishu app timeout must be positive")
        self._app_id = app_id.strip()
        self._app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings):
        """Use configured credentials or a private, unlogged credential file."""
        app_id = getattr(settings, "feishu_app_id", "")
        secret = getattr(settings, "feishu_app_secret", "")
        secret = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        if not app_id or not secret:
            path = getattr(settings, "feishu_app_credentials_file", "")
            if path:
                try:
                    credentials = json.loads(Path(path).read_text(encoding="utf-8"))
                except FileNotFoundError:
                    credentials = {}
                except (OSError, ValueError):
                    raise FeishuAppError("Feishu app credentials file cannot be read") from None
                if not isinstance(credentials, dict):
                    raise FeishuAppError("Feishu app credentials file must contain an object")
                # Never combine an app ID from one application with another's secret.
                if app_id and credentials.get("app_id") not in (None, app_id):
                    raise FeishuAppError("Feishu app credentials contain conflicting app IDs")
                if secret and secret != credentials.get("app_secret"):
                    raise FeishuAppError("Feishu app credentials contain conflicting secrets")
                app_id = app_id or credentials.get("app_id", "")
                secret = secret or credentials.get("app_secret", "")
        return cls(app_id=app_id, app_secret=secret, timeout=settings.feishu_timeout)

    def _post_json(self, path: str, payload: dict, *, token: str | None = None,
                   params: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.post(
                    self.base_url + path, headers=headers, params=params, json=payload,
                )
        except httpx.HTTPError as exc:
            # Do not propagate response bodies, request headers, URLs or causes.
            raise FeishuAppError(
                f"Feishu app HTTP request failed: {type(exc).__name__}"
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise FeishuAppError("Feishu app returned invalid JSON") from None
        if not isinstance(data, dict):
            raise FeishuAppError("Feishu app returned an unexpected response")
        code = data.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise FeishuAppError("Feishu app response did not contain a valid status code")
        if not 200 <= response.status_code < 300:
            # An expired token can be reported with a non-2xx HTTP status.
            message_path = path == self._MESSAGE_PATH or re.fullmatch(
                re.escape(self._MESSAGE_PATH) + r"/om_[A-Za-z0-9_-]+/reply", path)
            if not (message_path and code == self._EXPIRED_TOKEN_CODE):
                raise FeishuAppError(
                    f"Feishu app HTTP request rejected: HTTP {response.status_code}, code={code}"
                )
        return data

    def _get_token(self, *, rejected_token: str | None = None) -> str:
        with self._token_lock:
            now = time.monotonic()
            if (
                self._token and now < self._token_expires_at
                and self._token != rejected_token
            ):
                return self._token
            self._token = None
            self._token_expires_at = 0.0
            data = self._post_json(self._TOKEN_PATH, {
                "app_id": self._app_id, "app_secret": self._app_secret,
            })
            if data["code"] != 0:
                raise FeishuAppError(f"Feishu app token request rejected: code={data['code']}")
            token, expires_in = data.get("tenant_access_token"), data.get("expire")
            if (
                not isinstance(token, str) or not token.strip()
                or not isinstance(expires_in, int) or isinstance(expires_in, bool)
                or expires_in <= 0
            ):
                raise FeishuAppError("Feishu app returned an invalid token response")
            # Use the server's actual lifetime and leave a refresh margin, also
            # for unusually short-lived tokens. Never hardcode a two-hour TTL.
            refresh_margin = min(60.0, expires_in * 0.1)
            self._token = token
            self._token_expires_at = now + expires_in - refresh_margin
            return token

    def send_text(self, text: str, *, receive_id: str,
                  receive_id_type: str = "chat_id",
                  open_ids: list[str] | None = None,
                  idempotency_key: str) -> dict:
        """Send text and return ``{"message_id": ...}`` on confirmed success.

        The key must identify one delivery to one recipient. Retries must reuse
        it; separate group and individual deliveries must use distinct keys.
        Only mapped Open IDs can create mentions. Data cannot inject @all tags.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Feishu message text cannot be empty")
        if receive_id_type not in {"chat_id", "open_id"}:
            raise ValueError("Feishu receive_id_type must be chat_id or open_id")
        prefix = "oc_" if receive_id_type == "chat_id" else "ou_"
        if not isinstance(receive_id, str) or not re.fullmatch(
            re.escape(prefix) + r"[A-Za-z0-9_-]+", receive_id,
        ):
            raise ValueError("Feishu message recipient has an invalid ID")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Feishu message idempotency_key is required")
        if open_ids is not None and (
            not isinstance(open_ids, list) or not all(
                isinstance(uid, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid)
                for uid in open_ids
            )
        ):
            raise ValueError("Feishu mentions must contain valid Open IDs")
        mentions = "".join(
            f'<at user_id="{uid}">提醒</at> '
            for uid in dict.fromkeys(open_ids or [])
        )
        content = {"text": mentions + html.escape(text, quote=False)}
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps(content, ensure_ascii=False),
            "uuid": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:40],
        }
        return self._send_message_payload(payload, receive_id_type)

    def send_card(self, card: dict, *, receive_id: str, receive_id_type: str = "chat_id",
                  open_ids: list[str] | None = None, idempotency_key: str) -> dict:
        """Deliver a native table card with only explicitly mapped mentions."""
        from jx_alert.notifications.cards import with_mentions
        if receive_id_type not in {"chat_id", "open_id"}:
            raise ValueError("Feishu message receive_id_type is invalid")
        prefix = "oc_" if receive_id_type == "chat_id" else "ou_"
        if not isinstance(receive_id, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]+", receive_id):
            raise ValueError("Feishu message recipient has an invalid ID")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Feishu message idempotency_key is required")
        payload = {
            "receive_id": receive_id, "msg_type": "interactive",
            "content": json.dumps(with_mentions(card, open_ids or []), ensure_ascii=False),
            "uuid": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:40],
        }
        return self._send_message_payload(payload, receive_id_type)

    def reply_card(self, message_id: str, card: dict, *, idempotency_key: str) -> dict:
        """Reply in the original question's thread with a stable retry UUID."""
        from jx_alert.notifications.cards import with_mentions
        if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
            raise ValueError("Feishu reply requires a valid original message ID")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Feishu reply requires an idempotency key")
        payload = {"msg_type": "interactive", "content": json.dumps(with_mentions(card, []), ensure_ascii=False),
                   "uuid": hashlib.sha256(idempotency_key.encode()).hexdigest()[:40], "reply_in_thread": True}
        return self._send_message_payload(payload, None, path=f"{self._MESSAGE_PATH}/{message_id}/reply")

    def _send_message_payload(self, payload, receive_id_type, *, path=None):
        # Bound operational alerts conservatively, including JSON escaping.
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 20 * 1024:
            raise ValueError("Feishu app text message exceeds the 20 KB application limit")
        token = self._get_token()
        for attempt in range(2):
            data = self._post_json(
                path or self._MESSAGE_PATH, payload, token=token,
                params={"receive_id_type": receive_id_type} if receive_id_type else None,
            )
            if data["code"] == self._EXPIRED_TOKEN_CODE and attempt == 0:
                token = self._get_token(rejected_token=token)
                continue
            if data["code"] != 0:
                raise FeishuAppError(f"Feishu app message rejected: code={data['code']}")
            result = data.get("data")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, str) or not message_id.strip():
                raise FeishuAppError("Feishu app success response did not contain a message_id")
            return {"message_id": message_id}
        raise FeishuAppError("Feishu app message could not be sent")
