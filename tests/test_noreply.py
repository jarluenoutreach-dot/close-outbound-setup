"""Webinar Day 3 / Day 7 follow-ups after the nudge, only for leads who never answered anyone."""
import asyncio
import random
from datetime import datetime, timezone

from outbound import noreply as noreply_mod
from outbound.db import Db
from outbound.engine import Engine
from outbound.noreply import NoReplyRunner, render_step
from outbound.nudge import START_KEY as NUDGE_START, NudgeRunner

from .fakes import OTHER, Clock, FakeClose, FakeTelegram, make_settings

UTC = timezone.utc
MON_10AM_CT = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)


async def _nosleep(_):
    return None


def setup(tmp_path, start=MON_10AM_CT, **overrides):
    clock = Clock(start)
    close, tg = FakeClose(clock=clock), FakeTelegram()
    engine = Engine(make_settings(tmp_path, min_seconds_between_sends=0, **overrides), Db(), close, tg,
                    clock=clock, sleep=_nosleep, rng=random.Random(1))
    engine.db.kv_set(NUDGE_START, "2026-01-01T00:00:00+00:00")
    engine.db.kv_set(noreply_mod.START_KEY, "2026-01-01T00:00:00+00:00")
    return engine, NudgeRunner(engine), NoReplyRunner(engine), close, clock


def run(coro):
    return asyncio.run(coro)


def first_text(engine, close, lead_id="lead_a", **kw):
    close.add_lead(lead_id, **kw)

    async def go():
        await engine.on_lead_event(lead_id)
        await engine.sender_tick()
    run(go())


def settle(close):
    for s in close.created_sms:
        s["status"] = "sent"


def texts(close, lead_id="lead_a"):
    return [s["text"] for s in close.created_sms if s["lead_id"] == lead_id]


def walk(nudge, noreply, close, clock, hours):
    sent_at = []
    for h in range(hours):
        settle(close)
        before = len(close.created_sms)
        run(nudge.tick())
        settle(close)
        run(noreply.tick())
        if len(close.created_sms) > before:
            sent_at.append(clock.now)
        clock.advance(hours=1)
    settle(close)
    return sent_at


def test_full_no_reply_path(tmp_path):
    engine, nudge, noreply, close, clock = setup(tmp_path)
    first_text(engine, close)
    sends = walk(nudge, noreply, close, clock, 8 * 24)
    assert [round((t - MON_10AM_CT).total_seconds() / 86400, 2) for t in sends] == [1.0, 3.0, 7.0]
    assert texts(close)[2:] == [render_step(0, "Sam"), render_step(1, "Sam")]
    assert engine.db.get("lead_a")["noreply_status"] == "done"
    assert ("lead_a", "Webinar Day 3 follow-up sent (no reply yet)") in close.notes


def test_reply_after_nudge_stops_follow_ups(tmp_path):
    engine, nudge, noreply, close, clock = setup(tmp_path)
    first_text(engine, close)
    walk(nudge, noreply, close, clock, 30)  # nudge sent
    close.add_activity("sms", "lead_a", direction="inbound", status="inbox", date_created=clock.now.isoformat())
    walk(nudge, noreply, close, clock, 8 * 24)
    assert len(texts(close)) == 2
    assert engine.db.get("lead_a")["noreply_reason"] == "lead has replied by text before"


def test_no_follow_ups_when_nudge_was_skipped(tmp_path):
    engine, nudge, noreply, close, clock = setup(tmp_path)
    first_text(engine, close)
    close.add_activity("sms", "lead_a", direction="inbound", status="inbox", user_id=OTHER,
                       local_phone="+15550001111", date_created="2026-05-01T12:00:00+00:00")
    walk(nudge, noreply, close, clock, 8 * 24)
    assert len(texts(close)) == 1


def test_sunday_and_evening_wait(tmp_path):
    # First text Thursday 6:30pm Central -> Day 3 lands Sunday 6:30pm: waits until Monday 9am.
    start = datetime(2026, 11, 20, 0, 30, tzinfo=UTC)
    engine, nudge, noreply, close, clock = setup(tmp_path, start=start)
    first_text(engine, close)
    sends = walk(nudge, noreply, close, clock, 5 * 24)
    day3 = sends[1].astimezone(noreply_mod.zones_for_phone("+13125550100")[0])
    assert day3.weekday() == 0 and day3.hour == 9


def test_only_first_texts_after_go_live(tmp_path):
    engine, nudge, noreply, close, clock = setup(tmp_path)
    engine.db.conn.execute("DELETE FROM kv WHERE key = ?", (noreply_mod.START_KEY,))
    first_text(engine, close)
    clock.advance(minutes=5)
    run(noreply.tick())  # records the start time, after this lead's first text
    walk(nudge, noreply, close, clock, 8 * 24)
    assert len(texts(close)) == 2  # first text + nudge, no Day 3 / Day 7


def test_disabled(tmp_path):
    engine, nudge, noreply, close, clock = setup(tmp_path, noreply_enabled=False)
    first_text(engine, close)
    walk(nudge, noreply, close, clock, 8 * 24)
    assert len(texts(close)) == 2
