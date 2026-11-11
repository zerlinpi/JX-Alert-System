import json
import httpx
from jx_alert.lingxing.client import LingxingError

def request_token(*, base_url: str, path: str, request_json: str, timeout: int = 30) -> dict:
    """Call a token endpoint explicitly configured from the user's Lingxing API docs."""
    if not path:
        raise LingxingError("Token endpoint path is not configured; set LINGXING_TOKEN_PATH")
    try:
        payload = json.loads(request_json or "{}")
    except json.JSONDecodeError as exc:
        raise LingxingError("Token request JSON is invalid") from exc
    with httpx.Client(timeout=timeout) as client:
        response = client.post(base_url.rstrip("/") + "/" + path.lstrip("/"), json=payload)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise LingxingError("Unexpected token response type")
    return data
