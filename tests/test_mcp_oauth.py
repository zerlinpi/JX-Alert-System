import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import TokenError
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jx_alert.mcp_oauth import COOKIE, SCOPES, SqliteOAuthProvider, create_owner_credentials, _callback_origin

ISSUER = "https://jx.example.com/"
RESOURCE = "https://jx.example.com/mcp"
REDIRECT = "https://chatgpt.com/connector/oauth/test-callback"
PASSWORD = "a test owner password with enough length"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def run(awaitable):
    return asyncio.run(awaitable)


def app_for(provider):
    routes = create_auth_routes(
        provider, AnyHttpUrl(ISSUER),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=list(SCOPES), default_scopes=["jx:read"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ) + provider.owner_routes()
    return provider.auth_guard(Starlette(routes=routes))


@pytest.fixture
def setup(tmp_path):
    credentials = tmp_path / "owner.json"
    create_owner_credentials(credentials, PASSWORD)
    options = dict(database_path=tmp_path / "oauth.sqlite", credentials_path=credentials,
                   issuer_url=ISSUER, resource_url=RESOURCE)
    provider = SqliteOAuthProvider(**options)
    with TestClient(app_for(provider), base_url=ISSUER, follow_redirects=False) as client:
        yield provider, client, options


def register(client, method="none", scope="jx:read jx:collect"):
    response = client.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": scope,
    })
    assert response.status_code == 201, response.text
    return response.json()


