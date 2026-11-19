"""End-to-end flows through the Engine with a fake Close."""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from outbound.db import Db
from outbound.engine import Engine
from outbound.followup import MSG_4, PATH_A, PATH_C
from outbound.messages import VERSIONS

from .fakes import ME, OTHER, Clock, FakeClose, FakeTelegram, make_settings

UTC = timezone.utc
CT = ZoneInfo("America/Chicago")
DAY = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)    # 10am Central
NIGHT = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)   # 9pm Central


async def _nosleep(_):
    return None


def setup(tmp_path, start=DAY, dry_run=False, **settings):
    clock = Clock(start)
    close, tg = FakeClose(clock=clock), FakeTelegram()
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


def test_sms_error_webhook_no_task_and_three_errors_stop(tmp_path):
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
    assert close.tasks == []  # no task on failure — tracked internally only
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
    assert close.tasks == []  # no task on failure — tracked internally only


def test_carrier_content_rejection_retries_with_another_template(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    close.add_lead("lead_a", name="Sam Smith")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        await engine.sender_tick()
        rec = engine.db.get("lead_a")
        first_version, sms_id = rec["message_version"], rec["sms_activity_id"]
        close.sms[sms_id].update(status="error", error_message="flagged as going against carrier guidelines")
        clock.advance(seconds=121)
        await engine.verify_tick()
        return first_version
    first_version = run(go())

    rec = engine.db.get("lead_a")
    assert rec["status"] == "sent"  # retried, not given up
    assert rec["message_version"] != first_version
    assert {int(v) for v in rec["tried_versions"].split(",")} == {first_version, rec["message_version"]}
    assert len(close.created_sms) == 2  # original + one retry
    assert close.tasks == []  # no "failed" task yet — still in flight


def test_carrier_content_rejection_gives_up_after_all_templates_tried(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    close.add_lead("lead_a", name="Sam Smith")

    async def go():
        await engine.process_lead("lead_a", "webhook")
        await engine.sender_tick()
        # Every subsequent send also gets flagged — walk through all template versions.
        for _ in range(len(VERSIONS)):
            rec = engine.db.get("lead_a")
            close.sms[rec["sms_activity_id"]].update(
                status="error", error_message="flagged as going against carrier guidelines")
            clock.advance(seconds=121)
            await engine.verify_tick()
    run(go())

    rec = engine.db.get("lead_a")
    assert rec["status"] == "error"  # out of templates to try, finally gives up
    assert len(close.created_sms) == len(VERSIONS)  # tried every version exactly once
    assert close.tasks == []  # no task on failure — tracked internally only


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
    assert close.tasks == []  # no task on failure — tracked internally only


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


def test_sweep_widens_lookback_after_a_pause_gap(tmp_path):
    engine, close, tg, clock = setup(tmp_path)

    async def go():
        await engine.sweep()  # first sweep: no gap yet, default lookback
        assert close.last_lookback_minutes == 15
        clock.advance(hours=2)  # simulate a long pause (stop file, Telegram, whatever)
        await engine.sweep()  # first sweep after the gap: should widen to cover it
        assert close.last_lookback_minutes >= 120
        clock.advance(seconds=5)
        await engine.sweep()  # back to normal once caught up
        assert close.last_lookback_minutes == 15
    run(go())


def test_sweep_does_not_widen_lookback_while_stopped(tmp_path):
    engine, close, tg, clock = setup(tmp_path)

    async def go():
        await engine.sweep()
        engine.s.stop_file.write_text("stopped")
        clock.advance(hours=2)
        assert await engine.sweep() == 0  # paused: does nothing, doesn't even call Close
        engine.s.stop_file.unlink()
        await engine.sweep()  # first sweep after resuming: now it should widen
        assert close.last_lookback_minutes >= 120
    run(go())


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


# --------------------------------------------------------- follow-up buttons
def _send_first_text(engine, close, lead_id="lead_a", name="Sam Smith"):
    close.add_lead(lead_id, name=name)
    run(engine.process_lead(lead_id, "webhook"))
    run(engine.sender_tick())
    assert engine.db.get(lead_id)["status"] == "sent"
    return lead_id


def test_reply_notifies_with_buttons_for_the_current_step(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="Yes I'm interested!")

    run(engine.reply_tick())

    assert len(tg.sent_with_buttons) == 1
    text, buttons = tg.sent_with_buttons[0]
    assert "Sam" in text and "Yes I'm interested!" in text
    assert [label for label, _ in buttons] == ["Msg 2", "✏️ Write my own", "🚫 No action needed"]
    assert engine.db.get(lead_id)["followup_notified_sms_id"] == "acti_reply1"

    run(engine.reply_tick())  # same reply again: must not re-notify
    assert len(tg.sent_with_buttons) == 1


def test_tapping_a_button_sends_the_exact_pre_written_text_and_advances(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="yes")
    run(engine.reply_tick())
    _, buttons = tg.sent_with_buttons[0]
    token = buttons[0][1]  # the "Msg 2" button's token

    result = run(engine.handle_followup_button(token))

    assert "Cool, what state are you from" in result
    assert close.created_sms[-1]["text"] == "Cool, what state are you from and what do you do for work?"
    assert engine.db.get(lead_id)["followup_step"] == "msg2"


def test_a_used_or_unknown_button_token_is_rejected_not_resent(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="yes")
    run(engine.reply_tick())
    _, buttons = tg.sent_with_buttons[0]
    token = buttons[0][1]
    run(engine.handle_followup_button(token))  # first tap: consumes it
    sent_before = len(close.created_sms)

    result = run(engine.handle_followup_button(token))  # tapped again (or a stale copy)

    assert "already" in result.lower() or "expired" in result.lower()
    assert len(close.created_sms) == sent_before  # nothing new sent


def test_a_different_button_in_the_same_notification_can_correct_a_wrong_tap(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    # Walk the lead to the msg4 step, which offers three paths.
    close.add_activity("sms", lead_id, id="acti_1", direction="inbound", text="yes", date_created=clock.now.isoformat())
    run(engine.reply_tick())
    run(engine.handle_followup_button(tg.sent_with_buttons[0][1][0][1]))  # tap "Msg 2"
    clock.advance(minutes=1)
    close.add_activity("sms", lead_id, id="acti_2", direction="inbound", text="Texas, driver", date_created=clock.now.isoformat())
    run(engine.reply_tick())
    run(engine.handle_followup_button(tg.sent_with_buttons[1][1][0][1]))  # tap "Msg 3"
    clock.advance(minutes=1)
    close.add_activity("sms", lead_id, id="acti_3", direction="inbound", text="because freedom", date_created=clock.now.isoformat())
    run(engine.reply_tick())
    msg4_buttons = tg.sent_with_buttons[2][1]
    assert [label for label, _ in msg4_buttons][:1] == ["Msg 4"]
    assert "Msg 4 (positive)" not in [label for label, _ in msg4_buttons]
    run(engine.handle_followup_button(msg4_buttons[0][1]))  # tap "Msg 4"
    assert close.created_sms[-1]["text"] == MSG_4
    clock.advance(minutes=1)
    close.add_activity("sms", lead_id, id="acti_4", direction="inbound", text="not really", date_created=clock.now.isoformat())
    run(engine.reply_tick())
    buttons = tg.sent_with_buttons[3][1]
    labels = [label for label, _ in buttons]
    assert labels[:3] == ["Path A: Coaching", "Path B: Self-paced", "Path C: Not ready"]
    wrong_token = buttons[0][1]  # tapped Path A by mistake
    right_token = buttons[2][1]  # meant Path C

    first = run(engine.handle_followup_button(wrong_token))
    assert "quick 5-10 min call" in first
    second = run(engine.handle_followup_button(right_token))  # correct the mistake

    assert "free resources" in second
    assert "can't be unsent" in second
    assert engine.db.get(lead_id)["followup_step"] == "done"
    assert [s["text"] for s in close.created_sms[-2:]] == [PATH_A, PATH_C]


def test_write_my_own_then_say_sends_the_humans_exact_text(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="what's the catch?")
    run(engine.reply_tick())
    _, buttons = tg.sent_with_buttons[0]
    custom_token = buttons[-2][1]
    assert buttons[-2][0] == "✏️ Write my own"

    prompt = run(engine.handle_followup_button(custom_token))
    assert "/say" in prompt and "Sam Smith" in prompt

    result = run(engine.handle_custom_send("/say No catch at all, just want to help!"))

    assert close.created_sms[-1]["text"] == "No catch at all, just want to help!"
    assert "Sam Smith" in result

    # The pending slot is cleared after use — a second /say with nothing queued is rejected.
    again = run(engine.handle_custom_send("/say another message"))
    assert "no pending" in again.lower()
    assert len(close.created_sms) == 2  # first text + the one custom send, nothing extra


def test_followup_step_catches_up_when_a_human_texts_the_script_directly(tmp_path):
    from outbound.followup import MSG_3

    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    # A teammate replies in Close itself, skipping our buttons entirely, and jumps to Msg 3.
    close.add_activity("sms", lead_id, direction="outbound", text=MSG_3, date_created=clock.now.isoformat())
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="because freedom",
                        date_created=clock.now.isoformat())

    run(engine.reply_tick())

    assert engine.db.get(lead_id)["followup_step"] == "msg3"
    _, buttons = tg.sent_with_buttons[0]
    labels = [label for label, _ in buttons]
    assert labels[:1] == ["Msg 4"]  # msg3 step's options, not Msg 2


def test_dismiss_button_marks_handled_and_clears_from_pending(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="not interested",
                        date_created=clock.now.isoformat())
    run(engine.reply_tick())
    _, buttons = tg.sent_with_buttons[0]
    dismiss_token = buttons[-1][1]
    assert buttons[-1][0] == "🚫 No action needed"

    sent_before = len(close.created_sms)
    result = run(engine.handle_followup_button(dismiss_token))

    assert "no action needed" in result.lower()
    assert len(close.created_sms) == sent_before  # dismiss never sends anything
    pending = run(engine.pending_summary())
    assert "nothing pending" in pending.lower()


def test_pending_summary_lists_unanswered_replies_then_clears_after_a_response(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    clock.advance(minutes=1)
    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="tell me more",
                        date_created=clock.now.isoformat())

    pending = run(engine.pending_summary())
    assert "Sam Smith" in pending and "tell me more" in pending

    run(engine.reply_tick())
    _, buttons = tg.sent_with_buttons[0]
    run(engine.handle_followup_button(buttons[0][1]))  # answer it (real send-time clock advances this)

    pending_after = run(engine.pending_summary())
    assert "nothing pending" in pending_after.lower()


def test_custom_message_overwrite_warns_about_the_replaced_lead(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_a = _send_first_text(engine, close, lead_id="lead_a", name="Alice")
    clock.advance(seconds=20)  # clear the per-send rate limit
    close.add_lead("lead_b", name="Bob", phone="+13125550199")
    run(engine.process_lead("lead_b", "webhook"))
    run(engine.sender_tick())
    assert engine.db.get("lead_b")["status"] == "sent"

    close.add_activity("sms", lead_a, id="acti_a", direction="inbound", text="hi", date_created=clock.now.isoformat())
    close.add_activity("sms", "lead_b", id="acti_b", direction="inbound", text="hey", date_created=clock.now.isoformat())
    run(engine.reply_tick())
    buttons_a = tg.sent_with_buttons[0][1]
    buttons_b = tg.sent_with_buttons[1][1]

    run(engine.handle_followup_button(buttons_a[-2][1]))  # tap "Write my own" for Alice first
    result = run(engine.handle_followup_button(buttons_b[-2][1]))  # then for Bob, without sending Alice's first

    assert "Alice" in result and "replaced" in result.lower()
    send_result = run(engine.handle_custom_send("/say hello"))
    assert "Bob" in send_result  # the most recent tap wins


def test_report_includes_reply_funnel_by_furthest_step(tmp_path):
    from outbound.report import build_report

    engine, close, tg, clock = setup(tmp_path)
    lead_a = _send_first_text(engine, close, lead_id="lead_a", name="Alice")
    clock.advance(seconds=20)
    close.add_lead("lead_b", name="Bob", phone="+13125550199")
    run(engine.process_lead("lead_b", "webhook"))
    run(engine.sender_tick())
    engine.db.upsert(lead_a, followup_step="msg2")  # Alice progressed to Msg 2, Bob stayed at opener

    report = run(build_report(engine, clock.now))

    assert "Reply funnel" in report
    assert "Reached Msg 2: 1 (50%)" in report
    assert "Texted (opener sent): 2 (100%)" in report


def test_stop_file_pauses_new_outbound_but_not_reply_notifications(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close)
    engine.s.stop_file.write_text("stopped")
    close.add_lead("lead_new", phone="+13125550111")

    close.add_activity("sms", lead_id, id="acti_reply1", direction="inbound", text="still here")
    notified = run(engine.reply_tick())
    assert notified == 1  # keeps notifying on existing conversations while stopped
    assert len(tg.sent_with_buttons) == 1

    run(engine.process_lead("lead_new", "webhook"))
    assert engine.db.get("lead_new") is None  # but a brand new lead is not touched

    # Buttons and /say still work while stopped, too.
    buttons = tg.sent_with_buttons[0][1]
    result = run(engine.handle_followup_button(buttons[0][1]))
    assert "Cool, what state are you from" in result


def test_reply_to_a_different_teammates_number_is_not_mistaken_for_a_reply_to_us(tmp_path):
    engine, close, tg, clock = setup(tmp_path)
    lead_id = _send_first_text(engine, close, name="Cortasha")
    # A different teammate also texted this lead from their own number, and
    # the lead replied to THAT message — not to ours.
    close.add_activity("sms", lead_id, direction="outbound", local_phone="+19786338506",
                        text="Hi Cortasha! My name is Hazel.", date_created=clock.now.isoformat())
    close.add_activity("sms", lead_id, id="acti_wrong_thread", direction="inbound", local_phone="+19786338506",
                        text="No", date_created=clock.now.isoformat())

    notified = run(engine.reply_tick())

    assert notified == 0  # not our conversation thread — must not notify
    pending = run(engine.pending_summary())
    assert "nothing pending" in pending.lower()

    # A genuine reply to OUR number (the default local_phone) still works.
    close.add_activity("sms", lead_id, id="acti_right_thread", direction="inbound",
                        text="Yes interested", date_created=clock.now.isoformat())
    assert run(engine.reply_tick()) == 1
