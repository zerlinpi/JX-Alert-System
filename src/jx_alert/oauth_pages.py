"""Self-contained owner OAuth screens. Only known CSS is allowed by the CSP."""
from base64 import b64encode
from hashlib import sha256
from html import escape
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse


_STYLE = """
:root {
  color-scheme:light; font-family:system-ui,"Microsoft YaHei","PingFang SC",sans-serif;
  color:#102332; background:#edf4f6; line-height:1.6;
}
*, *::before, *::after { box-sizing:border-box; }
body { margin:0; }
.frame {
  display:grid; grid-template-columns:minmax(0,280px) minmax(0,1fr);
  max-width:860px; min-height:480px; margin:clamp(24px,8vh,100px) auto;
  width:calc(100% - 48px); background:#fff; border:1px solid #d3e1e7;
  border-radius:24px; box-shadow:0 16px 46px #10233212; overflow:hidden;
}
.context {
  padding:32px; background:#102b38; color:#fff; display:flex;
  flex-direction:column; gap:22px;
}
.brand { display:flex; align-items:center; gap:12px; font-size:17px; font-weight:760; }
.brand-mark {
  display:grid; place-items:center; width:37px; height:37px; border-radius:11px;
  background:#9cf1df; color:#102b38; font-size:14px; letter-spacing:.04em;
}
.context p { margin:0; color:#c4d9df; font-size:14px; }
.steps { list-style:none; display:grid; gap:10px; margin:12px 0 0; padding:0; }
.steps li {
  display:flex; align-items:center; gap:10px; padding:7px 10px;
  font-size:14px; color:#c4d9df; border:1px solid #34525e; border-radius:10px;
}
.steps [aria-current="step"] { color:#102b38; background:#e1fbf4; border-color:#e1fbf4; font-weight:700; }
.step-number { flex:none; font-variant-numeric:tabular-nums; font-size:12px; font-weight:700; }
.screen { padding:clamp(24px,5vw,42px); min-width:0; }
h1 { margin:0 0 8px; font-size:clamp(24px,5vw,36px); line-height:1.23; letter-spacing:-.025em; }
.intro { margin:0 0 23px; color:#425968; }
.field { display:grid; gap:7px; margin-bottom:18px; }
.field label, .scopes h2 { font-weight:720; font-size:15px; }
input[type="password"] {
  min-width:0; min-height:46px; width:100%; border:1px solid #8196a1; border-radius:10px;
  padding:10px 12px; background:#fff; color:#102332; font:inherit;
}
input[type="password"][aria-invalid="true"] { border-color:#a82a21; }
.error-message {
  background:#fff1ee; border:1px solid #efc4bc; border-radius:10px; color:#84291f;
  padding:9px 12px; margin:0 0 17px; font-size:14px;
}
.field p, .next { font-size:13px; color:#4d6170; margin:0; }
.actions { display:flex; flex-wrap:wrap; align-items:center; gap:9px; }
button {
  min-height:44px; font:inherit; font-weight:700; border:1px solid #087167;
  color:#fff; background:#07635e; border-radius:10px; padding:9px 19px; cursor:pointer;
}
button:hover { background:#054e4b; }
button.secondary { color:#173643; border-color:#aabdc7; background:#fff; }
button.secondary:hover { background:#f0f6f8; }
:focus-visible { outline:3px solid #1a69a0; outline-offset:3px; }
.destination, .scopes, .metadata { border-top:1px solid #dce7eb; padding:14px 0; }
.destination { margin:0; }
.destination dt { font-size:13px; color:#425968; }
.destination dd { margin:2px 0 0; font-weight:720; overflow-wrap:anywhere; }
.scopes h2 { margin:0 0 8px; }
.scopes ul { margin:0; padding:0; list-style:none; display:grid; gap:8px; }
.scopes li {
  display:grid; gap:2px; background:#f1f7f8; border:1px solid #dae8ed;
  border-radius:10px; padding:9px 12px; overflow-wrap:anywhere;
}
.scopes strong { font-size:14px; }
.scopes span, .metadata { font-size:13px; color:#425968; }
summary { cursor:pointer; font-weight:700; }
.metadata dl { margin:9px 0 0; display:grid; gap:4px; }
.metadata dd { margin:0 0 5px; overflow-wrap:anywhere; }
.next { margin-top:10px; }
@media (max-width:640px) {
  .frame { grid-template-columns:minmax(0,1fr); margin:12px auto; width:calc(100% - 24px); border-radius:16px; }
  .context { padding:18px 22px; gap:8px; }
  .steps { grid-template-columns:1fr 1fr; margin-top:7px; }
  .screen { padding:22px 20px 26px; }
  .actions button { flex:1; }
}
"""
_STYLE_HASH = b64encode(sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")

_PERMISSIONS = {
    "jx:read": ("读取已采集的数据", "查看店铺经营、广告和绩效数据。"),
    "jx:collect": ("请求采集与分析任务", "可提交数据采集与分析请求。"),
    "jx:notify": ("发送飞书通知", "将有数据依据的异常或你指定的中文报告发送至已配置飞书群。"),
}


def _document(title: str, content: str, *, headers: dict[str, str], status=200, step=None) -> HTMLResponse:
    response_headers = {
        **headers,
        "Content-Security-Policy": (
            f"{headers['Content-Security-Policy']}; style-src 'sha256-{_STYLE_HASH}'"
        ),
    }
    steps = ""
    if step is not None:
        entries = []
        for key, number, label in (("login", "01", "验证身份"), ("consent", "02", "确认权限")):
            active = ' aria-current="step"' if key == step else ""
            entries.append(f'<li{active}><span class="step-number" aria-hidden="true">{number}</span>{label}</li>')
        steps = '<ol class="steps" aria-label="授权进度">' + "".join(entries) + "</ol>"
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · JX Alert</title>
  <style>{_STYLE}</style>
</head>
<body>
<div class="frame">
  <header class="context">
    <div class="brand"><span class="brand-mark" aria-hidden="true">JX</span>JX Alert</div>
    <p>店铺监控 · 应用连接</p>
    {steps}
  </header>
  <main class="screen"><h1>{escape(title)}</h1>{content}</main>
</div>
</body>
</html>"""
    return HTMLResponse(page, status_code=status, headers=response_headers)


def owner_screen(*, pending_id, csrf, authenticated, data, headers, error=False) -> HTMLResponse:
    hidden = (
        f'<input type="hidden" name="request" value="{escape(str(pending_id), quote=True)}">'
        f'<input type="hidden" name="csrf" value="{escape(str(csrf), quote=True)}">'
    )
    if authenticated:
        title = "确认应用权限"
        step = "consent"
        intro = "核对授权返回地址和下列权限，再决定是否连接。"
        callback = str(data["redirect_uri"])
        destination = urlsplit(callback).netloc
        permissions = "".join(
            f"<li><strong>{label}</strong><span>{detail}</span></li>"
            for label, detail in (_PERMISSIONS[scope] for scope in data["scopes"])
        )
        controls = f"""
<dl class="destination"><dt>授权后返回的域名</dt><dd>{escape(destination)}</dd></dl>
<section class="scopes" aria-labelledby="permission-list">
  <h2 id="permission-list">应用请求的权限</h2><ul>{permissions}</ul>
</section>
<details class="metadata">
  <summary>查看连接详情</summary>
  <dl><dt>客户端 ID</dt><dd>{escape(str(data["client_id"]))}</dd>
  <dt>完整返回地址</dt><dd>{escape(callback)}</dd></dl>
</details>
<div class="actions">
  <button type="submit" name="action" value="approve">同意并连接</button>
  <button type="submit" class="secondary" name="action" value="deny">拒绝</button>
</div>"""
    else:
        title = "验证所有者身份"
        step = "login"
        intro = "使用 JX Alert 所有者口令登录，再确认应用请求的权限。"
        alert = '<p id="login-error" class="error-message" role="alert">登录未成功，请核对口令后重试。</p>' if error else ""
        described_by = "login-error password-help" if error else "password-help"
        invalid = ' aria-invalid="true"' if error else ""
        controls = f"""
{alert}
<div class="field">
  <label for="owner-password">所有者口令</label>
  <input id="owner-password" type="password" name="password" autocomplete="current-password"
         required maxlength="1024" aria-describedby="{described_by}"{invalid}>
  <p id="password-help">请输入在 JX Alert 服务器上设置的口令。</p>
</div>
<div class="actions"><button type="submit" name="action" value="login">继续</button></div>"""
    content = f'<p class="intro">{intro}</p><form method="post" action="/oauth/owner">{hidden}{controls}</form>'
    return _document(title, content, headers=headers, step=step)


def expired_screen(headers: dict[str, str]) -> HTMLResponse:
    return _document(
        "授权请求无效或已过期",
        '<p class="intro">请返回发起连接的应用，在连接设置中重新发起授权。</p>',
        headers=headers, status=403,
    )
