"""Runs the follow-up sequences (outbound/sequences.py) inside the same service as the first texts.

Flow for each sequence:
  sweep (every 5 min): new lead in the smart view -> evaluate -> enroll once (or record why not)
  due tick (every 15s): next step due + inside Mon–Sat 9–6 Central -> full re-check -> send -> note
  verify tick: 2 min after each send, look the SMS up again; a carrier error stops that run

Texts share the first-text service's 15s gap / hourly cap and its STOP file.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .close_api import CloseError
from .eligibility import (
    _BLOCKED,
    activity_time,
    first_phone,
    is_inbound_sms,
    lead_name,
    lead_phones,
    normalize_phone,
)
from .messages import clean_first_name
from .sequences import (
    NURTURE,
    PREP,
    SEQUENCE_LABELS,
    SETTER_FIELD,
    SS_BOOKED,
    SS_COMPLETED,
    TC_BOOKED,
    TC_COMPLETED,
    VARIANT_STEPS,
    render,
)
from .timeutil import iso, parse_dt

log = logging.getLogger("outbound.seq")

NURTURE_OPP_STATUSES = {TC_COMPLETED, TC_BOOKED, SS_COMPLETED}
_PENDING = {"scheduled", "outbox"}
_CANCELED_WORDS = ("cancel", "declin", "no-show", "noshow")


class SequenceRunner:
    def __init__(self, engine):
        self.e = engine
        self.s = engine.s
        self.db = engine.db
        self.close = engine.close
        self.tz = ZoneInfo(self.s.seq_timezone)

    # ------------------------------------------------------------- settings
    def enabled(self, sequence: str) -> bool:
        return self.s.nurture_enabled if sequence == NURTURE else self.s.prep_enabled

    def view_id(self, sequence: str) -> str:
        return self.s.nurture_view_id if sequence == NURTURE else self.s.prep_view_id

    def _event(self, event, lead_id=None, rule=None, detail=None):
        self.e._event(event, lead_id, rule, detail)

    # ---------------------------------------------------------- send window
    def in_window(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return (local.weekday() in self.s.seq_window_days
                and self.s.seq_window_start_hour <= local.hour < self.s.seq_window_end_hour)

    def next_open(self, now: datetime) -> datetime:
        if self.in_window(now):
            return now
        local = now.astimezone(self.tz)
        for days in range(0, 8):
            day = local.date() + timedelta(days=days)
            start = datetime.combine(day, time(self.s.seq_window_start_hour), tzinfo=self.tz)
            if start.weekday() in self.s.seq_window_days and start > local:
                return start.astimezone(now.tzinfo)
        return now + timedelta(days=1)

    def _local(self, dt: datetime | None) -> str:
        return dt.astimezone(self.tz).strftime("%a %b %d %I:%M %p %Z") if dt else "?"

    # -------------------------------------------------------------- lookups
    async def _load(self, lead_id: str) -> dict | None:
        lead = await self.close.get_lead_full(lead_id)
        if lead is None:
            return None
        opps = await self.close.list_opportunities(lead_id)
        sms = await self.close.list_activities("sms", lead_id)
        calls = await self.close.list_activities("call", lead_id)
        meetings = await self.close.list_activities("meeting", lead_id)
        return {"lead": lead, "opps": opps, "sms": sms, "calls": calls, "meetings": meetings}

    def _is_mine(self, sequence: str, data: dict) -> str | None:
        """None if this is Jarluen's lead for this sequence, else why not."""
        me = self.s.my_user_id
        if sequence == NURTURE:
            if any(o.get("user_id") == me and o.get("status_id") in NURTURE_OPP_STATUSES for o in data["opps"]):
                return None
            return "no TC Completed / TC Booked / SS Completed opportunity owned by you"
        setter = data["lead"].get(f"custom.{SETTER_FIELD}")
        if setter != me:
            return "Setter Lead Owner is not you"
        if not any(o.get("status_id") == SS_BOOKED for o in data["opps"]):
            return "no opportunity in SS Booked"
        return None

    def next_meeting(self, meetings: list[dict], now: datetime) -> datetime | None:
        upcoming = []
        for m in meetings:
            status = (m.get("status") or "").lower()
            if any(word in status for word in _CANCELED_WORDS):
                continue
            starts = parse_dt(m.get("starts_at") or m.get("activity_at"))
            if starts and starts > now:
                upcoming.append(starts)
        return min(upcoming) if upcoming else None

    # ------------------------------------------------------------ enrolling
    async def evaluate(self, sequence: str, lead_id: str) -> dict:
        """What enrolling this lead would do now. Reads Close, writes nothing."""
        now = self.e.clock()
        data = await self._load(lead_id)
        if data is None:
            return {"action": "skip", "reason": "lead no longer exists"}
        lead = data["lead"]
        plan = {"lead_name": lead_name(lead), "lead_status": lead.get("status_label") or ""}
        contact, phone = first_phone(lead)
        plan.update(contact_id=(contact or {}).get("id"), phone=normalize_phone(phone),
                    first_name=clean_first_name((contact or {}).get("name")))
        if self.s.test_phones and not lead_phones(lead) & set(self.s.test_phones):
            return {**plan, "action": "ignore", "reason": "TEST MODE: phone not in TEST_PHONES"}
        not_mine = self._is_mine(sequence, data)
        if not_mine:
            return {**plan, "action": "skip", "reason": not_mine}
        if plan["lead_status"].casefold() in _BLOCKED:
            return {**plan, "action": "skip", "reason": f"lead status is {plan['lead_status']}"}
        if not phone:
            return {**plan, "action": "skip", "reason": "no contact has a phone number"}
        if sequence == NURTURE:
            if any(o.get("status_id") == SS_BOOKED for o in data["opps"]):
                return {**plan, "action": "skip", "reason": "already has an SS Booked opportunity"}
            other = self.db.seq_get(PREP, lead_id)
            if other and other["status"] in ("active", "waiting"):
                return {**plan, "action": "skip", "reason": "in the SS prep texts"}
            return {**plan, "action": "enroll", "variant": "nurture", "meeting_at": None}
        meeting = self.next_meeting(data["meetings"], now)
        if meeting is None:
            return {**plan, "action": "wait", "variant": None, "meeting_at": None,
                    "reason": "no upcoming meeting found in Close yet"}
        hours = (meeting - now).total_seconds() / 3600
        variant = "prep_long" if hours >= self.s.prep_long_hours else "prep_short"
        return {**plan, "action": "enroll", "variant": variant, "meeting_at": meeting}

    async def enroll(self, sequence: str, lead_id: str) -> dict | None:
        now = self.e.clock()
        try:
            plan = await self.evaluate(sequence, lead_id)
        except CloseError as exc:
            log.error("[%s] could not check %s: %s", sequence, lead_id, exc)
            return None
        action = plan["action"]
        if action == "ignore":
            log.info("[%s] %s: %s", sequence, lead_id, plan["reason"])
            return plan
        common = {k: plan.get(k) for k in ("lead_name", "contact_id", "phone", "first_name", "lead_status")}
        if action == "skip":
            self.db.seq_upsert(sequence, lead_id, status="skipped", reason=plan["reason"], enrolled_at=iso(now), **common)
            self._event("seq_skipped", lead_id, rule=sequence, detail=plan["reason"])
            log.info("[%s] SKIP %s (%s): %s", sequence, plan.get("lead_name"), lead_id, plan["reason"])
            return plan
        if action == "wait":
            self.db.seq_upsert(sequence, lead_id, status="waiting", reason=plan["reason"], enrolled_at=iso(now),
                               due_at=iso(now + timedelta(minutes=5)), **common)
            log.info("[%s] WAITING %s (%s): %s", sequence, plan.get("lead_name"), lead_id, plan["reason"])
            return plan
        self._start(sequence, lead_id, plan, now, common)
        return plan

    def _start(self, sequence, lead_id, plan, now, common) -> None:
        if sequence == PREP:
            nurture = self.db.seq_get(NURTURE, lead_id)
            if nurture and nurture["status"] in ("active", "waiting"):
                self.db.seq_upsert(NURTURE, lead_id, status="stopped", due_at=None,
                                   reason="booked a Strategy Session (moved to SS prep texts)")
                self._event("seq_stopped", lead_id, rule=NURTURE, detail="booked a Strategy Session")
                log.info("[nurture] STOP %s (%s): booked a Strategy Session", plan.get("lead_name"), lead_id)
        self.db.seq_upsert(
            sequence, lead_id, status="active", variant=plan["variant"], step=0, due_at=iso(now),
            meeting_at=iso(plan.get("meeting_at")), reason=None, enrolled_at=iso(now), attempts=0, **common,
        )
        steps = len(VARIANT_STEPS[plan["variant"]])
        extra = f", meeting {self._local(plan.get('meeting_at'))}" if plan.get("meeting_at") else ""
        self._event("seq_enrolled", lead_id, rule=sequence, detail=f"{plan['variant']} ({steps} texts){extra}")
        log.info("[%s] ENROLLED %s (%s) %s, %s texts%s", sequence, plan.get("lead_name"), lead_id,
                 plan["variant"], steps, extra)

    async def sweep(self, sequence: str) -> int:
        if not self.enabled(sequence) or self.e.stopped():
            return 0
        ids = await self.close.view_lead_ids(self.view_id(sequence))
        members = set(ids)
        enrolled = 0
        for lead_id in ids:
            if self.e.stopped():
                break
            if self.db.seq_get(sequence, lead_id) is None:
                async with self.e._locks[lead_id]:
                    if self.db.seq_get(sequence, lead_id) is None:
                        await self.enroll(sequence, lead_id)
                        enrolled += 1
        for rec in self.db.seq_rows(sequence=sequence, statuses=("active", "waiting")):
            if rec["lead_id"] not in members:
                self._stop(rec, "left the smart view")
        log.info("[%s] sweep: %s leads in view, %s new", sequence, len(ids), enrolled)
        return enrolled

    # -------------------------------------------------------------- stopping
    def _stop(self, rec: dict, reason: str, status: str = "stopped") -> None:
        self.db.seq_upsert(rec["sequence"], rec["lead_id"], status=status, due_at=None, reason=reason)
        self._event("seq_stopped" if status == "stopped" else f"seq_{status}", rec["lead_id"],
                    rule=rec["sequence"], detail=reason)
        log.info("[%s] %s %s (%s): %s", rec["sequence"], status.upper(), rec.get("lead_name"), rec["lead_id"], reason)

    def stop_reason(self, rec: dict, data: dict | None, now: datetime) -> str | None:
        if data is None:
            return "lead was deleted"
        lead = data["lead"]
        not_mine = self._is_mine(rec["sequence"], data)
        if not_mine:
            return not_mine
        status = lead.get("status_label") or ""
        if status.casefold() in _BLOCKED:
            return f"lead status is {status}"
        if rec.get("lead_status") is not None and status != rec["lead_status"]:
            return f"lead status changed from {rec['lead_status'] or '(none)'} to {status}"
        since = parse_dt(rec["enrolled_at"])
        for s in data["sms"]:
            if is_inbound_sms(s) and (activity_time(s) or now) >= since:
                return "lead replied by text"
        for c in data["calls"]:
            if (c.get("direction") or "").lower() in ("inbound", "incoming") and (activity_time(c) or now) >= since:
                return "lead called in"
        if rec["sequence"] == NURTURE:
            if any(o.get("status_id") == SS_BOOKED for o in data["opps"]):
                return "opportunity moved to SS Booked"
            for m in data["meetings"]:
                created = parse_dt(m.get("date_created"))
                if created and created >= since:
                    return "meeting booked"
        return None

    # --------------------------------------------------------------- sending
    async def due_tick(self) -> bool:
        """Handle at most one due run. Returns True if a text went out."""
        if self.e.stopped():
            return False
        now = self.e.clock()
        for rec in self.db.seq_due(iso(now)):
            if not self.enabled(rec["sequence"]):
                continue
            if rec["status"] == "waiting":
                await self._resolve_waiting(rec, now)
                continue
            if not self.in_window(now) or self.e.send_wait_seconds() > 0:
                continue  # still let 'waiting' runs further down resolve
            async with self.e._locks[rec["lead_id"]]:
                rec = self.db.seq_get(rec["sequence"], rec["lead_id"])
                if rec and rec["status"] == "active":
                    return await self._advance(rec, now)
        return False

    async def _resolve_waiting(self, rec: dict, now: datetime) -> None:
        async with self.e._locks[rec["lead_id"]]:
            try:
                plan = await self.evaluate(rec["sequence"], rec["lead_id"])
            except CloseError as exc:
                log.error("[%s] could not re-check %s: %s", rec["sequence"], rec["lead_id"], exc)
                self.db.seq_upsert(rec["sequence"], rec["lead_id"], due_at=iso(now + timedelta(minutes=5)))
                return
            common = {k: plan.get(k) for k in ("lead_name", "contact_id", "phone", "first_name", "lead_status")}
            if plan["action"] == "enroll":
                self._start(rec["sequence"], rec["lead_id"], plan, now, common)
            elif plan["action"] == "wait":
                waited = now - parse_dt(rec["enrolled_at"])
                if waited >= timedelta(minutes=self.s.prep_meeting_wait_minutes):
                    # No meeting on the lead: send the slides once, no check-in.
                    self._start(rec["sequence"], rec["lead_id"], {**plan, "variant": "prep_short"}, now, common)
                else:
                    self.db.seq_upsert(rec["sequence"], rec["lead_id"], due_at=iso(now + timedelta(minutes=5)))
            else:
                self._stop({**rec, **common}, plan.get("reason") or plan["action"], status="skipped")

    async def _advance(self, rec: dict, now: datetime) -> bool:
        sequence, lead_id = rec["sequence"], rec["lead_id"]
        steps = VARIANT_STEPS[rec["variant"]]
        step_index = rec["step"] or 0
        if step_index >= len(steps):
            self._stop(rec, "all texts sent", status="done")
            return False
        try:
            data = await self._load(lead_id)
        except CloseError as exc:
            attempts = (rec.get("attempts") or 0) + 1
            log.error("[%s] pre-send check failed for %s (attempt %s): %s", sequence, lead_id, attempts, exc)
            if attempts >= 5:
                self._stop(rec, f"Close API kept failing: {exc}", status="error")
            else:
                self.db.seq_upsert(sequence, lead_id, attempts=attempts, due_at=iso(now + timedelta(minutes=5)))
            return False
        reason = self.stop_reason(rec, data, now)
        if reason:
            self._stop(rec, reason)
            return False

        # Don't step on another pending text (a first text, a teammate's scheduled SMS...).
        own = rec.get("last_sms_id")
        if any(not is_inbound_sms(s) and s.get("id") != own and (s.get("status") or "").lower() in _PENDING
               for s in data["sms"]):
            self.db.seq_upsert(sequence, lead_id, due_at=iso(now + timedelta(minutes=10)))
            log.info("[%s] %s has a pending SMS in Close; trying again in 10 min", sequence, lead_id)
            return False

        if sequence == PREP:
            meeting = self.next_meeting(data["meetings"], now) or parse_dt(rec.get("meeting_at"))
            if meeting and meeting <= now:
                self._stop(rec, "the Strategy Session time has passed", status="done")
                return False
            if meeting and iso(meeting) != rec.get("meeting_at"):
                self.db.seq_upsert(sequence, lead_id, meeting_at=iso(meeting))
            if step_index > 0 and meeting and meeting - now < timedelta(hours=self.s.prep_followup_hours_before):
                self._stop(rec, "too close to the call for the check-in text", status="done")
                return False

        lead = data["lead"]
        phones = lead_phones(lead)
        phone = rec.get("phone") if rec.get("phone") in phones else None
        contact_id = rec.get("contact_id")
        if phone is None:
            contact, raw = first_phone(lead)
            phone, contact_id = normalize_phone(raw), (contact or {}).get("id")
        if not phone:
            self._stop(rec, "no contact has a phone number")
            return False

        step = steps[step_index]
        text = render(step.message, rec.get("first_name"))
        label = f"{SEQUENCE_LABELS[sequence]} text {step_index + 1}/{len(steps)} ({step.message})"
        sms_id = None
        if self.e.dry_run:
            log.info("[DRY RUN] would SEND %s to %s (%s) %s: %s", label, rec.get("lead_name"), lead_id, phone, text)
        else:
            try:
                sms = await self.close.create_sms({
                    "lead_id": lead_id, "contact_id": contact_id, "local_phone": self.s.local_phone,
                    "remote_phone": phone, "text": text, "status": "outbox",
                })
            except CloseError as exc:
                self._event("seq_send_failed", lead_id, rule=sequence, detail=str(exc))
                await self._error(rec, exc.message, label)
                return False
            sms_id = sms.get("id")
        last = step_index + 1 >= len(steps)
        self.db.seq_upsert(
            sequence, lead_id, step=step_index + 1, last_sent_at=iso(now), last_sms_id=sms_id, attempts=0,
            verify_after=iso(now + timedelta(seconds=self.s.error_check_delay_seconds)) if sms_id else None,
            status="done" if last else "active", reason="all texts sent" if last else None,
            due_at=None if last else iso(now + steps[step_index + 1].delay), phone=phone, contact_id=contact_id,
        )
        self._event("seq_sent", lead_id, rule=sequence, detail=f"{label} to {phone} sms={sms_id}")
        if not self.e.dry_run:
            log.info("[%s] SENT %s to %s (%s) %s sms=%s", sequence, label, rec.get("lead_name"), lead_id, phone, sms_id)
            await self.e._note(lead_id, f"{label} sent")
        return True

    # ---------------------------------------------------------------- errors
    async def _error(self, rec: dict, message: str, label: str) -> None:
        self._stop(rec, f"text failed: {message}", status="error")  # logs the seq_error event
        await self.e._task(rec["lead_id"], f"{label} failed: {message}")
        now = self.e.clock()
        hour_ago = iso(now - timedelta(hours=1))
        count = self.db.count_events_since("seq_error", hour_ago) + self.db.errors_since(hour_ago)
        if count < self.s.error_stop_threshold or self.e.dry_run or self.s.stop_file.exists():
            return
        self.s.stop_file.write_text(
            f"Created automatically at {iso(now)}: {count} texts errored within one hour.\n"
            f"Last error: {message}\nDelete this file (or send /resume to the bot) to resume.\n"
        )
        log.critical("STOP file created: %s texts errored within one hour", count)
        self._event("auto_stop", detail=f"{count} errors in 1h; last: {message}")
        await self.e.telegram.send(
            f"🛑 Close outbound texting STOPPED\n\n{count} texts errored within the last hour.\n"
            f"Last error: {message}\n\nNothing will be sent until you fix the cause and send /resume."
        )

    async def verify_tick(self) -> None:
        now = self.e.clock()
        for rec in self.db.seq_to_verify(iso(now)):
            try:
                sms = await self.close.get_sms(rec["last_sms_id"])
            except CloseError as exc:
                log.warning("[%s] backup check for %s failed: %s", rec["sequence"], rec["lead_id"], exc)
                continue
            status = ((sms or {}).get("status") or "").lower()
            if status == "error":
                steps = VARIANT_STEPS.get(rec["variant"] or "", ())
                label = f"{SEQUENCE_LABELS[rec['sequence']]} text {rec['step']}/{len(steps)}"
                self.db.seq_upsert(rec["sequence"], rec["lead_id"], verify_after=None)
                await self._error(rec, sms.get("error_message") or "unknown error (Close gave no message)", label)
            elif status in ("outbox", "scheduled") and (rec.get("attempts") or 0) < 10:
                self.db.seq_upsert(rec["sequence"], rec["lead_id"], attempts=(rec.get("attempts") or 0) + 1,
                                   verify_after=iso(now + timedelta(seconds=60)))
            else:
                self.db.seq_upsert(rec["sequence"], rec["lead_id"], verify_after=None, attempts=0)

    # ----------------------------------------------------------------- admin
    async def baseline(self, sequence: str) -> int:
        """Mark everyone already in the view as 'leave alone'."""
        count = 0
        for lead_id in await self.close.view_lead_ids(self.view_id(sequence)):
            if self.db.seq_get(sequence, lead_id) is None:
                self.db.seq_upsert(sequence, lead_id, status="baseline", reason="in the view before go-live")
                count += 1
        return count

    def status_lines(self) -> list[str]:
        counts = self.db.seq_counts()
        lines = []
        for sequence in (NURTURE, PREP):
            state = "ON" if self.enabled(sequence) else "OFF"
            parts = [f"{status} {n}" for (seq, status), n in sorted(counts.items()) if seq == sequence]
            lines.append(f"{SEQUENCE_LABELS[sequence]} [{state}]: " + (", ".join(parts) or "nothing yet"))
        upcoming = self.db.seq_rows(statuses=("active",))[:5]
        for rec in upcoming:
            steps = VARIANT_STEPS[rec["variant"]]
            lines.append(f"  next: {rec.get('lead_name')} text {rec['step'] + 1}/{len(steps)} "
                         f"{self._local(parse_dt(rec['due_at']))}")
        return lines
