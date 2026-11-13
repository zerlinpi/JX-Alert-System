"""Map all authorized stores to one uniquely verified member of the report group.

Does not activate stores, collect reports or change advertisements. No secrets output.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil

import httpx

from jx_alert.config import get_settings
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.routing import _load_routing


def unique_member(items, name):
    matches = {item.get("member_id") for item in items if item.get("name") == name}
    if len(matches) != 1:
        raise ValueError("Expected exactly one group member matching the requested name")
    identity = next(iter(matches))
    if not isinstance(identity, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", identity):
        raise ValueError("The matching member has no application Open ID")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    settings = get_settings()
    route = _load_routing(settings.feishu_routing_file)
    alias = settings.feishu_daily_report_chat_id
    chat = route.get("groups", {}).get(alias, {}).get("chat_id", "")
    if not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat):
        raise SystemExit("A verified report group is required")
    client = FeishuAppClient.from_settings(settings)
    items, page = [], ""
    with httpx.Client(timeout=settings.feishu_timeout, follow_redirects=False) as http:
        for _ in range(100):
            params = {"member_id_type": "open_id", "page_size": 100}
            if page:
                params["page_token"] = page
            response = http.get(client.base_url + "/open-apis/im/v1/chats/" + chat + "/members",
                params=params, headers={"Authorization": "Bearer " + client._get_token()})
            response.raise_for_status()
            value = response.json()
            if value.get("code") != 0:
                raise SystemExit(f"Group-member lookup denied (code={value.get('code')}); no mappings changed")
            data = value.get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            next_page = data.get("page_token", "")
            if not next_page or next_page == page:
                raise SystemExit("Incomplete group-member pagination; no mappings changed")
            page = next_page
        else:
            raise SystemExit("Group-member pagination exceeded limit; no mappings changed")
    identity = unique_member(items, args.name)
    from jx_alert.lingxing.client import LingxingClient
    result = LingxingClient.from_settings().get_stores()
    sellers = result.get("data") if isinstance(result, dict) else None
    if not isinstance(sellers, list) or not sellers:
        raise SystemExit("Complete authorized-store inventory is required")
    stores = set()
    for seller in sellers:
        sid = seller.get("sid") if isinstance(seller, dict) else None
        if type(sid) not in (str, int) or not str(sid).isdigit() or int(sid) <= 0:
            raise SystemExit("Invalid store inventory; no mappings changed")
        stores.add(str(int(sid)))
    for sid in stores:
        entry = route.setdefault("stores", {}).setdefault(sid, {})
        entry.update(chat_id=alias, open_ids=[identity])
    path = Path(settings.feishu_routing_file)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(path, path.with_name(path.name + ".before-operator-" + stamp))
    temporary = path.with_name(path.name + ".operator-tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(route, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    print(f"Verified one member; mapped {len(stores)} authorized stores. Collection scope unchanged.")


if __name__ == "__main__":
    main()
