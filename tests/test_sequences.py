"""Nurture and SS prep sequences through the SequenceRunner with a fake Close."""
import asyncio
import random
from datetime import datetime, timedelta, timezone

from outbound.db import Db
from outbound.engine import Engine
from outbound.seq_engine import SequenceRunner
from outbound.sequences import (
    NURTURE,
    NURTURE_STEPS,
    PREP,
    SETTER_FIELD,
    SLIDES_URL,
    SS_BOOKED,
    TC_COMPLETED,
    render,
)

from .fakes import ME, OTHER, Clock, FakeClose, FakeTelegram, make_settings

UTC = timezone.utc
MON_9AM = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # Monday 9:00am Central
SUN_NOON = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)  # Sunday noon Central


async def _nosleep(_):
    return None


def setup(tmp_path, start=MON_9AM, dry_run=False, **overrides):
    settings = dict(nurture_enabled=True, prep_enabled=True, nurture_view_id="v_n", prep_view_id="v_p",
                    min_seconds_between_sends=0)
    settings.update(overrides)
    close, tg, clock = FakeClose(), FakeTelegram(), Clock(start)
    engine = Engine(make_settings(tmp_path, **settings), Db(), close, tg, dry_run=dry_run, clock=clock,
                    sleep=_nosleep, rng=random.Random(1))
    seq = SequenceRunner(engine)
    engine.sequences = seq
    close.views = {"v_n": set(), "v_p": set()}
    # Tests start after go-live: the first-run baseline already happened (on an empty view).
    engine.db.kv_set("seq_started:nurture", "test")
    engine.db.kv_set("seq_started:prep", "test")
    return seq, close, clock


def run(coro):
    return asyncio.run(coro)


def nurture_lead(close, lead_id="lead_n", **kw):
    close.add_lead(lead_id, in_view=False, **kw)
    close.add_opp(lead_id, TC_COMPLETED, ME)
    close.views["v_n"].add(lead_id)
    return close.leads[lead_id]


def prep_lead(close, meeting_in: timedelta, now: datetime, lead_id="lead_p", setter=ME, **kw):
    lead = close.add_lead(lead_id, in_view=False, **kw)
    lead[f"custom.{SETTER_FIELD}"] = setter
    close.add_opp(lead_id, SS_BOOKED, OTHER)  # the closer owns the opportunity once it's booked
    close.add_activity("meeting", lead_id, starts_at=(now + meeting_in).isoformat(), date_created=now.isoformat(),
                       user_id=OTHER, status="upcoming")
    close.views["v_p"].add(lead_id)
    return lead


def texts(close, lead_id):
    return [s["text"] for s in close.created_sms if s["lead_id"] == lead_id]


async def tick_all(seq, n=20):
    for _ in range(n):
        await seq.due_tick()
    for sms in seq.close.created_sms:  # Close moves outbox texts to sent within seconds
        if sms.get("status") == "outbox":
            sms["status"] = "sent"


def test_nurture_sends_first_text_on_enroll_then_daily_nudge(tmp_path):
    seq, close, clock = setup(tmp_path)
    nurture_lead(close)

    async def go():
        await seq.sweep(NURTURE)
        await tick_all(seq)
        assert texts(close, "lead_n") == [render("intro", "Sam")]
        sms = close.created_sms[0]
        assert sms["local_phone"] == "+19786669462" and sms["remote_phone"] == "+13125550100"
        clock.advance(hours=23)
        await tick_all(seq)
        assert len(texts(close, "lead_n")) == 1  # not a full day yet
        clock.advance(hours=1)
        await tick_all(seq)
    run(go())
    assert texts(close, "lead_n")[1] == render("nudge", "Sam")
    assert ("lead_n", "Booked nurture text 2/8 (nudge) sent") in close.notes


def test_nurture_full_run_follows_the_day_plan(tmp_path):
    seq, close, clock = setup(tmp_path, seq_window_days=(0, 1, 2, 3, 4, 5, 6))
    nurture_lead(close)
    sent_days = []

    async def go():
        await seq.sweep(NURTURE)
        for hour in range(0, 16 * 24):
            before = len(close.created_sms)
            await tick_all(seq, 2)
            if len(close.created_sms) > before:
                sent_days.append(hour / 24)
            clock.advance(hours=1)
    run(go())
    assert sent_days == [0, 1, 2, 4, 6, 8, 11, 15]
    assert texts(close, "lead_n") == [render(s.message, "Sam") for s in NURTURE_STEPS]
    assert seq.db.seq_get(NURTURE, "lead_n")["status"] == "done"


