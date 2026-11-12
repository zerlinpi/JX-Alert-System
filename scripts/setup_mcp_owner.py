"""Create a private owner password hash for optional MCP OAuth; never print passwords."""
import argparse
import getpass
from pathlib import Path

from jx_alert.mcp_oauth import create_owner_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", default="/app/data/secrets/mcp-owner.json")
    args = parser.parse_args()
    path = Path(args.credentials_file)
    if path.exists():
        parser.error("Credentials already exist; this command never overwrites them")
    password = getpass.getpass("New MCP owner password (at least 16 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        parser.error("Passwords do not match")
    try:
        create_owner_credentials(path, password)
    except (OSError, ValueError) as exc:
        parser.error(f"Private credential creation failed: {type(exc).__name__}")
    print("Private owner password hash created. OAuth remains disabled until configured.")


if __name__ == "__main__":
    main()
