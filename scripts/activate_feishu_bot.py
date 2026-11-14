"""Enable the separate group bot after its private key and Feishu subscription are ready."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from jx_alert.ai.deepseek import DeepSeekClient
from jx_alert.config import get_settings
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.routing import _load_routing, report_group_delivery


def main():
    settings = get_settings()
    # Validate locally without making a paid model request or exposing credentials.
    DeepSeekClient.from_settings(settings)
    FeishuAppClient.from_settings(settings)
    report_group_delivery(settings, _load_routing(settings.feishu_routing_file))
    path = Path(settings.runtime_settings_file)
    original = path.read_bytes() if path.exists() else b"{}"
    runtime = json.loads(original)
    runtime.update(feishu_bot_enabled=True, excel_archive_enabled=True)
    backup = path.with_name(path.name + ".before-bot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    with backup.open("xb") as stream:
        stream.write(original)
    os.chmod(backup, 0o600)
    descriptor, temporary = tempfile.mkstemp(prefix=".bot-settings-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(runtime, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    print("Bot enabled in runtime settings. Start only the bot service after publishing its Feishu event subscription.")


if __name__ == "__main__":
    main()
