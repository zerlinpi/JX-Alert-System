import hashlib
import base64
import json
import time
from urllib.parse import urljoin
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

class LingxingError(RuntimeError): pass

def build_signature(params: dict, *, access_token: str, app_key: str, app_id: str) -> str:
    values = {**params, "access_token": access_token, "app_key": app_key,
              "timestamp": params.get("timestamp", int(time.time()))}
    raw = "".join(f"{k}={values[k]}&" for k in sorted(values, key=lambda x: x.encode("ascii"))).rstrip("&")
    digest = hashlib.md5(raw.encode()).hexdigest().upper()
    padder = PKCS7(128).padder()
    padded = padder.update(digest.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(app_id.encode()), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode()

class LingxingClient:
    """Read-only Lingxing API client. Token endpoint is deliberately configurable."""
    def __init__(self, *, base_url: str, app_id: str, app_key: str, access_token: str = "",
                 timeout: int = 30):
        self.base_url, self.app_id, self.app_key = base_url.rstrip("/") + "/", app_id, app_key
        self.access_token, self.timeout = access_token, timeout

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, *, advertising: bool = False):
        if not self.access_token:
            raise LingxingError("LINGXING_ACCESS_TOKEN is not configured")
        params = dict(params or {})
        timestamp = int(time.time())
        params["timestamp"] = timestamp
        signature = build_signature(params, access_token=self.access_token,
                                     app_key=self.app_key, app_id=self.app_id)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if advertising:
            headers["X-API-VERSION"] = "2"
        query = {**params, "sign": signature}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(method, urljoin(self.base_url, path.lstrip("/")),
                                      params=query, json=json_body, headers=headers)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0, "0", 200, "200"):
            raise LingxingError(f"Lingxing API error: code={data.get('code')} message={data.get('msg') or data.get('message')}")
        return data

    def get_stores(self):
        return self._request("GET", "/erp/sc/data/seller/lists")

    def sales_daily(self, payload: dict):
        return self._request("POST", "/erp/sc/data/sales_report/asinDailyLists", json_body=payload)

    def advertising_report(self, report_type: str, payload: dict):
        paths = {"sp": "/pb/openapi/newad/spCampaignReports",
                 "sb": "/pb/openapi/newad/hsaCampaignReports",
                 "sd": "/pb/openapi/newad/sdCampaignReports"}
        if report_type not in paths: raise ValueError("report_type must be sp, sb or sd")
        return self._request("POST", paths[report_type], json_body=payload, advertising=True)

    def performance(self, path: str, payload: dict):
        if not path.startswith("/"):
            raise ValueError("performance API path must be an absolute API path")
        return self._request("POST", path, json_body=payload)