def begin(client, account, **overrides):
    params = {
        "client_id": account["client_id"], "redirect_uri": REDIRECT,
        "response_type": "code", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
        "scope": "jx:read jx:collect", "state": "opaque-state", **overrides,
    }
    response = client.get("/authorize", params={key: value for key, value in params.items() if value is not None})
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert "/oauth/owner?request=" in location, location
    page = client.get(location)
    assert page.status_code == 200
    request_id = re.search(r'name="request" value="([^"]+)"', page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return request_id, csrf, page


def owner_post(client, request_id, csrf, **fields):
    return client.post("/oauth/owner", data={"request": request_id, "csrf": csrf, **fields},
                       headers={"Origin": ISSUER.rstrip("/")})


def code_for(client, account):
    request_id, csrf, page = begin(client, account)
    logged_in = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    assert logged_in.status_code == 200
    assert "同意" in logged_in.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', logged_in.text)[1]
    response = owner_post(client, request_id, csrf, action="approve")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["opaque-state"]
    assert query["iss"] == [ISSUER]
    return query["code"][0]


def exchange(client, account, code, **overrides):
    body = {
        "grant_type": "authorization_code", "client_id": account["client_id"],
        "code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
        "resource": RESOURCE, **overrides,
    }
    headers = {}
    if account.get("token_endpoint_auth_method") == "client_secret_post":
        body["client_secret"] = account["client_secret"]
    if account.get("token_endpoint_auth_method") == "client_secret_basic":
        credentials = base64.b64encode(f'{account["client_id"]}:{account["client_secret"]}'.encode()).decode()
        headers["Authorization"] = "Basic " + credentials
    return client.post("/token", data=body, headers=headers)


def refresh(client, account, token, **overrides):
    return client.post("/token", data={
        "grant_type": "refresh_token", "client_id": account["client_id"],
        "refresh_token": token, "resource": RESOURCE, **overrides,
    })


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
def test_sdk_public_and_confidential_clients_complete_owner_approved_pkce_flow(setup, method):
    provider, client, options = setup
    account = register(client, method)
    code = code_for(client, account)
    response = exchange(client, account, code)
    assert response.status_code == 200, response.text
    result = response.json()
    token = run(provider.load_access_token(result["access_token"]))
    assert token.client_id == account["client_id"]
    assert token.scopes == ["jx:read", "jx:collect"]
    assert token.resource == RESOURCE and token.subject == "owner"
    assert response.headers["cache-control"] == "no-store"
    raw_db = options["database_path"].read_bytes()
    for secret in (PASSWORD, code, result["access_token"], result["refresh_token"]):
        assert secret.encode() not in raw_db


def test_bad_pkce_does_not_issue_or_consume_a_code_and_good_code_is_single_use(setup):
    provider, client, options = setup
    account = register(client)
    code = code_for(client, account)
    failed = exchange(client, account, code, code_verifier="b" * 64)
    assert failed.status_code == 400
    assert failed.json()["error"] == "invalid_grant"
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    assert exchange(client, account, code).status_code == 200
    assert exchange(client, account, code).status_code == 400


def test_resource_redirect_and_client_binding_are_not_bypassed_at_exchange(setup):
    provider, client, options = setup
    account, other = register(client), register(client)
    code = code_for(client, account)
    wrong_target = exchange(client, account, code, resource=ISSUER + ".well-known/oauth-authorization-server")
    assert wrong_target.status_code == 400
    assert wrong_target.json()["error"] == "invalid_target"
    assert wrong_target.json()["expected_resource"] == RESOURCE
    assert code not in wrong_target.text
    assert exchange(client, account, code, redirect_uri="https://evil.example/callback").status_code == 400
    assert exchange(client, other, code).status_code == 400
    assert exchange(client, account, code).status_code == 200


def test_owner_must_login_and_post_valid_csrf_from_same_origin(setup):
    provider, client, options = setup
    account = register(client)
    request_id, csrf, page = begin(client, account)
    assert owner_post(client, request_id, csrf, action="approve").status_code == 403
    assert owner_post(client, request_id, "forged", action="login", password=PASSWORD).status_code == 403
    forged_origin = client.post("/oauth/owner", data={
        "request": request_id, "csrf": csrf, "action": "login", "password": PASSWORD,
    }, headers={"Origin": "https://evil.example"})
    assert forged_origin.status_code == 403
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
    cookie = page.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "domain=" not in cookie
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


def test_login_rotates_browser_session_and_incorrect_password_is_generic(setup):
    provider, client, options = setup
    account = register(client)
    request_id, csrf, page = begin(client, account)
    original_cookie = client.cookies.get(COOKIE)
    failed = owner_post(client, request_id, csrf, action="login", password="wrong-password")
    assert "wrong-password" not in failed.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', failed.text)[1]
    success = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    assert "同意" in success.text
    assert client.cookies.get(COOKIE) != original_cookie
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM browsers WHERE authenticated=0").fetchone()[0] == 0


def test_refresh_rotation_scope_restrictions_and_replay_revoke_active_family(setup):
    provider, client, options = setup
    account, other = register(client), register(client)
    first = exchange(client, account, code_for(client, account)).json()
    assert refresh(client, other, first["refresh_token"]).status_code == 400
    assert refresh(client, account, first["refresh_token"], scope="jx:admin").status_code == 400
    assert refresh(client, account, first["refresh_token"], resource="https://evil.example/mcp").status_code == 400
    response = refresh(client, account, first["refresh_token"], scope="jx:read")
    assert response.status_code == 200
    second = response.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == "jx:read"
    assert run(provider.load_access_token(first["access_token"])) is None
    assert run(provider.load_access_token(second["access_token"])) is not None
    assert refresh(client, account, first["refresh_token"]).status_code == 400
    assert run(provider.load_access_token(second["access_token"])) is None
    assert refresh(client, account, second["refresh_token"]).status_code == 400


def test_revoke_either_token_revokes_its_associated_pair(setup):
    provider, client, options = setup
    account = register(client)
    first = exchange(client, account, code_for(client, account)).json()
    response = client.post("/revoke", data={
        "client_id": account["client_id"], "token": first["access_token"],
    })
    assert response.status_code == 200
    assert run(provider.load_access_token(first["access_token"])) is None
    assert refresh(client, account, first["refresh_token"]).status_code == 400


def test_read_only_chatgpt_client_can_request_workflow_scopes_with_fresh_owner_consent(setup):
    provider, client, _ = setup
    account = register(client, scope="jx:read")
    request_id, csrf, _ = begin(client, account, scope=None)
    login = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
    callback = owner_post(client, request_id, csrf, action="approve")
    code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
    original = exchange(client, account, code).json()
    assert original["scope"] == "jx:read"
    workflow = "jx:read jx:collect jx:notify"
    assert refresh(client, account, original["refresh_token"], scope=workflow).json()["error"] == "invalid_scope"
    assert refresh(client, account, original["refresh_token"]).json()["scope"] == "jx:read"

    request_id, csrf, consent = begin(client, account, scope=workflow)
    assert "请求采集与分析任务" in consent.text
    assert "发送至已配置飞书群" in consent.text
    callback = owner_post(client, request_id, csrf, action="approve")
    code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
    upgraded = exchange(client, account, code).json()
    assert upgraded["scope"] == workflow
    assert run(provider.load_access_token(upgraded["access_token"])).scopes == list(SCOPES)
    narrowed = refresh(client, account, upgraded["refresh_token"], scope="jx:read").json()
    assert narrowed["scope"] == "jx:read"
    assert run(provider.load_access_token(narrowed["access_token"])).scopes == ["jx:read"]
    assert refresh(client, account, narrowed["refresh_token"]).json()["scope"] == workflow


def test_restart_preserves_client_and_authorization_code_then_access_tokens(setup):
    provider, client, options = setup
    account = register(client)
    code = code_for(client, account)
    restored = SqliteOAuthProvider(**options)
    with TestClient(app_for(restored), base_url=ISSUER) as second:
        result = exchange(second, account, code)
        assert result.status_code == 200
        token = result.json()["access_token"]
    again = SqliteOAuthProvider(**options)
    assert run(again.get_client(account["client_id"])) is not None
    assert run(again.load_access_token(token)).resource == RESOURCE


def test_expired_code_access_refresh_and_owner_session_are_rejected(setup):
    provider, client, options = setup
    account = register(client)
    code = code_for(client, account)
    with provider._db() as db:
        db.execute("UPDATE codes SET expires=0")
    assert exchange(client, account, code).status_code == 400
    tokens = exchange(client, account, code_for(client, account)).json()
    with provider._db() as db:
        db.execute("UPDATE tokens SET expires=0")
    assert run(provider.load_access_token(tokens["access_token"])) is None
    assert refresh(client, account, tokens["refresh_token"]).status_code == 400
    request_id, csrf, page = begin(client, account)
    with provider._db() as db:
        db.execute("UPDATE browsers SET expires=0")
    assert owner_post(client, request_id, csrf, action="approve").status_code == 403


def test_login_form_preserves_browser_origin_without_accepting_opaque_origin(setup):
    _, client, _ = setup
    account = register(client)
    request_id, csrf, page = begin(client, account)
    assert page.headers["referrer-policy"] == "same-origin"
    rejected = client.post("/oauth/owner", data={"request": request_id, "csrf": csrf,
        "action": "login", "password": PASSWORD}, headers={"Origin": "null"})
    assert rejected.status_code == 403
    accepted = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    assert accepted.status_code == 200 and "同意" in accepted.text
    assert accepted.headers["referrer-policy"] == "same-origin"


def test_only_authenticated_consent_allows_its_registered_callback_origin(setup):
    _, client, _ = setup
    account = register(client)
    request_id, csrf, login = begin(client, account)
    assert "form-action 'self';" in login.headers["content-security-policy"]
    accepted = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    policy = accepted.headers["content-security-policy"]
    assert "form-action 'self' https://chatgpt.com;" in policy
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    csrf = re.search(r'name="csrf" value="([^"]+)"', accepted.text)[1]
    redirect = owner_post(client, request_id, csrf, action="approve")
    assert redirect.status_code == 302
    assert redirect.headers["content-security-policy"] == policy
    assert owner_post(client, request_id, csrf, action="approve").status_code == 403


@pytest.mark.parametrize(("value", "expected"), [
    ("https://chatgpt.com/connector_platform_oauth_redirect?state=a;b", "https://chatgpt.com"),
    ("https://example.test:8443/path;script-src%20*?x='", "https://example.test:8443"),
    ("http://127.0.0.1:8123/callback", "http://127.0.0.1:8123"),
    ("http://[::1]:8123/callback", "http://[::1]:8123"),
])
def test_callback_csp_uses_only_normalized_origin(value, expected):
    assert _callback_origin(value) == expected


@pytest.mark.parametrize("value", ["http://evil.example/cb", "https://user:password@example.test/cb",
                                  "https://example.test/cb#fragment", "https://bad'host.test/cb"])
def test_callback_csp_rejects_unsafe_uri(value):
    with pytest.raises(ValueError):
        _callback_origin(value)


def test_transactionally_consumes_code_under_two_independent_connections(setup):
    provider, client, options = setup
    account = register(client)
    raw = code_for(client, account)
    info = run(provider.get_client(account["client_id"]))
    code = run(provider.load_authorization_code(info, raw))
    other_provider = SqliteOAuthProvider(**options)

    def consume(selected):
        try:
            return run(selected.exchange_authorization_code(info, code))
        except TokenError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, [provider, other_provider]))
    assert sum(value is not None for value in results) == 1


