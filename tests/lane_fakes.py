"""What tasks.run_lane returns for each lane, so lane tests need neither SparkX nor DeepSeek."""
import copy
from datetime import datetime, timezone

from jx_alert.reports import tasks

SEND = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)  # 08:30 Beijing, 16:30 Nov 23 in Los Angeles
HOUR = datetime(2026, 11, 24, 6, 5, tzinfo=timezone.utc)   # 14:05 Beijing, 22:05 Nov 23 in Los Angeles

ROSTER = {
    "101": {"name": "香港川页", "country": "US", "store_type": "seller"},
    "102": {"name": "Campx-主账号", "country": "US", "store_type": "seller"},
    "103": {"name": "FITFIZZ-主账号", "country": "US", "store_type": "seller"},
    "104": {"name": "陈子俊", "country": "US", "store_type": "seller"},
    "105": {"name": "昌泉", "country": "US", "store_type": "seller"},
    "106": {"name": "OCTORA", "country": "US", "store_type": "seller"},
    "201": {"name": "川页-CA", "country": "CA", "store_type": "seller"},
}
SUMMARY = {"content": "", "sparkx_calls": 4, "skill_reads": 2, "rounds": 5, "tools_used": ["get_ads_perf"],
           "usage": {"total_tokens": 9000}, "model": "deepseek-v4-pro", "thinking": True}

DAILY_TEXT = """**全公司核心指标**
| 指标 | 当日 | 前一日 | 环比 |
|---|---|---|---|
| 总销售额 | $12,345 | $11,000 | +12.2% |
| 广告销售额 | $10,900 | $9,800 | +11.2% |
| 总订单 | — | 410 | — |
| 广告订单 | 250 | 236 | +5.9% |
| 总销量 | 525 | 479 | +9.6% |
| 广告销量 | 289 | 295 | -2.0% |
| 广告花费 | $4,034 | $3,900 | +3.4% |
| ACOS | 32.7% | 30.1% | +2.6pp |

**有花费店铺明细**
| 店铺 | 运营 | 广告花费 | ACOS | TACOS | CPC | ROAS | CVR | 广告销售额 | 总销售额 | 广告订单 | 总订单 | 广告销量 | 总销量 | 花费日环比 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 香港川页 | 未映射运营 | $3,217.70 | 35.8% | 29.3% | $1.52 | 2.80 | 11.4% | $9,000 | $11,000 | 240 | — | 280 | 500 | +3.1% |
| Campx | 未映射运营 | $420.00 | 22.1% | 16.2% | $1.75 | 4.52 | 4.2% | $1,900 | $2,600 | 10 | — | 9 | 25 | -2.0% |

**须知**
1. 花费集中在香港川页，占全公司 80%。
2. 昌泉有广告花费没有广告订单，先看搜索词。
3. Campx ACOS 22.1%、ROAS 4.52，可加预算放量。"""

REVIEW_TEXT = """**2 个店铺检出 3 个异常 campaign，合计浪费约 $520。**
近 7 日广告花费 $2,500（+12%）、广告销售额 $5,600（-8%）、ACOS 44.6%（+8.1pp）。

**一、最严重**
香港川页的 SP-Auto 花了 $310 一单没出。

**二、异常店铺总览**
| 店铺 | 运营 | 近7日花费 | ACOS 近7日 | 前7日 | 异常数 | 主要问题 |
|---|---|---|---|---|---|---|
| 香港川页 | 未映射运营 | $1,900 | 48% | 30% | 2 | 高花费零出单 |
| Campx | 未映射运营 | $600 | 41% | 25% | 1 | ACOS 恶化 |

**四、建议动作**
1. 香港川页：SP-Auto 出价 2.45 → 1.70，先否定无关搜索词。"""


def finding(pid, cid, category, spend, clicks, orders, acos=None, cvr=0.0, name=None):
    return {"pid": pid, "campaign_id": cid, "category": category, "campaign": name or f"Campaign {cid}",
            "spend": float(spend), "clicks": float(clicks), "orders": float(orders), "sales": None,
            "acos": acos, "cvr": cvr, "suggestion": "先看今日搜索词"}


HOURLY_FINDINGS = [finding("102", "9001", "high_spend_no_conversion", 41.2, 28, 0),
                   finding("101", "9002", "low_spend_no_conversion", 12.5, 19, 0),
                   finding("101", "9003", "low_spend_high_conversion", 18.0, 30, 3, acos=12.4, cvr=10.0)]


def lane_result(lane, *, content=None, stores=None, findings=None, roster=None):
    roster = copy.deepcopy(roster or ROSTER)
    base = {"summary": dict(SUMMARY), "profiles": sorted(roster, key=int), "roster": roster, "skills": "v1.3.7"}
    if lane == "hourly":
        return {**base, "findings": copy.deepcopy(HOURLY_FINDINGS if findings is None else findings), "dropped": 0,
                "note": "", "day": "2026-11-23", "time": "22:05"}
    return {**base, "content": content or (DAILY_TEXT if lane == "daily" else REVIEW_TEXT),
            "stores": (["101", "102"] if lane == "review" else []) if stores is None else stores,
            "report_date": "2026-11-22", "window": ["2026-11-16", "2026-11-22"]}


def runner_for(results=None, calls=None, error=None):
    """A stand-in for tasks.run_lane; hourly findings already alerted today are dropped like the real check."""
    def run(settings, lane, config, *, now, directory, alerted=frozenset(), on_roster=None):
        if calls is not None:
            calls.append(lane)
        if error is not None:
            raise error
        value = copy.deepcopy((results or {}).get(lane) or lane_result(lane))
        if on_roster:
            on_roster(value["roster"])
        if lane == "hourly":
            value["findings"] = [item for item in value["findings"] if tasks.finding_key(item) not in alerted]
        return value
    return run