def test_reply_stops_nurture(tmp_path):
    seq, close, clock = setup(tmp_path)
    nurture_lead(close)

    async def go():
        await seq.sweep(NURTURE)
        await tick_all(seq)
        clock.advance(hours=3)
        close.add_activity("sms", "lead_n", direction="inbound", status="inbox", date_created=clock.now.isoformat())
        clock.advance(days=1)
        await tick_all(seq)
    run(go())
    assert len(texts(close, "lead_n")) == 1
    rec = seq.db.seq_get(NURTURE, "lead_n")
    assert rec["status"] == "stopped" and rec["reason"] == "lead replied by text"


def test_inbound_call_status_change_and_meeting_stop_nurture(tmp_path):
    seq, close, clock = setup(tmp_path)
    for lead_id in ("lead_call", "lead_status", "lead_meet"):
        nurture_lead(close, lead_id, phone=f"+1312555{abs(hash(lead_id)) % 10000:04d}")

    async def go():
        await seq.sweep(NURTURE)
        await tick_all(seq)
        clock.advance(hours=2)
        close.add_activity("call", "lead_call", direction="inbound", date_created=clock.now.isoformat())
        close.leads["lead_status"]["status_label"] = "Qualified"
        close.add_activity("meeting", "lead_meet", date_created=clock.now.isoformat(),
                           starts_at=(clock.now + timedelta(days=3)).isoformat())
        clock.advance(days=1)
        await tick_all(seq)
    run(go())
    reasons = {i: seq.db.seq_get(NURTURE, i)["reason"] for i in ("lead_call", "lead_status", "lead_meet")}
    assert reasons == {"lead_call": "lead called in",
                       "lead_status": "lead status changed from Potential to Qualified",
                       "lead_meet": "meeting booked"}
    assert len(close.created_sms) == 3  # only the first text each


def test_nurture_only_for_my_opportunities(tmp_path):
    seq, close, _ = setup(tmp_path)
    close.add_lead("lead_x", in_view=False)
    close.add_opp("lead_x", TC_COMPLETED, OTHER)
    close.views["v_n"].add("lead_x")
    run(seq.sweep(NURTURE))
    run(tick_all(seq))
    assert close.created_sms == []
    assert seq.db.seq_get(NURTURE, "lead_x")["status"] == "skipped"


def test_nothing_sends_on_sunday_or_after_6pm(tmp_path):
    seq, close, clock = setup(tmp_path, start=SUN_NOON)
    nurture_lead(close)

    async def go():
        await seq.sweep(NURTURE)
        await tick_all(seq)
        assert close.created_sms == []
        clock.now = datetime(2026, 11, 17, 0, 30, tzinfo=UTC)  # Monday 6:30pm Central
        await tick_all(seq)
        assert close.created_sms == []
        clock.now = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # Tuesday 9:00am Central
        await tick_all(seq)
    run(go())
    assert len(close.created_sms) == 1


def test_prep_meeting_two_plus_days_out_gets_slides_and_check_in(tmp_path):
    seq, close, clock = setup(tmp_path)
    prep_lead(close, timedelta(days=3), MON_9AM)

    async def go():
        await seq.sweep(PREP)
        await tick_all(seq)
        clock.advance(days=1)
        await tick_all(seq)
        clock.advance(days=1)
        await tick_all(seq)
    run(go())
    assert texts(close, "lead_p") == [render("slides", "Sam"), render("slides_check", "Sam")]
    assert SLIDES_URL in texts(close, "lead_p")[0]
    rec = seq.db.seq_get(PREP, "lead_p")
    assert rec["variant"] == "prep_long" and rec["status"] == "done"


def test_prep_meeting_within_two_days_gets_slides_only(tmp_path):
    seq, close, clock = setup(tmp_path)
    prep_lead(close, timedelta(hours=30), MON_9AM)

    async def go():
        await seq.sweep(PREP)
        await tick_all(seq)
        clock.advance(days=1)
        await tick_all(seq)
    run(go())
    assert texts(close, "lead_p") == [render("slides", "Sam")]
    assert seq.db.seq_get(PREP, "lead_p")["variant"] == "prep_short"


def test_prep_only_when_i_am_the_setter(tmp_path):
    seq, close, _ = setup(tmp_path)
    prep_lead(close, timedelta(days=3), MON_9AM, setter=OTHER)
    run(seq.sweep(PREP))
    run(tick_all(seq))
    assert close.created_sms == []
    assert seq.db.seq_get(PREP, "lead_p")["reason"] == "Setter Lead Owner is not you"


def test_prep_check_in_dropped_when_call_moved_too_close(tmp_path):
    seq, close, clock = setup(tmp_path)
    prep_lead(close, timedelta(days=3), MON_9AM)

    async def go():
        await seq.sweep(PREP)
        await tick_all(seq)
        meeting = close.activities[("meeting", "lead_p")][0]
        meeting["starts_at"] = (clock.now + timedelta(hours=25)).isoformat()  # rescheduled sooner
        clock.advance(days=1)
        await tick_all(seq)
    run(go())
    assert len(texts(close, "lead_p")) == 1
    assert seq.db.seq_get(PREP, "lead_p")["reason"] == "too close to the call for the check-in text"


