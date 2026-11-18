"""Ad judgement requests resolve evidence scope without exposing other shops."""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.bot_intents import ad_review_request, ad_review_stores
from jx_alert.config import Settings
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


REQUEST = ('有哪些店铺的广告情况存在异常情况，只发送当前能看见的数据的店铺的异常广告记录，'
           '数据不全，或者缺少成熟归因数据的内容不显示')


@pytest.mark.parametrize('question', [
    REQUEST, 'Geteen美国广告有什么异常', 'Get 美国 SP 广告花费正常吗',
    'Geteen和Campx广告风险排查', '美国店铺的广告为什么CPC升高',
    '检查广告', '广告情况好不好', '所有店铺广告花费有无异常',
])
def test_ad_judgement_reaches_analyst_without_a_required_store_template(question):
    assert ad_review_request(question)


@pytest.mark.parametrize('question', [
    '现在有哪些店铺数据', '有哪些店铺在投SP', '店铺列表', '美国店铺广告花费排名',
    '分析美国店铺广告花费排名', 'Geteen美国CPC是多少', 'Geteen美国有哪些在投SP广告活动',
    'CPC是什么', '怎么判断广告是否异常', '解释广告ROAS的含义',
    '不要分析广告，只看销量',
    'Geteen美国是否违规',
])
def test_inventory_metrics_and_concepts_keep_their_existing_readers(question):
    assert not ad_review_request(question)


@pytest.fixture
def warehouse(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, store_controls_file=str(tmp_path / 'switches.json'))
    identities = [('5174', '梵界-Geteen-主账号-US', '美国', 1),
                  ('5175', '盟野-Campx-主账号-CA', '加拿大', 1),
                  ('5176', '停用的店铺-US', '美国', 1),
                  ('5177', '授权失败店铺-US', '美国', 2)]
    snapshots = [{'store_id': sid, 'store_name': name, 'country': country}
                 for sid, name, country, _ in identities]
    with Session(engine) as db:
        db.add_all([Store(sid=sid, name=name, country=country, status=status)
                    for sid, name, country, status in identities])
        db.add(Store(sid='9999', name='NotVisible-US', country='美国', status=1))
        db.commit()
        from jx_alert.store_controls import get_store_controls, save_store_controls
        save_store_controls(settings, '5176', False, False,
                            get_store_controls(settings, '5176')['revision'])
        yield db, settings, snapshots
    engine.dispose()


def selected(warehouse, question=REQUEST, ids=()):
    db, settings, snapshots = warehouse
    stores = ad_review_stores(db, settings, question, snapshots, ids)
    return None if stores is None else {s.sid for s in stores}


def test_exact_operator_request_uses_operating_authorized_visible_stores(warehouse):
    assert selected(warehouse) == {'5174', '5175'}
    assert selected(warehouse, '麻烦帮我看看当前有哪些店铺的广告异常') == {'5174', '5175'}
    assert selected(warehouse, '美国店铺广告异常') == {'5174'}


def test_single_matched_fuzzy_brand_keeps_priority_over_a_broad_word(warehouse):
    assert selected(warehouse, 'Get 美国所有广告有没有异常', ['5174']) == {'5174'}
    assert selected(warehouse, '检查店铺梵界的广告异常', ['5174']) == {'5174'}


def test_multiple_known_brand_fragments_or_identifiers_are_not_a_global_query(warehouse):
    assert selected(warehouse, 'Get和Camp广告风险排查') == {'5174', '5175'}
    assert selected(warehouse, '5174和5175的广告异常') == {'5174', '5175'}


@pytest.mark.parametrize('question', [
    'MissingBrand广告异常', '9999广告异常', '不存在品牌的广告异常',
    '检查不存在品牌店铺广告异常', 'Get和Unknown广告异常',
    '店铺不存在品牌的广告异常', '检查店铺不存在品牌的广告风险',
    '站点不存在品牌广告异常',
])
def test_unresolved_concrete_brand_or_id_cannot_expand_to_all_shops(warehouse, question):
    assert selected(warehouse, question) is None


def test_named_paused_shop_remains_historically_readable_but_not_an_implicit_target(warehouse):
    assert selected(warehouse, '停用的店铺广告异常', ['5176']) == {'5176'}
    assert selected(warehouse, '包含已关闭的所有店铺广告异常') == {'5174', '5175', '5176'}


def test_no_authorized_shops_is_an_empty_scope_not_a_resolution_failure(warehouse):
    assert selected(warehouse, '5177广告异常', ['5177']) == set()
    assert selected(warehouse, '9999广告异常', ['9999']) == set()


def test_dates_and_amounts_are_not_treated_as_identifiers(warehouse):
    assert selected(warehouse, '2026-11-16美国店铺广告花费超过1000是否异常') == {'5174'}


def test_non_review_does_not_change_switches_or_invoke_a_new_reader(warehouse):
    _, settings, _ = warehouse
    before = json.loads(Path(settings.store_controls_file).read_text())
    assert selected(warehouse, '美国店铺广告花费排名') is None
    assert json.loads(Path(settings.store_controls_file).read_text()) == before
