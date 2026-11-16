"""`outbound seq-preview` runs against a fake Close, matches the real baseline rule and writes nothing."""
import asyncio
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import outbound.__main__ as cli
from outbound.db import Db
from outbound.sequences import SETTER_FIELD, SS_BOOKED, TC_COMPLETED

from .fakes import ME, OTHER, FakeClose, make_settings


def test_preview_lists_baseline_and_would_be_texts(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, nurture_view_id="v_n", prep_view_id="v_p")
    db = Db(settings.db_path)
    db.kv_set("seq_started:nurture", "x")
    db.kv_set("seq_view:nurture", "v_n")
    db.kv_set("seq_started:prep", "x")
    db.kv_set("seq_view:prep", "old_view")  # prep just switched views
    close = FakeClose()
    close.views = {"v_n": {"lead_new", "lead_other"}, "v_p": {"lead_call"}}
    close.add_lead("lead_new", name="Pauline Doe", in_view=False)
    close.add_opp("lead_new", TC_COMPLETED, ME)
    other = close.add_lead("lead_other", name="Not Mine", phone="+13125550177", in_view=False)
    other[f"custom.{SETTER_FIELD}"] = OTHER
    close.add_opp("lead_other", TC_COMPLETED, ME)
    call = close.add_lead("lead_call", name="William Roe", phone="+13125550166", in_view=False)
    call[f"custom.{SETTER_FIELD}"] = ME
    close.add_opp("lead_call", SS_BOOKED, OTHER)
    close.add_activity("meeting", "lead_call",
                       starts_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    async def aclose():
        return None
    close.aclose = aclose
    monkeypatch.setattr(cli, "_need_close", lambda s: close)
    asyncio.run(cli._seq_preview(Namespace(which="all", limit=0), settings))
    out = capsys.readouterr().out

    assert "Pauline Doe +13125550100: ENROLL nurture" in out
    assert "Not Mine: SKIP - Setter Lead Owner is someone else" in out
    assert "watching a new smart view, all 1 leads already in this view will be LEFT ALONE" in out
    assert "- William Roe" in out
    assert close.created_sms == [] and close.notes == []
    assert Db(settings.db_path).seq_rows() == []  # nothing saved
