import httpx

class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn"):
        self.app_id, self.app_secret, self.base_url = app_id, app_secret, base_url.rstrip("/")
    def send_text(self, chat_id: str, text: str, open_ids: list[str] | None = None) -> dict:
        if not self.app_id or not self.app_secret: raise RuntimeError("Feishu app credentials missing")
        with httpx.Client(timeout=20) as c:
            token_resp = c.post(self.base_url + "/open-apis/auth/v3/tenant_access_token/internal",
                                json={"app_id": self.app_id, "app_secret": self.app_secret})
            token_resp.raise_for_status(); token_data = token_resp.json()
            if token_data.get("code") != 0: raise RuntimeError(f"Feishu token error: {token_data}")
            mentions = "".join(f'<at user_id="{uid}"></at> ' for uid in (open_ids or []))
            content = {"text": mentions + text}
            resp = c.post(self.base_url + "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token_data['tenant_access_token']}"},
                json={"receive_id": chat_id, "msg_type": "text",
                      "content": __import__("json").dumps(content, ensure_ascii=False)})
            resp.raise_for_status(); data = resp.json()
            if data.get("code") != 0: raise RuntimeError(f"Feishu send error: {data}")
            return data.get("data", {})
