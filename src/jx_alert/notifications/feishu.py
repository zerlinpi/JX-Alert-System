"""Feishu group custom-bot webhook client (not an app bot)."""
import base64
import hashlib
import hmac
import html
import time
from urllib.parse import urlparse

import httpx


class FeishuWebhookError(RuntimeError):
    pass


def build_feishu_signature(timestamp: str, secret: str) -> str:
    """Feishu custom-bot signature: HMAC-SHA256(key=timestamp + '\n' + secret, msg='')."""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class FeishuClient:
    """Send messages to the single group bound to a Feishu custom-bot webhook."""

    def __init__(self, webhook_url: str, sign_secret: str = "", keyword: str = "",
                 timeout: int = 20):
        parsed = urlparse(webhook_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "open.feishu.cn", "open.larksuite.com"
        } or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
            raise ValueError("FEISHU_WEBHOOK_URL must be a Feishu/Lark custom-bot HTTPS webhook")
        self.webhook_url = webhook_url
        self.sign_secret = sign_secret
        self.keyword = keyword.strip()
        self.timeout = timeout

    def _payload(self, text: str, open_ids: list[str] | None = None) -> dict:
        mentions = "".join(
            f'<at user_id="{html.escape(uid, quote=True)}">提醒</at> '
            for uid in (open_ids or [])
        )
        message = mentions + text
        if self.keyword and self.keyword not in message:
            message = f"{self.keyword}\n{message}"
        payload = {"msg_type": "text", "content": {"text": message}}
        if self.sign_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = build_feishu_signature(timestamp, self.sign_secret)
        return payload

    def send_text(self, text: str, open_ids: list[str] | None = None) -> dict:
        if not text.strip():
            raise ValueError("Feishu message text cannot be empty")
        payload = self._payload(text, open_ids)
        # Feishu custom-bot webhook payloads are limited to 20 KB.
        import json
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 20 * 1024:
            raise ValueError("Feishu custom-bot message exceeds the 20 KB payload limit")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    self.webhook_url,
                    content=encoded,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            # Do not include the URL: it contains the secret webhook token.
            raise FeishuWebhookError(f"Feishu webhook HTTP request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise FeishuWebhookError("Feishu webhook returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise FeishuWebhookError("Feishu webhook returned an unexpected response")
        if "code" in data:
            code = data["code"]
        elif "StatusCode" in data:
            code = data["StatusCode"]
        else:
            raise FeishuWebhookError("Feishu webhook response did not contain a status code")
        if code not in (0, "0"):
            raise FeishuWebhookError(
                f"Feishu webhook rejected message: code={code}, "
                f"message={data.get('msg') or data.get('StatusMessage') or 'unknown'}"
            )
        return data
