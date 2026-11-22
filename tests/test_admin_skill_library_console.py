import asyncio
import base64
from contextlib import closing
import json
import sqlite3

import httpx
import pytest

from jx_alert.admin_console import SESSION
from test_admin_console import ORIGIN, console, login  # noqa: F401


def test_owner_browser_upload_review_and_private_inspect(console, tmp_path):  # noqa: F811 - imported fixture
    client, _, _, settings = console
    settings.agent_skill_library_dir = str(tmp_path / "library")
    path = "/console/api/skill-library"
    source = "---\nname: preview-only\ndescription: Test\n---\n# Demo\n<b>literal</b>"
    upload = {"action": "upload", "filename": "SKILL.md", "revision": 0,
              "content_base64": base64.b64encode(source.encode()).decode()}
    assert client.get(path).status_code == client.get(path + "/preview-only").status_code == 401
    assert client.post(path, json=upload).status_code == 401
    assert login(client).is_redirect
    page = client.get("/console/")
    assert 'id="skill-library-panel"' in page.text
    assert page.text.index("skill-library.js") < page.text.index("/console/assets/console.js")
    js = client.get("/console/assets/skill-library.js")
    assert js.status_code == 200 and "innerHTML" not in js.text
    rev = client.get(path)
    assert rev.json()["revision"] == 0 and rev.headers["cache-control"] == "no-store"
    headers = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/console/api/stores").json()["csrf"]}
    assert client.post(path, json=upload, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(path, json=upload, headers={**headers, "Origin": "https://test.invalid"}).status_code == 403
    assert client.post(path, content=b"x" * (24 * 1024 + 1), headers={
        **headers, "content-type": "application/json"}).status_code == 413
    assert client.post(path, content=b"file", headers={**headers, "content-type": "application/zip"}).status_code == 415
    updated = client.post(path, json=upload, headers=headers)
    assert updated.status_code == 200 and not updated.json()["skills"][0]["enabled"]
    assert client.post(path, json=upload, headers=headers).status_code == 409
    detail = client.get(path + "/preview-only?version=1")
    assert detail.headers["content-type"].startswith("application/json")
    assert detail.json()["document"]["source"] == source
    assert client.get(path + "/preview-only?version=1&version=2").status_code == 400
    assert client.get(path + "/preview-only?version=2").status_code == 404
    assert client.get(path + "/preview-only", headers={"host": "unknown.invalid"}).status_code == 400
    assert client.post("/console/logout", headers=headers).status_code == 303
    assert client.get(path + "/preview-only").status_code == 401


@pytest.mark.parametrize("revoke", ["logout", "expiry"])
def test_upload_rechecks_session_after_receiving_body(console, tmp_path, revoke):  # noqa: F811
    client, _, _, settings = console
    root = tmp_path / "library"
    settings.agent_skill_library_dir = str(root)
    assert login(client).is_redirect
    headers = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/console/api/stores").json()["csrf"],
               "Content-Type": "application/json"}
    cookie = {SESSION: client.cookies.get(SESSION)}
    source = b"---\nname: delayed-review\ndescription: Check dates\n---\n# Report"
    body = json.dumps({"action": "upload", "filename": "SKILL.md", "revision": 0,
                       "content_base64": base64.b64encode(source).decode()}).encode()

    async def send_upload():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url=ORIGIN, cookies=cookie) as isolated:
            async def chunks():
                yield body[:32]
                if revoke == "logout":
                    logout = await isolated.post("/console/logout", headers=headers)
                    assert logout.status_code == 303
                else:
                    # Advance the fixture's session past expiry without sleeping or relying on timing.
                    with closing(sqlite3.connect(tmp_path / "console-sessions.sqlite3")) as db, db:
                        db.execute("UPDATE sessions SET expires=0")
                yield body[32:]

            return await isolated.post("/console/api/skill-library", content=chunks(), headers=headers)

    response = asyncio.run(send_upload())
    assert response.status_code == 401
    assert not root.exists()
