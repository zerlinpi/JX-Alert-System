"""Privately initialize a DeepSeek key; never accept it as a command-line argument."""
import argparse
import getpass
import json
import os
from pathlib import Path

from jx_alert.ai.deepseek import _validated_key
from jx_alert.config import get_settings


def create_credentials(path, key):
    key = _validated_key(key)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"api_key": key}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", default=get_settings().deepseek_credentials_file)
    args = parser.parse_args()
    if Path(args.credentials_file).exists():
        parser.error("Private credentials already exist; no file was overwritten")
    key = getpass.getpass("DeepSeek API key (hidden): ")
    if key != getpass.getpass("Confirm API key (hidden): "):
        parser.error("Keys do not match")
    try:
        create_credentials(args.credentials_file, key)
    except Exception as exc:
        parser.error("Private initialization failed: " + type(exc).__name__)
    print("DeepSeek private credentials saved. No key was printed. Bot activation is a separate step.")


if __name__ == "__main__":
    main()
