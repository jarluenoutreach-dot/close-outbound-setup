"""Webinar no-reply nudge: first text -> 24h -> one nudge, only if the lead never replied to anyone."""
import asyncio
import random
from datetime import datetime, timezone

from outbound.db import Db
from outbound.engine import Engine
from outbound.nudge import START_KEY, NudgeRunner, render_nudge

from .fakes import ME, OTHER, Clock, FakeClose, FakeTelegram, make_settings

UTC = timezone.utc
DAY = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # Monday 10am Central


async def _nosleep(_):
    return None


def setup(tmp_path, start=DAY, **overrides):
    clock = Clock(start)
    close, tg = FakeClose(clock=clock), FakeTelegram()
    engine = Engine(make_settings(tmp_path, min_seconds_between_sends=0, **overrides), Db(), close, tg,
                    clock=clock, sleep=_nosleep, rng=random.Random(1))
    nudge = NudgeRunner(engine)
    engine.db.kv_set(START_KEY, "2026-01-01T00:00:00+00:00")  # feature already live
    return engine, nudge, close, clock


def run(coro):
    return asyncio.run(coro)


def first_text(engine, close, lead_id="lead_a", **kw):
    close.add_lead(lead_id, **kw)

    async def go():
        await engine.on_lead_event(lead_id)
        await engine.sender_tick()
    run(go())
    assert engine.db.get(lead_id)["status"] == "sent"
    for s in close.created_sms:
        s["status"] = "sent"


def nudges(close, lead_id="lead_a"):
    return [s["text"] for s in close.created_sms if s["lead_id"] == lead_id and s["text"].startswith("Hey")
            and "following up" in s["text"]]


def test_no_reply_gets_one_nudge_after_24h(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close)
    clock.advance(hours=23)
    run(nudge.tick())
    assert nudges(close) == []
    clock.advance(hours=1)
    assert run(nudge.tick())
    assert nudges(close) == [render_nudge("Sam")]
    sms = close.created_sms[-1]
    assert sms["local_phone"] == "+19786669462" and sms["remote_phone"] == "+13125550100"
    clock.advance(days=2)
    run(nudge.tick())
    assert len(nudges(close)) == 1  # only ever one
    assert engine.db.get("lead_a")["nudge_status"] == "sent"


def test_reply_to_us_means_no_nudge(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close)
    clock.advance(hours=2)
    close.add_activity("sms", "lead_a", direction="inbound", status="inbox", date_created=clock.now.isoformat())
    clock.advance(hours=22)
    run(nudge.tick())
    assert nudges(close) == []
    assert engine.db.get("lead_a")["nudge_reason"] == "lead has replied by text before"


def test_lead_who_replied_to_another_setter_long_ago_is_never_nudged(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close)
    # A reply to a teammate's number 6 months ago (outside the first text's 60-day rule).
    close.add_activity("sms", "lead_a", direction="inbound", status="inbox", user_id=OTHER,
                       local_phone="+15550001111", date_created="2026-05-01T12:00:00+00:00")
    clock.advance(hours=24)
    run(nudge.tick())
    assert nudges(close) == []


def test_inbound_call_or_conversation_means_no_nudge(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close, "lead_in", phone="+13125550101")
    first_text(engine, close, "lead_talk", phone="+13125550102")
    close.add_activity("call", "lead_in", direction="inbound", duration=5, user_id=OTHER,
                       date_created="2026-03-01T12:00:00+00:00")
    close.add_activity("call", "lead_talk", direction="outbound", duration=300, user_id=OTHER,
                       date_created="2026-03-01T12:00:00+00:00")
    clock.advance(hours=24)
    run(nudge.tick())
    run(nudge.tick())
    assert nudges(close, "lead_in") == [] and nudges(close, "lead_talk") == []
    assert engine.db.get("lead_in")["nudge_reason"] == "lead has called in before"


def test_short_unanswered_call_by_me_still_nudges(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close)
    close.add_activity("call", "lead_a", direction="outbound", duration=0, user_id=ME,
                       date_created=clock.now.isoformat())
    clock.advance(hours=24)
    run(nudge.tick())
    assert len(nudges(close)) == 1


def test_waits_for_8am_in_the_leads_time_zone(tmp_path):
    engine, nudge, close, clock = setup(tmp_path, start=datetime(2026, 11, 17, 1, 30, tzinfo=UTC))  # 7:30pm CT
    first_text(engine, close)
    clock.advance(hours=24)  # 7:30pm next day: still inside 8am-8pm
    run(nudge.tick())
    assert len(nudges(close)) == 1

    engine2, nudge2, close2, clock2 = setup(tmp_path, start=datetime(2026, 11, 17, 1, 50, tzinfo=UTC))
    first_text(engine2, close2)
    clock2.advance(hours=24, minutes=15)  # 8:05pm Central: window closed
    run(nudge2.tick())
    assert nudges(close2) == []
    clock2.advance(hours=12)  # 8:05am Central
    run(nudge2.tick())
    assert len(nudges(close2)) == 1


def test_old_first_texts_are_not_nudged_when_feature_turns_on(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    engine.db.conn.execute("DELETE FROM kv")
    first_text(engine, close)  # sent before the nudge feature started
    clock.advance(hours=1)
    run(nudge.tick())  # first tick records the start time
    clock.advance(days=2)
    run(nudge.tick())
    assert nudges(close) == []


def test_other_setter_text_status_meeting_and_stop_file(tmp_path):
    engine, nudge, close, clock = setup(tmp_path)
    first_text(engine, close, "lead_s", phone="+13125550103")
    first_text(engine, close, "lead_dnc", phone="+13125550104")
    first_text(engine, close, "lead_m", phone="+13125550105")
    clock.advance(hours=3)
    close.add_activity("sms", "lead_s", direction="outbound", status="sent", user_id=OTHER,
                       local_phone="+15550002222", date_created=clock.now.isoformat())
    close.leads["lead_dnc"]["status_label"] = "DNC"
    close.add_activity("meeting", "lead_m", date_created=clock.now.isoformat(), starts_at=clock.now.isoformat())
    clock.advance(hours=21)
    engine.s.stop_file.write_text("stop")
    run(nudge.tick())
    assert len(close.created_sms) == 3
    engine.s.stop_file.unlink()
    for _ in range(3):
        run(nudge.tick())
    assert len(close.created_sms) == 3
    reasons = {i: engine.db.get(i)["nudge_reason"] for i in ("lead_s", "lead_dnc", "lead_m")}
    assert reasons == {"lead_s": "another setter texted the lead after the first text",
                       "lead_dnc": "lead status is DNC", "lead_m": "lead has a meeting"}


def test_disabled(tmp_path):
    engine, nudge, close, clock = setup(tmp_path, nudge_enabled=False)
    first_text(engine, close)
    clock.advance(hours=25)
    run(nudge.tick())
    assert nudges(close) == []
