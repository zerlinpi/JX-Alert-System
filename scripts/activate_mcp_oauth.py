"""Enable OAuth after the owner has privately initialized their password."""
import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone

from jx_alert.config import get_settings
from jx_alert.mcp_oauth import SqliteOAuthProvider


def main():
    settings = get_settings()
    if not settings.mcp_public_url.startswith("https://"):
        raise SystemExit("Configure the verified HTTPS public URL first")
    if "*" in settings.mcp_forwarded_allow_ips:
        raise SystemExit("Only the verified proxy IP may be trusted")
    # Validate the existing private credential file and initialize the private
    # state store using normal ownership/mode checks. Never expose their content.
    SqliteOAuthProvider(database_path=settings.mcp_oauth_database_path,
        credentials_path=settings.mcp_owner_credentials_file,
        issuer_url=settings.mcp_public_url, resource_url=settings.mcp_public_url.rstrip("/") + settings.mcp_path)
    path = Path(settings.runtime_settings_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Runtime configuration must be an object")
    if data.get("mcp_auth_mode") == "oauth":
        print("OAuth already configured; restart the MCP process if necessary.")
        return
    backup = path.with_name(path.name + ".before-oauth-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    shutil.copy2(path, backup)
    data["mcp_auth_mode"] = "oauth"
    temporary = path.with_name(path.name + ".oauth-tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    print("OAuth configured with a runtime backup. Restart only the MCP service, then verify metadata and anonymous 401.")


if __name__ == "__main__":
    main()
