"""Read owner-managed, bounded advertising review profiles without executable instructions."""

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


DEFAULT_SKILL = {
    "objective": "balanced",
    "learning_days": 7,
    "min_clicks": 30,
    "min_impressions": 2000,
    "min_spend_usd": 50,
    "min_relative_change": .30,
    "min_expected_orders": 3,
    "intraday_budget_multiple": 2,
    "intraday_overage_usd": 30,
}
_TYPE_KEYS = {"sp", "sb", "sd"}
_OBJECTIVES = {"balanced", "efficiency", "reach", "discovery"}
_BOUNDS = {
    "learning_days": (7, 60, int),
    "min_clicks": (30, 10000, int),
    "min_impressions": (2000, 10000000, int),
    "min_spend_usd": (50, 10000, float),
    "min_relative_change": (.30, .95, float),
    "min_expected_orders": (3, 30, float),
    "intraday_budget_multiple": (2, 10, float),
    "intraday_overage_usd": (30, 10000, float),
}
_CAMPAIGN_KEY = re.compile(r"[0-9]{1,20}:(?:sp|sb|sd):[A-Za-z0-9_-]{1,100}")
_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class AdReviewSkills:
    status: str = "default"
    defaults: dict = field(default_factory=dict)
    ad_types: dict = field(default_factory=dict)
    campaigns: dict = field(default_factory=dict)

    def for_campaign(self, store_id, ad_type, campaign_id):
        layers = (self.defaults, self.ad_types.get(ad_type, {}), self.campaigns.get(
            f"{store_id}:{ad_type}:{campaign_id}", {}))
        resolved = DEFAULT_SKILL.copy()
        for layer in layers:
            for key, value in layer.items():
                resolved[key] = value if key == "objective" else max(resolved[key], value)
        return {**resolved, "status": self.status}


def skill_fingerprint(profile):
    raw = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def frozen_ad_skills_current(settings, checks):
    """Revalidate the relevant effective profiles without rebuilding frozen data."""
    skills = load_ad_review_skills(settings)
    if skills.status == "invalid":
        return False
    if checks is None:
        # Before policy fingerprints, an already-frozen page can only prove
        # compatibility with the built-in rules and only while they are active.
        return skills.status == "default"
    return isinstance(checks, list) and bool(checks) and all(
        isinstance(check, (list, tuple)) and len(check) == 4
        and all(isinstance(value, str) and value for value in check)
        and check[3] == skill_fingerprint(skills.for_campaign(*check[:3]))
        for check in checks
    )


def _profile(value):
    if not isinstance(value, dict) or value.keys() - DEFAULT_SKILL.keys():
        raise ValueError
    for key, item in value.items():
        if key == "objective":
            if not isinstance(item, str) or item not in _OBJECTIVES:
                raise ValueError
            continue
        low, high, kind = _BOUNDS[key]
        allowed_types = (int,) if kind is int else (int, float)
        if type(item) not in allowed_types or not low <= item <= high or not math.isfinite(item):
            raise ValueError
    return value


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def read_ad_review_skills_file(path):
    """Read the bounded operator policy; reject links and non-regular files."""
    if path.is_symlink():
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError
    return data


def parse_ad_review_skills(data):
    """Return the validated policy document and effective layer model."""
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or value.keys() - {"version", "defaults", "ad_types", "campaigns"}):
        raise ValueError
    types, campaigns = value.get("ad_types", {}), value.get("campaigns", {})
    if (not isinstance(types, dict) or types.keys() - _TYPE_KEYS or not isinstance(campaigns, dict)
            or len(campaigns) > 300 or not all(_CAMPAIGN_KEY.fullmatch(k) for k in campaigns)):
        raise ValueError
    return value, AdReviewSkills("configured", _profile(value.get("defaults", {})),
        {key: _profile(row) for key, row in types.items()}, {key: _profile(row) for key, row in campaigns.items()})


def load_ad_review_skills(settings):
    """Read once per analysis pass. Invalid files never widen or broadcast policy."""
    raw_path = getattr(settings, "ad_review_skills_file", None)
    if not raw_path:
        return AdReviewSkills()
    try:
        return parse_ad_review_skills(read_ad_review_skills_file(Path(raw_path)))[1]
    except FileNotFoundError:
        return AdReviewSkills()
    except (OSError, ValueError, RecursionError):
        return AdReviewSkills("invalid")