def test_concurrent_stale_refresh_object_revokes_the_rotated_family(setup):
    provider, client, options = setup
    account = register(client)
    issued = exchange(client, account, code_for(client, account)).json()
    info = run(provider.get_client(account["client_id"]))
    old = run(provider.load_refresh_token(info, issued["refresh_token"]))
    restored = SqliteOAuthProvider(**options)

    def rotate(selected):
        try:
            return run(selected.exchange_refresh_token(info, old, ["jx:read"]))
        except TokenError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rotate, [provider, restored]))
    assert sum(value is not None for value in results) == 1
    winner = next(value for value in results if value is not None)
    assert run(provider.load_access_token(winner.access_token)) is None


def test_authorization_requires_explicit_s256_and_correct_resource(setup):
    provider, client, options = setup
    account = register(client)
    for changes in ({"code_challenge_method": "plain"}, {"resource": "https://evil.example/mcp"}):
        response = client.get("/authorize", params={
            "client_id": account["client_id"], "redirect_uri": REDIRECT,
            "response_type": "code", "code_challenge": CHALLENGE,
            "code_challenge_method": "S256", "resource": RESOURCE, **changes,
        })
        assert response.status_code == 400
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


@pytest.mark.parametrize("wrong_resource", [
    ISSUER + ".well-known/oauth-authorization-server", ISSUER,
    "https://wrong.example/mcp?token=private-resource-marker",
])
def test_wrong_authorization_target_explains_actual_mcp_url_without_reflecting_request(setup, wrong_resource):
    provider, client, options = setup
    account = register(client)
    response = client.get("/authorize", params={
        "client_id": account["client_id"], "redirect_uri": REDIRECT,
        "response_type": "code", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": wrong_resource,
        "state": "private-state-marker",
    })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"
    assert response.json()["expected_resource"] == RESOURCE
    assert RESOURCE in response.json()["error_description"]
    assert "重新发起授权" in response.json()["error_description"]
    assert "private-state-marker" not in response.text
    assert "private-resource-marker" not in response.text
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    # The strict rejection does not change the valid /mcp authorization path.
    _, _, page = begin(client, account)
    assert page.status_code == 200


