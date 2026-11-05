"""End-to-end flows through the Engine with a fake Close."""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from outbound.db import Db
from outbound.engine import Engine

from .fakes import ME, OTHER, Clock, FakeClose, FakeTelegram, make_settings

UTC = timezone.utc
CT = ZoneInfo("America/Chicago")
DAY = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)    # 10am Central
NIGHT = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)   # 9pm Central


async def _nosleep(_):
    return None


def setup(tmp_path, start=DAY, dry_run=False, **settings):
    close, tg, clock = FakeClose(), FakeTelegram(), Clock(start)
    engine = Engine(make_settings(tmp_path, **settings), Db(), close, tg, dry_run=dry_run, clock=clock,
                    sleep=_nosleep, rng=random.Random(1))
    return engine, close, tg, clock


def run(coro):
    return asyncio.run(coro)


def test_daytime_lead_is_queued_then_sent_with_note(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    close.add_lead("lead_a", name="Sam Smith")

    async def go():
        await engine.on_lead_event("lead_a")
        assert engine.db.get("lead_a")["status"] == "queued"
        assert await engine.sender_tick()
    run(go())

    rec = engine.db.get("lead_a")
    assert rec["status"] == "sent"
    sms = close.created_sms[0]
    assert sms["status"] == "outbox"
    assert sms["local_phone"] == "+19786669462"
    assert sms["remote_phone"] == "+13125550100"
    assert sms["text"].split("!")[0] in ("Hey Sam", "Hi Sam")
    assert close.notes == [("lead_a", f"First text sent: Version {rec['message_version']}")]


def test_lead_outside_smart_view_is_ignored(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a", in_view=False)
    run(engine.on_lead_event("lead_a"))
    assert engine.db.get("lead_a") is None


def test_skip_is_logged_with_rule(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a", status="DNC")
    run(engine.on_lead_event("lead_a"))
    rec = engine.db.get("lead_a")
    assert (rec["status"], rec["skip_rule"]) == ("skipped", "6")
    assert engine.db.skip_counts_since("2000-01-01") == {"6": 1}


def test_rate_limit_15_seconds_and_60_per_hour(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    for i in range(3):
        close.add_lead(f"lead_{i}", phone=f"+1312555010{i}")

    async def go():
        for i in range(3):
            await engine.process_lead(f"lead_{i}", "webhook")
        assert await engine.sender_tick()
        assert not await engine.sender_tick()  # too soon
        clock.advance(seconds=14)
        assert not await engine.sender_tick()
        clock.advance(seconds=1)
        assert await engine.sender_tick()
    run(go())
    assert len(close.created_sms) == 2

    engine2, close2, _, clock2 = setup(tmp_path, max_sends_per_hour=2)
    for i in range(3):
        close2.add_lead(f"lead_{i}", phone=f"+1312555010{i}")

    async def go2():
        for i in range(3):
            await engine2.process_lead(f"lead_{i}", "webhook")
        await engine2.sender_tick()
        clock2.advance(seconds=20)
        await engine2.sender_tick()
        clock2.advance(seconds=20)
        assert not await engine2.sender_tick()  # hourly cap reached
        clock2.advance(minutes=60)
        assert await engine2.sender_tick()
    run(go2())


def test_night_leads_scheduled_8am_central_15s_apart_and_west_local(tmp_path):
    engine, close, *_ = setup(tmp_path, start=datetime(2026, 11, 6, 5, 0, tzinfo=UTC))  # 11pm CT / 9pm PT
    close.add_lead("lead_ny", phone="+12125550100")
    close.add_lead("lead_chi", phone="+13125550100")
    close.add_lead("lead_sf", phone="+14155550100")

    async def go():
        for lid in ("lead_ny", "lead_chi", "lead_sf"):
            await engine.process_lead(lid, "webhook")
    run(go())

    times = {s["lead_id"]: datetime.fromisoformat(s["date_scheduled"]) for s in close.created_sms}
    assert all(s["status"] == "scheduled" for s in close.created_sms)
    assert times["lead_ny"].astimezone(CT).strftime("%H:%M:%S") == "08:00:00"
    assert times["lead_chi"].astimezone(CT).strftime("%H:%M:%S") == "08:00:15"
    assert times["lead_sf"].astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M:%S") == "08:00:00"
    assert engine.db.get("lead_ny")["status"] == "scheduled"


def test_morning_check_deletes_scheduled_text_when_lead_replied(tmp_path):
    engine, close, _, clock = setup(tmp_path, start=NIGHT)
    close.add_lead("lead_a")
    close.add_lead("lead_b", phone="+13125550199")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        await engine.process_lead("lead_b", "webhook")
        close.add_activity("sms", "lead_a", direction="inbound", status="inbox", date_created=clock.now.isoformat())
        clock.now = datetime(2026, 11, 6, 13, 56, tzinfo=UTC)  # 7:56am Central
        await engine.morning_tick()
    run(go())

    a, b = engine.db.get("lead_a"), engine.db.get("lead_b")
    assert (a["status"], a["skip_rule"]) == ("skipped", "1")
    assert close.deleted_sms == [a["sms_activity_id"]]
    assert b["status"] == "scheduled" and b["morning_checked_at"]  # own scheduled SMS doesn't trip rules 4/7


def test_scheduled_text_going_out_is_recorded_with_note(tmp_path):
    engine, close, _, clock = setup(tmp_path, start=NIGHT)
    close.add_lead("lead_a")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        rec = engine.db.get("lead_a")
        close.sms[rec["sms_activity_id"]]["status"] = "sent"
        engine.dispatch_webhook({"object_type": "activity.sms", "action": "updated",
                                 "data": dict(close.sms[rec["sms_activity_id"]])})
        await asyncio.gather(*engine._tasks)
    run(go())
    assert engine.db.get("lead_a")["status"] == "sent"
    assert close.notes and close.notes[0][1].startswith("First text sent: Version ")


def test_sms_error_webhook_creates_task_and_three_errors_stop(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    for i in range(3):
        close.add_lead(f"lead_{i}", phone=f"+1312555010{i}")

    async def go():
        for i in range(3):
            await engine.process_lead(f"lead_{i}", "webhook")
            await engine.sender_tick()
            clock.advance(seconds=15)
        for i in range(3):
            sms_id = engine.db.get(f"lead_{i}")["sms_activity_id"]
            await engine.on_sms_update({"id": sms_id, "status": "error", "error_message": "Carrier rejected"})
    run(go())

    assert all(engine.db.get(f"lead_{i}")["status"] == "error" for i in range(3))
    assert ("lead_0", "First text failed: Carrier rejected") in close.tasks
    assert engine.s.stop_file.exists()
    assert len(tg.sent) == 1 and "STOPPED" in tg.sent[0]
    assert len(close.created_sms) == 3  # never retried


def test_two_minute_backup_check_catches_error(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    close.add_lead("lead_a")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        await engine.sender_tick()
        sms_id = engine.db.get("lead_a")["sms_activity_id"]
        close.sms[sms_id].update(status="error", error_message="Unreachable")
        clock.advance(seconds=60)
        await engine.verify_tick()
        assert engine.db.get("lead_a")["status"] == "sent"  # not due yet
        clock.advance(seconds=61)
        await engine.verify_tick()
    run(go())
    assert engine.db.get("lead_a")["status"] == "error"
    assert close.tasks == [("lead_a", "First text failed: Unreachable")]


def test_create_failure_is_an_error_and_not_retried(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a")
    close.fail_create_sms = "invalid remote_phone"

    async def go():
        await engine.process_lead("lead_a", "webhook")
        await engine.sender_tick()
        await engine.process_lead("lead_a", "sweep")
    run(go())
    assert engine.db.get("lead_a")["status"] == "error"
    assert close.tasks == [("lead_a", "First text failed: invalid remote_phone")]


def test_duplicate_failing_check_skips_and_creates_task(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_new", name="Sam Smith", phone="+13125550100")
    close.add_lead("lead_old", name="Samuel Smith", phone="(312) 555-0100", in_view=False)
    close.add_activity("call", "lead_old", user_id=OTHER, duration=300, date_created=DAY.isoformat())
    run(engine.process_lead("lead_new", "webhook"))
    rec = engine.db.get("lead_new")
    assert (rec["status"], rec["skip_rule"]) == ("skipped", "14")
    assert close.tasks == [("lead_new", "Possible duplicate of Samuel Smith, check before texting.")]
    run(engine.process_lead("lead_new", "sweep"))
    assert len(close.tasks) == 1  # task only once


def test_two_eligible_duplicates_only_one_gets_texted(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_1", phone="+13125550100")
    close.add_lead("lead_2", phone="+13125550100")

    async def go():
        await engine.process_lead("lead_1", "webhook")
        await engine.process_lead("lead_2", "webhook")
    run(go())
    assert engine.db.get("lead_1")["status"] == "queued"
    assert engine.db.get("lead_2")["skip_rule"] == "14"


def test_presend_recheck_skips_lead_that_became_ineligible(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        close.leads["lead_a"]["status_label"] = "Not Interested"
        await engine.sender_tick()
    run(go())
    assert close.created_sms == []
    assert engine.db.get("lead_a")["skip_rule"] == "6"


def test_stop_file_blocks_sending_and_scheduling(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        engine.s.stop_file.write_text("stop")
        assert not await engine.sender_tick()
        close.add_lead("lead_b", phone="+13125550111")
        await engine.process_lead("lead_b", "webhook")
        assert await engine.sweep() == 0
    run(go())
    assert close.created_sms == []
    assert engine.db.get("lead_b") is None


def test_dry_run_writes_nothing_to_close(tmp_path):
    engine, close, *_ = setup(tmp_path, dry_run=True)
    close.add_lead("lead_day")
    close.add_lead("lead_dup", phone="+13125550100")
    close.add_lead("lead_bad", status="DNC", phone="+13125550177")

    async def go():
        await engine.sweep()
        await engine.sender_tick()
    run(go())
    assert close.created_sms == [] and close.notes == [] and close.tasks == [] and close.deleted_sms == []
    statuses = {lid: engine.db.get(lid)["status"] for lid in ("lead_day", "lead_dup", "lead_bad")}
    assert statuses["lead_bad"] == "skipped"
    assert sorted(statuses.values()).count("sent") == 1


def test_queued_lead_past_8pm_is_scheduled_instead(tmp_path):
    late = datetime(2026, 11, 6, 1, 59, tzinfo=UTC)  # 7:59pm Central
    engine, close, _, clock = setup(tmp_path, start=late)
    close.add_lead("lead_a")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        assert engine.db.get("lead_a")["status"] == "queued"
        clock.advance(minutes=2)
        await engine.sender_tick()
    run(go())
    assert close.created_sms[0]["status"] == "scheduled"
    assert engine.db.get("lead_a")["status"] == "scheduled"


def test_webhook_burst_is_coalesced(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_a")

    async def go():
        for _ in range(5):
            engine.dispatch_webhook({"object_type": "lead", "action": "updated", "object_id": "lead_a"})
        await asyncio.gather(*list(engine._tasks))
    run(go())
    assert engine.db.get("lead_a")["status"] == "queued"


def test_pacific_lead_at_9pm_central_is_still_in_its_window(tmp_path):
    engine, close, *_ = setup(tmp_path, start=NIGHT)  # 9pm CT = 7pm PT
    close.add_lead("lead_sf", phone="+14155550100")
    run(engine.process_lead("lead_sf", "webhook"))
    assert engine.db.get("lead_sf")["status"] == "queued"


def test_test_mode_only_processes_listed_phones(tmp_path):
    engine, close, *_ = setup(tmp_path, test_phones=("+13125550100",))
    close.add_lead("lead_me", phone="(312) 555-0100")
    close.add_lead("lead_real", phone="+13125550142")

    async def go():
        await engine.sweep()
    run(go())
    assert engine.db.get("lead_me")["status"] == "queued"
    assert engine.db.get("lead_real") is None


def test_baseline_leads_are_left_alone(tmp_path):
    engine, close, *_ = setup(tmp_path)
    close.add_lead("lead_old")
    engine.db.upsert("lead_old", status="baseline")
    close.add_lead("lead_new", phone="+13125550142")
    run(engine.sweep())
    run(engine.on_lead_event("lead_old"))
    assert engine.db.get("lead_old")["status"] == "baseline"
    assert engine.db.get("lead_new")["status"] == "queued"