def test_prep_waits_for_meeting_then_sends_slides_only(tmp_path):
    seq, close, clock = setup(tmp_path)
    prep_lead(close, timedelta(days=3), MON_9AM)
    close.activities[("meeting", "lead_p")] = []

    async def go():
        await seq.sweep(PREP)
        await tick_all(seq)
        assert close.created_sms == [] and seq.db.seq_get(PREP, "lead_p")["status"] == "waiting"
        clock.advance(minutes=31)
        await tick_all(seq)
    run(go())
    assert texts(close, "lead_p") == [render("slides", "Sam")]


def test_booking_moves_lead_from_nurture_to_prep(tmp_path):
    seq, close, clock = setup(tmp_path)
    lead = nurture_lead(close, "lead_b")

    async def go():
        await seq.sweep(NURTURE)
        await tick_all(seq)
        clock.advance(hours=2)
        lead[f"custom.{SETTER_FIELD}"] = ME
        close.add_opp("lead_b", SS_BOOKED, OTHER)
        close.add_activity("meeting", "lead_b", starts_at=(clock.now + timedelta(days=3)).isoformat(),
                           date_created=clock.now.isoformat())
        close.views["v_n"].discard("lead_b")
        close.views["v_p"].add("lead_b")
        await seq.sweep(PREP)
        await seq.sweep(NURTURE)
        await tick_all(seq)
    run(go())
    assert texts(close, "lead_b") == [render("intro", "Sam"), render("slides", "Sam")]
    assert seq.db.seq_get(NURTURE, "lead_b")["status"] == "stopped"
    assert seq.db.seq_get(PREP, "lead_b")["status"] == "active"


def test_pending_sms_postpones(tmp_path):
    seq, close, _ = setup(tmp_path)
    nurture_lead(close)
    close.add_activity("sms", "lead_n", direction="outbound", status="scheduled", user_id=ME)
    run(seq.sweep(NURTURE))
    run(tick_all(seq))
    assert close.created_sms == []
    assert seq.db.seq_get(NURTURE, "lead_n")["status"] == "active"


def test_send_errors_create_task_and_auto_stop(tmp_path):
    seq, close, _ = setup(tmp_path)
    for i in range(3):
        nurture_lead(close, f"lead_{i}", phone=f"+131255501{i:02d}")
    close.fail_create_sms = "carrier rejected"
    run(seq.sweep(NURTURE))
    run(tick_all(seq))
    assert all(seq.db.seq_get(NURTURE, f"lead_{i}")["status"] == "error" for i in range(3))
    assert len(close.tasks) == 3 and "carrier rejected" in close.tasks[0][1]
    assert seq.s.stop_file.exists()


def test_stop_file_and_dry_run_send_nothing(tmp_path):
    seq, close, _ = setup(tmp_path)
    nurture_lead(close)
    seq.s.stop_file.write_text("stop")
    run(seq.sweep(NURTURE))
    run(tick_all(seq))
    assert close.created_sms == [] and seq.db.seq_get(NURTURE, "lead_n") is None
    seq.s.stop_file.unlink()

    seq2, close2, _ = setup(tmp_path, dry_run=True)
    nurture_lead(close2)
    run(seq2.sweep(NURTURE))
    run(tick_all(seq2))
    assert close2.created_sms == [] and close2.notes == []
    assert seq2.db.seq_get(NURTURE, "lead_n")["step"] == 1


def test_disabled_sequences_do_nothing(tmp_path):
    seq, close, _ = setup(tmp_path, nurture_enabled=False, prep_enabled=False)
    nurture_lead(close)
    prep_lead(close, timedelta(days=3), MON_9AM)
    run(seq.sweep(NURTURE))
    run(seq.sweep(PREP))
    run(tick_all(seq))
    assert close.created_sms == []


def test_no_name_renders_cleanly():
    assert render("intro", None).startswith("Hey, it's Jarluen")
    assert render("calendar", None).startswith("Want me to check")
    assert "{name}" not in "".join(render(k, None) for k in ("question", "slides", "slides_check", "breakup"))


def test_first_run_leaves_existing_leads_alone(tmp_path):
    seq, close, clock = setup(tmp_path)
    seq.db.conn.execute("DELETE FROM kv")
    nurture_lead(close, "lead_old")

    async def go():
        await seq.sweep(NURTURE)  # first run after turning it on
        await tick_all(seq)
        nurture_lead(close, "lead_new", phone="+13125550199")
        await seq.sweep(NURTURE)
        await tick_all(seq)
    run(go())
    assert seq.db.seq_get(NURTURE, "lead_old")["status"] == "baseline"
    assert texts(close, "lead_old") == []
    assert texts(close, "lead_new") == [render("intro", "Sam")]
