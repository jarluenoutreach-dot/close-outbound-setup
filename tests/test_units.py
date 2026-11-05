from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from outbound.eligibility import check_activities, precheck
from outbound.messages import clean_first_name, render
from outbound.security import sign, verify_signature
from outbound.timeutil import parse_dt
from outbound.timezones import in_send_window, next_send_time, zones_for_phone

from .fakes import ME, OTHER

UTC = timezone.utc
CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # 9:00am Central


def lead(status="Potential", phone="+13125550100", name="Sam Smith"):
    contacts = [{"id": "cont_1", "name": name, "phones": [{"phone": phone}] if phone else []}]
    return {"id": "lead_1", "display_name": name, "status_label": status, "contacts": contacts}


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def check(sms=(), calls=(), meetings=(), **kw):
    return check_activities(lead(), list(sms), list(calls), list(meetings), my_user_id=ME, now=NOW, **kw)


# ---------------------------------------------------------------- rules
def test_clean_lead_is_eligible():
    assert precheck(lead(), local_record=None) is None
    assert check() is None


def test_rule1_reply_within_60_days():
    assert check(sms=[{"direction": "inbound", "status": "inbox", "date_created": ago(days=59)}]).rule == "1"
    assert check(sms=[{"direction": "inbound", "status": "inbox", "date_created": ago(days=61)}]) is None


def test_rule2_other_setter_long_call():
    assert check(calls=[{"user_id": OTHER, "duration": 120, "date_created": ago(days=10)}]).rule == "2"
    assert check(calls=[{"user_id": OTHER, "duration": 119, "date_created": ago(days=10)}]) is None
    assert check(calls=[{"user_id": ME, "duration": 600, "date_created": ago(days=10)}]) is None
    assert check(calls=[{"user_id": OTHER, "duration": 600, "date_created": ago(days=61)}]) is None


def test_rule3_meeting():
    assert check(meetings=[{"starts_at": ago(days=-3), "date_created": ago(days=1)}]).rule == "3"
    assert check(meetings=[{"starts_at": ago(days=30), "date_created": ago(days=40)}]).rule == "3"
    assert check(meetings=[{"starts_at": ago(days=90), "date_created": ago(days=95)}]) is None


def test_rule4_pending_sms_anyone():
    assert check(sms=[{"direction": "outbound", "status": "scheduled", "user_id": OTHER, "date_created": ago(days=90)}]).rule == "4"
    assert check(sms=[{"direction": "outbound", "status": "outbox", "user_id": ME}]).rule == "4"


def test_rule5_other_setter_text_window_is_a_setting():
    texted = [{"direction": "outbound", "status": "sent", "user_id": OTHER, "date_created": ago(days=6)}]
    assert check(sms=texted).rule == "5"
    assert check(sms=texted, other_setter_text_days=5) is None
    assert check(sms=[{"direction": "outbound", "status": "sent", "user_id": OTHER, "date_created": ago(days=8)}]) is None


def test_rule6_blocked_statuses():
    for status in ["DNC", "Wrong Number", "Bad Data", "Not Interested", "Customer", "Student", "Deposit",
                   "Wanting Refund", "No Show"]:
        assert precheck(lead(status=status), local_record=None).rule == "6", status
    assert precheck(lead(status="Qualified"), local_record=None) is None


def test_rule7_already_texted():
    assert precheck(lead(), local_record={"status": "sent", "sms_activity_id": "a1"}).rule == "7"
    assert precheck(lead(), local_record={"status": "skipped"}) is None
    assert precheck(lead(), local_record={"status": "scheduled", "sms_activity_id": "a1"},
                    ignore_sms_ids=frozenset({"a1"})) is None
    mine = [{"direction": "outbound", "status": "sent", "user_id": ME, "date_created": ago(days=300)}]
    assert check(sms=mine).rule == "7"
    assert check(sms=[{"direction": "outbound", "status": "draft", "user_id": ME}]) is None


def test_rule8_no_phone():
    assert precheck(lead(phone=None), local_record=None).rule == "8"


# ---------------------------------------------------------- timezones
def test_zones_from_area_code():
    assert [z.key for z in zones_for_phone("+12125550100")] == ["America/New_York"]
    assert [z.key for z in zones_for_phone("+14155550100")] == ["America/Los_Angeles"]
    assert [z.key for z in zones_for_phone("+18005550100")] == ["America/Chicago"]  # toll-free → default
    assert [z.key for z in zones_for_phone("garbage")] == ["America/Chicago"]


def test_send_window():
    eastern = zones_for_phone("+12125550100")
    assert in_send_window(datetime(2026, 11, 5, 13, 0, tzinfo=UTC), eastern)  # 8:00 ET
    assert not in_send_window(datetime(2026, 11, 5, 12, 59, tzinfo=UTC), eastern)
    assert not in_send_window(datetime(2026, 11, 6, 1, 0, tzinfo=UTC), eastern)  # 8:00pm ET


def test_next_send_time_central_for_east_local_for_west():
    night = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)  # 9pm Central
    east = next_send_time(night, zones_for_phone("+12125550100"))
    assert east.astimezone(CT).strftime("%d %H:%M") == "06 08:00"
    west = next_send_time(night, zones_for_phone("+14155550100"))
    assert west.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%d %H:%M") == "06 08:00"
    mountain = next_send_time(night, zones_for_phone("+13035550100"))
    assert mountain.astimezone(ZoneInfo("America/Denver")).strftime("%d %H:%M") == "06 08:00"


def test_pacific_lead_early_morning_gets_same_day_8am_local():
    seven_am_pt = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # 7am PT, 9am CT
    when = next_send_time(seven_am_pt, zones_for_phone("+14155550100"))
    assert when.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%d %H:%M") == "05 08:00"


# ----------------------------------------------------------- messages
def test_messages():
    assert render(1, "Sam").startswith("Hey Sam! My name is Jarluen.")
    assert render(2, None).startswith("Hey! This is Jarluen")
    assert render(3, None).startswith("Hi! Jarluen here")
    assert clean_first_name("SAM SMITH") == "Sam"
    assert clean_first_name("+1 312 555 0100") is None
    assert clean_first_name("") is None
    assert clean_first_name("Mary-Jane Watson") == "Mary-Jane"


# ----------------------------------------------------------- signature
def test_signature_verification():
    key = "ab" * 32
    body = b'{"event": {}}'
    ts = "1700000000"
    good = sign(key, ts, body)
    assert verify_signature(key, body, good, ts, now=1700000010)
    assert not verify_signature(key, body + b" ", good, ts, now=1700000010)
    assert not verify_signature(key, body, good, ts, now=1700009999)  # too old
    assert not verify_signature(key, body, None, ts)
    assert not verify_signature("", body, good, ts)
    assert not verify_signature(key, body, "0" * 64, ts, now=1700000010)


def test_parse_dt_formats():
    assert parse_dt("2026-11-05T15:00:00Z") == datetime(2026, 11, 5, 15, tzinfo=UTC)
    assert parse_dt("2026-11-05T15:00:00.1+00:00").microsecond == 100000
    assert parse_dt("2026-11-05") == datetime(2026, 11, 5, tzinfo=UTC)
