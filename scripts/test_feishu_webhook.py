"""Dry-run by default. Add --send to send one explicit test message to the configured group."""
import argparse

from jx_alert.config import get_settings
from jx_alert.notifications.feishu import FeishuClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true",
                        help="Actually send a test message to the configured Feishu group.")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.feishu_webhook_url:
        raise SystemExit("FEISHU_WEBHOOK_URL is not configured in the local .env")
    if not args.send:
        print("DRY RUN: webhook URL is configured; no request was sent.")
        print("Run with --send only when you are ready to post a real test message.")
        return
    client = FeishuClient(
        webhook_url=settings.feishu_webhook_url,
        sign_secret=settings.feishu_sign_secret,
        keyword=settings.feishu_keyword,
        timeout=settings.feishu_timeout,
    )
    result = client.send_text(
        "JX Alert System connectivity test. This is an intentional test message.",
        open_ids=settings.feishu_at_ids(),
    )
    print("Feishu webhook accepted the test message:", result.get("StatusMessage", "ok"))


if __name__ == "__main__":
    main()
