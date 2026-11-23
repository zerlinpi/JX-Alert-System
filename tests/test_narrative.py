"""DeepSeek narrative is used only when every figure, date and store is in the facts."""
import json

from jx_alert.ai import narrative
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.sparkx import analysis
from test_sparkx_analysis import DAILY, PEOPLE, REVIEW


class Client:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def complete_json(self, system, user, **_):
        self.prompts.append((system, user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_figure_check_accepts_exact_and_rounded_values_only():
    allowed = narrative.allowed_figures(["花费 $3,217.70，ACOS 33.9%，环比 +25.7%，统计日 2026-11-22"])
    assert narrative.unsupported_figures("花费 $3,217.70（约 $3,218），ACOS 34%，+25.7%，11-22", allowed) == []
    assert narrative.unsupported_figures("ACOS 35%，花费 3.2 万", allowed) == ["35", "3.2"]
    assert narrative.unsupported_figures("11-23 的数据", allowed) == ["11-23"]


def test_daily_notes_from_deepseek_are_used_after_verification():
    report = analysis.build_daily(DAILY, PEOPLE)
    client = Client({"notes": [
        {"title": "加拿大没有数据", "text": "加拿大共 1 个店铺当日 SparkX 没有返回任何广告数据——先核对 Token 授权范围。"},
        {"title": "美国 ACOS 分化", "text": "Campx 花费 $598，只带回 $928，ACOS 64.5%；FITFIZZ 花费 $184，销售额为 0。"}]})
    notes, source = narrative.daily_notes(None, report, client_factory=lambda settings: client)
    assert source == "deepseek" and notes[1]["title"] == "美国 ACOS 分化"
    system, user = client.prompts[0]
    assert "原样照抄" in system and "json" in system
    packet = json.loads(user)
    assert packet["report_date"] == "2026-11-22" and packet["facts"]


def test_invented_numbers_get_one_correction_then_fall_back_to_rules():
    report = analysis.build_daily(DAILY, PEOPLE)
    bad = {"notes": [{"title": "预计", "text": "下周花费预计达到 $9,999，ACOS 会降到 12%。"}]}
    client = Client(bad, bad)
    notes, source = narrative.daily_notes(None, report, client_factory=lambda settings: client)
    assert source == "rejected" and notes == [{"title": "", "text": text} for text in report["notes"]]
    assert "9,999" in client.prompts[1][1] or "9999" in client.prompts[1][1]


def test_markup_links_and_unavailable_model_fall_back():
    report = analysis.build_daily(DAILY, PEOPLE)
    linked = {"notes": [{"title": "看这里", "text": "详情见 https://evil.example 页面。"}]}
    notes, source = narrative.daily_notes(None, report, client_factory=lambda s: Client(linked, linked))
    assert source == "rejected"
    notes, source = narrative.daily_notes(None, report, client_factory=lambda s: Client(DeepSeekError("x")))
    assert source == "unavailable" and notes[0]["text"] == report["notes"][0]

    def missing(settings):
        raise DeepSeekError("尚未配置DeepSeek凭据。")

    assert narrative.daily_notes(None, report, client_factory=missing)[1] == "unavailable"


def test_review_actions_must_name_known_stores():
    report = analysis.build_review(REVIEW, PEOPLE)
    good = {"headline": "昌泉全店零曝光，44 个 campaign 处于开启状态却没有任何花费，是本周损失最大的问题。",
            "actions": [{"store": "昌泉", "text": "先查账户余额和店铺状态。"},
                        {"store": "全局", "text": "零出单活动先排查搜索词再降出价。"}]}
    text, source = narrative.review_text(None, report, client_factory=lambda s: Client(good))
    assert source == "deepseek" and text["actions"][0]["store"] == "昌泉"
    unknown = {"headline": "昌泉全店零曝光。", "actions": [{"store": "不存在的店", "text": "调整预算。"}]}
    text, source = narrative.review_text(None, report, client_factory=lambda s: Client(unknown, unknown))
    assert source == "rejected" and text["actions"] == report["actions"]
