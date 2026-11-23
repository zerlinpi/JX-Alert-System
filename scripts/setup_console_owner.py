"""Create the private owner password hash for the console; never prints passwords."""
import argparse
import getpass
from pathlib import Path

from jx_alert.console_auth import create_owner_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", default="/app/data/secrets/mcp-owner.json")
    args = parser.parse_args()
    path = Path(args.credentials_file)
    if path.exists():
        parser.error("Credentials already exist; this command never overwrites them")
    password = getpass.getpass("New owner password (at least 16 characters): ")
    if password != getpass.getpass("Confirm password: "):
        parser.error("Passwords do not match")
    try:
        create_owner_credentials(path, password)
    except (OSError, ValueError) as exc:
        parser.error(f"Private credential creation failed: {type(exc).__name__}")
    print("Private owner password hash created.")


if __name__ == "__main__":
    main()
