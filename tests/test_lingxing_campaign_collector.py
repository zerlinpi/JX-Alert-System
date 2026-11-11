from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import Campaign
from jx_alert.lingxing.campaign_collector import collect_campaigns


class FakeCampaignClient:
    def get_stores(self):
        return {"code": 0, "data": [{
            "sid": 101, "name": "Store A", "status": 1, "has_ads_setting": 1,
        }]}

    def iter_advertising_campaigns(self, campaign_type, payload, **kwargs):
        return iter([{
            "campaign_id": {"sp": 1001, "sb": 2001, "sd": 3001}[campaign_type],
            "profile_id": 98765,
            "name": f"{campaign_type.upper()} Campaign",
            "state": "enabled",
            "serving_status": "running",
            "budget": "50.5" if campaign_type != "sp" else None,
            "daily_budget": "50.5" if campaign_type == "sp" else None,
            "budget_type": "daily" if campaign_type != "sp" else None,
            "start_date": "2026-01-01 00:00:00",
            "end_date": "",
            "lastOverBudgetAt": "2026-11-10 12:00:00",
        }])


def test_campaign_collector_saves_all_types_and_daily_budget():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_campaigns(FakeCampaignClient(), db)
        assert result == {
            "stores_seen": 1,
            "active_ad_stores": 1,
            "campaigns": {"sp": 1, "sb": 1, "sd": 1},
        }
        assert db.scalar(select(func.count()).select_from(Campaign)) == 3
        sp = db.scalar(select(Campaign).where(Campaign.campaign_type == "sp"))
        assert sp.budget == 50.5
        assert sp.budget_type == "daily"
        assert sp.state == "enabled"
        assert sp.last_over_budget_at == "2026-11-10 12:00:00"


def test_campaign_collector_updates_existing_campaigns_without_duplicates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        client = FakeCampaignClient()
        collect_campaigns(client, db)
        collect_campaigns(client, db)
        assert db.scalar(select(func.count()).select_from(Campaign)) == 3



def test_campaign_collector_marks_campaigns_missing_from_complete_list_not_returned():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    class CampaignRemovedClient(FakeCampaignClient):
        def iter_advertising_campaigns(self, campaign_type, payload, **kwargs):
            if campaign_type == "sp":
                return iter([])
            return super().iter_advertising_campaigns(campaign_type, payload, **kwargs)

    with Session() as db:
        collect_campaigns(FakeCampaignClient(), db)
        collect_campaigns(CampaignRemovedClient(), db)
        sp = db.scalar(select(Campaign).where(Campaign.campaign_type == "sp"))
        sb = db.scalar(select(Campaign).where(Campaign.campaign_type == "sb"))
        assert sp.state == "not_returned"
        assert sp.serving_status == "not_returned"
        assert sb.state == "enabled"