def test_sdk_metadata_and_cors_preflight_match_supported_auth_methods(setup):
    provider, client, options = setup
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == ISSUER
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == list(SCOPES)
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    response = client.options("/token", headers={
        "Origin": "https://inspector.example", "Access-Control-Request-Method": "POST",
    })
    assert response.status_code == 200


def test_sdk_authorization_error_redirect_also_identifies_issuer(setup):
    provider, client, options = setup
    account = register(client)
    response = client.get("/authorize", params={
        "client_id": account["client_id"], "redirect_uri": REDIRECT,
        "response_type": "code", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
        "scope": "jx:admin", "state": "expected-state",
    })
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["invalid_scope"]
    assert query["state"] == ["expected-state"]
    assert query["iss"] == [ISSUER]


def test_owner_denial_never_creates_a_code_and_returns_issuer(setup):
    provider, client, options = setup
    account = register(client)
    request_id, csrf, page = begin(client, account)
    logged_in = owner_post(client, request_id, csrf, action="login", password=PASSWORD)
    csrf = re.search(r'name="csrf" value="([^"]+)"', logged_in.text)[1]
    denied = owner_post(client, request_id, csrf, action="deny")
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["iss"] == [ISSUER]
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
    assert owner_post(client, request_id, csrf, action="approve").status_code == 403


def test_consent_html_escapes_data_and_does_not_render_external_scripts():
    response = SqliteOAuthProvider._page(
        pending_id="safe", csrf="safe", authenticated=True,
        data={"client_id": '<script>alert("bad")</script>',
              "redirect_uri": 'https://example.test/?x="<script>', "scopes": ["jx:read"]},
    )
    assert b"<script>" not in response.body
    assert b"&lt;script&gt;" in response.body


def test_invalid_registration_scope_or_unsafe_redirect_is_rejected(setup):
    provider, client, options = setup
    for changes in ({"scope": "jx:admin"}, {"redirect_uris": ["http://external.example/callback"]}):
        response = client.post("/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "jx:read", **changes,
        })
        assert response.status_code == 400


def test_authentication_request_body_and_rate_limits_are_bounded(setup):
    provider, client, options = setup
    assert client.post("/token", content=b"x" * 16_385).status_code == 413
    responses = [client.post("/register", json={}) for _ in range(6)]
    assert responses[-1].status_code == 429
    assert len(provider._rates) <= 1024


def test_malformed_registration_json_returns_sanitized_error(setup):
    provider, client, options = setup
    response = client.post("/register", content=b'{bad-private-value',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "bad-private-value" not in response.text


def test_no_default_password_no_overwrite_and_https_configuration_required(tmp_path):
    credentials = tmp_path / "owner.json"
    with pytest.raises(ValueError):
        create_owner_credentials(credentials, "short")
    create_owner_credentials(credentials, PASSWORD)
    with pytest.raises(FileExistsError):
        create_owner_credentials(credentials, PASSWORD)
    data = json.loads(credentials.read_text())
    assert data["algorithm"] == "pbkdf2_sha256" and data["rounds"] >= 600_000
    assert PASSWORD not in credentials.read_text()
    for resource in ("http://jx.example.com/mcp", "https://owner:secret@jx.example.com/mcp",
                     "https://jx.example.com/mcp?secret=bad"):
        with pytest.raises(ValueError, match="HTTPS"):
            SqliteOAuthProvider(database_path=tmp_path / "auth.db", credentials_path=credentials,
                                issuer_url=ISSUER, resource_url=resource)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode checks")
def test_private_files_require_mode_0600(tmp_path):
    credentials = tmp_path / "owner.json"
    create_owner_credentials(credentials, PASSWORD)
    credentials.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        SqliteOAuthProvider(database_path=tmp_path / "auth.db", credentials_path=credentials,
                            issuer_url=ISSUER, resource_url=RESOURCE)
