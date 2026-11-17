"""Privately initialize the optional read-only official Lingxing business source."""
import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from jx_alert.config import get_settings


def create_credentials(path, url, key):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not isinstance(key, str) or len(key) < 16
            or any(c.isspace() for c in key)):
        raise ValueError("Invalid private MCP configuration")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"url": url, "key": key}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", default=get_settings().lingxing_business_credentials_file)
    args = parser.parse_args()
    if Path(args.credentials_file).exists():
        parser.error("配置已存在，未覆盖任何文件。")
    url = getpass.getpass("领星官方 MCP Server URL（不回显）：").strip()
    key = getpass.getpass("领星官方 MCP Key（不回显）：").strip()
    if key != getpass.getpass("再次输入 MCP Key：").strip():
        parser.error("两次输入不一致。")
    try:
        create_credentials(args.credentials_file, url, key)
    except Exception as exc:
        parser.error("私有配置未保存：" + type(exc).__name__)
    print("只读经营数据源已保存。下次后台同步或18:30采集时生效；未修改.env。")


if __name__ == "__main__":
    main()
