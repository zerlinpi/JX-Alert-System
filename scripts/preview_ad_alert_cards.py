"""Render an offline Feishu card preview using only deliberately fictional evidence."""

import json

from jx_alert.notifications.hourly_ads import advertising_card, alert_item


def preview():
    profile = {'store_id': '999999', 'store_name': '演示店铺（非线上数据）',
               'ad_type': 'sp', 'currency': 'USD', 'report_date': '2026-11-19'}
    intraday = {**profile, 'campaign_id': '101', 'campaign_name': '示例：日内预算观察',
                'intraday': True, 'source': 'lingxing_sp_intraday_observation',
                'observation_captured_at': '2026-11-19T23:45:00+00:00',
                'current': {'spend': 260}, 'verified_daily_budget': 100}
    mature = {**profile, 'campaign_id': '102', 'campaign_name': '示例：成熟归因无订单',
              'mature_date': '2026-11-12', 'attribution_days': 7, 'source': 'lingxing_ads_sp',
              'current': {}, 'mature': {'spend': 120, 'clicks': 50, 'orders': 0}, 'kpis': {},
              'mature_comparison': {'expected_orders_at_reference_rate': 4.5}}
    cases = [(intraday, ['intraday_budget']), (mature, ['zero_conversion'])]
    items = [alert_item({'evidence': evidence, 'signals': signals}, include_dates=True)
             for evidence, signals in cases]
    return advertising_card(items, title='广告待复核 · 离线预览（非线上数据）')


if __name__ == '__main__':
    print(json.dumps(preview(), ensure_ascii=False, indent=2))
