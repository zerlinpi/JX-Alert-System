"""Register all authorized stores for analysis without guessing report timezones.

Run inside the existing app container. The scheduler should be stopped during
the short atomic configuration switch; no advertisements or secrets are changed.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from jx_alert.config import get_settings
from jx_alert.lingxing.client import LingxingClient
from jx_alert.operations import sync_scoped_store_inventory
from jx_alert.readiness import analysis_plan
from jx_alert.storage.db import SessionLocal


def atomic_json(path, value):
    path = Path(path)
    mode = path.stat().st_mode & 0o777
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validated_store_ids(response):
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("A complete nonempty authorized seller list is required")
    ids = []
    for row in rows:
        sid = row.get("sid") if isinstance(row, dict) else None
        if type(sid) not in (str, int) or not str(sid).isdigit() or int(sid) <= 0:
            raise ValueError("Seller list contains an invalid store ID")
        ids.append(str(int(sid)))
    if len(set(ids)) != len(ids):
        raise ValueError("Seller list contains duplicate store IDs")
    return sorted(ids, key=int)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-conservative-history", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    result = LingxingClient.from_settings().get_stores()
    ids = validated_store_ids(result)
    summary = {"authorized_stores": len(ids), "status_counts": dict(Counter(
        str(row.get("status")) for row in result["data"])), "applied": False}
    if not args.apply:
        print(json.dumps(summary))
        return
    if not args.allow_conservative_history:
        parser.error("Explicit --allow-conservative-history is required; missing timezones are never guessed")
    paths = [Path(settings.collection_scope_file), Path(settings.store_timezones_file),
             Path(settings.runtime_settings_file)]
    originals = {path: json.loads(path.read_text(encoding="utf-8")) for path in paths}
    if any(not isinstance(value, dict) for value in originals.values()):
        raise SystemExit("Configuration roots must be objects")
    scoped = {**originals[paths[0]], "store_ids": ids}
    zones = {**originals[paths[1]], "stores": {**originals[paths[1]].get("stores", {})}}
    for sid in ids:
        zones["stores"].setdefault(sid, {"timezone": None, "currency_code": ""})
    runtime = {**originals[paths[2]], "allow_unconfigured_timezone_history": True,
               "feishu_daily_report_group_digest": True}
    if (not settings.lingxing_performance_observation_only or settings.lingxing_performance_source_timezone):
        raise SystemExit("Preserve the approved observation-only health mode and unconfigured source timezone")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in paths:
        shutil.copy2(path, path.with_name(path.name + ".before-all-stores-" + stamp))
    updated = settings.model_copy(update={"allow_unconfigured_timezone_history": True,
                                          "feishu_daily_report_group_digest": True})
    try:
        for path, value in zip(paths, [scoped, zones, runtime], strict=True):
            atomic_json(path, value)
        with SessionLocal() as db:
            sync_scoped_store_inventory(db, updated, result)
            db.commit()
            plan = analysis_plan(db, updated)
        summary.update(applied=True, enabled_store_count=plan["enabled_store_count"],
            collection_ready_count=plan["collection_ready_count"], analysis_ready_count=plan["analysis_ready_count"],
            timezone_confirmed_count=sum(x["timezone_confirmed"] for x in plan["stores"]),
            state_counts=dict(Counter(x["state"] for x in plan["stores"])), backup_suffix=stamp)
    except Exception:
        for path, value in originals.items():
            atomic_json(path, value)
        raise
    proof = Path(settings.runtime_settings_file).parent / "verification" / ("all-store-activation-" + stamp + ".json")
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({"summary": summary, "plan": plan}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
