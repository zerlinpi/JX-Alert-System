"""Store → operator display labels from the verified routing mapping.

Labels are only display text for people already mapped to a store in the owner
console. They never create recipients, mentions or permissions.
"""
import unicodedata

UNNAMED_OPERATOR = "已映射（姓名待同步）"
UNMAPPED_OPERATOR = "未映射运营"


def clean_name(value, limit=40):
    if not isinstance(value, str):
        return None
    clean = "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()
    return clean[:limit] if clean and not clean.startswith("ou_") else None


def store_operator_labels(routing, store_ids, *, unmapped=None):
    """Return {store_id: "张三、李四"}; stores without an owner get ``unmapped`` if given."""
    stores = routing.get("stores") if isinstance(routing, dict) else None
    stores = stores if isinstance(stores, dict) else {}
    result = {}
    for sid in store_ids:
        entry = stores.get(str(sid))
        if isinstance(entry, list):
            entry = {"open_ids": entry}
        owners = entry.get("open_ids") if isinstance(entry, dict) else None
        names = entry.get("owner_display_names") if isinstance(entry, dict) else None
        names = names if isinstance(names, dict) else {}
        labels = [clean_name(names.get(uid)) or UNNAMED_OPERATOR
                  for uid in dict.fromkeys(owners if isinstance(owners, list) else [])
                  if isinstance(uid, str) and uid.startswith("ou_")]
        if labels:
            result[str(sid)] = "、".join(dict.fromkeys(labels))
        elif unmapped is not None:
            result[str(sid)] = unmapped
    return result
