"""The core flow: webhook/sweep -> eligibility -> queue or schedule -> send -> verify.

One Engine runs inside the web server process. All Close writes go through it, and in dry-run
mode every write is replaced by a log line.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta

from .close_api import CloseClient, CloseError
from .config import Settings
from .db import SETTLED_STATUSES, Db
from .eligibility import (
    RULE_NAMES,
    Decision,
    base_decision,
    check_activities,
    lead_name,
    lead_phones,
    normalize_phone,
    precheck,
)
from .messages import VERSIONS, render
from .telegram import Telegram
from .timeutil import iso, parse_dt, utcnow
from .timezones import CENTRAL, in_send_window, next_send_time, zone_label, zones_for_phone

log = logging.getLogger("outbound")


class Engine:
    def __init__(
        self,
        settings: Settings,
        db: Db,
        close: CloseClient,
        telegram: Telegram | None = None,
        *,
        dry_run: bool = False,
        ignore_stop: bool = False,
        clock=utcnow,
        sleep=asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.s = settings
        self.db = db
        self.close = close
        self.telegram = telegram or Telegram("", "")
        self.dry_run = dry_run
        self.ignore_stop = ignore_stop
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.SystemRandom()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: set[str] = set()
        self._dirty: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._weekly_next_try: datetime | None = None

    # ------------------------------------------------------------------ utils
    def stopped(self) -> bool:
        return not self.ignore_stop and self.s.stop_file.exists()

    def _event(self, event: str, lead_id: str | None = None, rule: str | None = None, detail: str | None = None):
        self.db.log(event, lead_id=lead_id, rule=rule, detail=detail, ts=iso(self.clock()))

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Background task failed", exc_info=task.exception())

    def _prune_lock(self, lead_id: str) -> None:
        lock = self._locks.get(lead_id)
        if lock is not None and not lock.locked() and not getattr(lock, "_waiters", None):
            del self._locks[lead_id]

    def _lead_fields(self, decision: Decision, zones, source: str | None) -> dict:
        fields = {
            "lead_name": decision.lead_name,
            "contact_id": decision.contact_id,
            "phone": normalize_phone(decision.phone),
            "timezone": zone_label(zones),
        }
        if source:
            fields["source"] = source
        return fields

    def _local(self, dt: datetime, zones) -> str:
        zone = zones[0] if zones else CENTRAL
        return dt.astimezone(zone).strftime("%a %b %d %I:%M:%S %p %Z")

    # ------------------------------------------------- Close writes (dry-run aware)
    async def _note(self, lead_id: str, text: str) -> None:
        if self.dry_run:
            log.info("[DRY RUN] would add note on %s: %s", lead_id, text)
            return
        try:
            await self.close.create_note(lead_id, text)
        except CloseError as exc:
            log.error("Could not add note on %s: %s", lead_id, exc)

    async def _task(self, lead_id: str, text: str) -> bool:
        if self.dry_run:
            log.info("[DRY RUN] would create task on %s: %s", lead_id, text)
            return True
        try:
            today = self.clock().astimezone(CENTRAL).date().isoformat()
            await self.close.create_task(lead_id, text, assigned_to=self.s.my_user_id, date=today)
            log.info("Task created on %s: %s", lead_id, text)
            return True
        except CloseError as exc:
            log.error("Could not create task on %s (%s): %s", lead_id, text, exc)
            return False

    # ------------------------------------------------------- eligibility check
    async def _fetch_history(self, lead_id: str):
        return await asyncio.gather(
            self.close.list_activities("sms", lead_id),
            self.close.list_activities("call", lead_id),
            self.close.list_activities("meeting", lead_id),
        )

    async def _check_lead(self, lead: dict, *, ignore_sms_ids=frozenset(), require_phone=True) -> Decision:
        failed = precheck(
            lead,
            local_record=self.db.get(lead["id"]),
            ignore_sms_ids=ignore_sms_ids,
            require_phone=require_phone,
        )
        if failed:
            return failed
        sms, calls, meetings = await self._fetch_history(lead["id"])
        failed = check_activities(
            lead,
            sms,
            calls,
            meetings,
            my_user_id=self.s.my_user_id,
            now=self.clock(),
            lookback_days=self.s.lookback_days,
            other_setter_text_days=self.s.other_setter_text_days,
            long_call_seconds=self.s.long_call_seconds,
            ignore_sms_ids=ignore_sms_ids,
        )
        return failed or base_decision(lead)

    async def _check_duplicates(self, lead: dict, decision: Decision) -> Decision | None:
        """Rule 14: other leads with the same phone must also pass the check."""
        phone = normalize_phone(decision.phone)
        local = self.db.other_lead_with_phone(phone, lead["id"])
        if local:
            name = local.get("lead_name") or local["lead_id"]
            return replace(
                decision, eligible=False, rule="14", duplicate_of=name, duplicate_lead_id=local["lead_id"],
                reason=f"possible duplicate of {name} (already {local['status']} in local database)",
            )
        for other_id in await self.close.find_lead_ids_by_phone(phone):
            if other_id == lead["id"]:
                continue
            other = await self.close.get_lead(other_id)
            if not other or phone not in lead_phones(other):
                continue
            result = await self._check_lead(other, require_phone=False)
            if not result.eligible:
                name = lead_name(other)
                return replace(
                    decision, eligible=False, rule="14", duplicate_of=name, duplicate_lead_id=other_id,
                    reason=f"possible duplicate of {name}: that lead fails rule {result.rule} ({result.reason})",
                )
        return None

    async def full_check(self, lead_id: str, *, ignore_sms_ids=frozenset(), lead: dict | None = None) -> Decision:
        """Rules 1-8 plus the duplicate check. Raises CloseError if Close can't be read."""
        lead = lead or await self.close.get_lead(lead_id)
        if lead is None:
            return Decision(False, "gone", "lead no longer exists in Close", lead_id=lead_id)
        decision = await self._check_lead(lead, ignore_sms_ids=ignore_sms_ids)
        if not decision.eligible:
            return decision
        return await self._check_duplicates(lead, decision) or decision

    # ---------------------------------------------------------------- webhooks
    def dispatch_webhook(self, event: dict) -> None:
        object_type = event.get("object_type")
        action = event.get("action")
        if object_type == "lead" and action in ("created", "updated"):
            lead_id = event.get("object_id") or event.get("lead_id")
            if lead_id:
                self.spawn(self.on_lead_event(lead_id))
        elif object_type == "activity.sms":
            data = event.get("data") or {}
            sms_id = data.get("id") or event.get("object_id")
            if sms_id and self.db.by_sms_id(sms_id):
                data.setdefault("id", sms_id)
                self.spawn(self.on_sms_update(data))

    async def on_lead_event(self, lead_id: str) -> None:
        rec = self.db.get(lead_id)
        if rec and rec["status"] in SETTLED_STATUSES:
            return
        if lead_id in self._pending:  # already waiting/processing: run once more afterwards
            self._dirty.add(lead_id)
            return
        self._pending.add(lead_id)
        try:
            while True:
                self._dirty.discard(lead_id)
                await self.sleep(self.s.webhook_delay_seconds)
                await self.process_lead(lead_id, "webhook")
                if lead_id not in self._dirty:
                    break
        finally:
            self._pending.discard(lead_id)
            self._dirty.discard(lead_id)

    async def on_sms_update(self, sms: dict) -> None:
        rec = self.db.by_sms_id(sms["id"])
        if not rec:
            return
        status = (sms.get("status") or "").lower()
        async with self._locks[rec["lead_id"]]:
            rec = self.db.get(rec["lead_id"])
            if status == "error":
                await self._mark_error(rec["lead_id"], sms.get("error_message") or "unknown error (Close gave no message)")
            elif status == "sent" and rec["status"] == "scheduled":
                await self._scheduled_went_out(rec, verified=False)

    # -------------------------------------------------------- lead processing
    async def process_lead(self, lead_id: str, source: str, *, check_view: bool = True) -> Decision | None:
        try:
            return await self._process_lead(lead_id, source, check_view=check_view)
        finally:
            self._prune_lock(lead_id)

    async def _process_lead(self, lead_id: str, source: str, *, check_view: bool) -> Decision | None:
        async with self._locks[lead_id]:
            rec = self.db.get(lead_id)
            if rec and rec["status"] in SETTLED_STATUSES:
                return None
            if self.stopped():
                log.info("STOP file present: not processing lead %s (%s)", lead_id, source)
                return None
            try:
                if check_view and not await self.close.lead_in_view(self.s.smart_view_id, lead_id):
                    log.debug("Lead %s is not in the %s smart view; ignoring", lead_id, self.s.smart_view_name)
                    return None
                lead = None
                if self.s.test_phones:
                    lead = await self.close.get_lead(lead_id)
                    if not lead or not lead_phones(lead) & set(self.s.test_phones):
                        log.info("TEST MODE: ignoring lead %s (phone not in TEST_PHONES)", lead_id)
                        return None
                decision = await self.full_check(lead_id, lead=lead)
            except CloseError as exc:
                log.error("Eligibility check failed for %s: %s", lead_id, exc)
                self._event("check_failed", lead_id, detail=str(exc))
                return None
            if not decision.eligible:
                await self._skip(lead_id, decision, source)
                return decision
            await self._route(lead_id, decision, source)
            return decision

    async def _route(self, lead_id: str, decision: Decision, source: str, version: int | None = None) -> None:
        zones = zones_for_phone(decision.phone, self.s.default_timezone)
        now = self.clock()
        if in_send_window(now, zones, self.s.send_window_start_hour, self.s.send_window_end_hour):
            version = version or self.rng.choice(VERSIONS)
            self.db.upsert(
                lead_id,
                status="queued",
                **self._lead_fields(decision, zones, source),
                message_version=version,
                message_text=render(version, decision.first_name),
                queued_at=iso(now),
                skip_rule=None,
                skip_reason=None,
                last_checked_at=iso(now),
            )
            self._event("queued", lead_id, detail=f"v{version} to {decision.phone} ({zone_label(zones)})")
            log.info("QUEUED %s (%s) %s tz=%s version %s", decision.lead_name, lead_id, decision.phone, zone_label(zones), version)
        else:
            await self._schedule(lead_id, decision, zones, source, version)

    def _pick_slot(self, base: datetime) -> datetime:
        """First time >= base that is 15s from other scheduled texts and under the hourly cap."""
        spacing = timedelta(seconds=self.s.schedule_spacing_seconds)
        hour = timedelta(hours=1)
        taken = sorted(
            parse_dt(t) for t in self.db.scheduled_times(iso(base - hour), iso(base + timedelta(days=2)))
        )
        slot = base
        for _ in range(100_000):
            if any(abs(t - slot) < spacing for t in taken):
                slot += spacing
                continue
            if sum(1 for t in taken if slot - hour < t <= slot) >= self.s.max_sends_per_hour:
                slot += spacing
                continue
            return slot
        return slot

    async def _schedule(self, lead_id, decision: Decision, zones, source, version: int | None = None) -> None:
        now = self.clock()
        slot = self._pick_slot(next_send_time(now, zones, self.s.send_window_start_hour))
        version = version or self.rng.choice(VERSIONS)
        text = render(version, decision.first_name)
        phone = normalize_phone(decision.phone)
        sms_id = None
        if self.dry_run:
            log.info("[DRY RUN] would SCHEDULE %s (%s) %s for %s, version %s", decision.lead_name, lead_id, phone, self._local(slot, zones), version)
        else:
            try:
                sms = await self.close.create_sms({
                    "lead_id": lead_id,
                    "contact_id": decision.contact_id,
                    "local_phone": self.s.local_phone,
                    "remote_phone": phone,
                    "text": text,
                    "status": "scheduled",
                    "date_scheduled": iso(slot),
                })
            except CloseError as exc:
                self._event("send_failed", lead_id, detail=f"schedule: {exc}")
                await self._mark_error(lead_id, f"could not schedule SMS: {exc.message}", decision=decision,
                                       zones=zones, version=version, text=text, source=source)
                return
            sms_id = sms.get("id")
        self.db.upsert(
            lead_id,
            status="scheduled",
            **self._lead_fields(decision, zones, source),
            message_version=version,
            message_text=text,
            sms_activity_id=sms_id,
            scheduled_for=iso(slot),
            verify_after=iso(slot + timedelta(seconds=self.s.error_check_delay_seconds)),
            verified_at=None,
            verify_attempts=0,
            morning_checked_at=None,
            skip_rule=None,
            skip_reason=None,
            last_checked_at=iso(now),
        )
        self._event("scheduled", lead_id, detail=f"v{version} for {self._local(slot, zones)} sms={sms_id}")
        if not self.dry_run:
            log.info("SCHEDULED %s (%s) %s for %s, version %s, sms=%s", decision.lead_name, lead_id, phone, self._local(slot, zones), version, sms_id)

    async def _skip(self, lead_id: str, decision: Decision, source: str, stage: str = "check") -> None:
        now = iso(self.clock())
        rec = self.db.get(lead_id)
        changed = (
            rec is None
            or rec["status"] != "skipped"
            or rec["skip_rule"] != decision.rule
            or rec["skip_reason"] != decision.reason
        )
        fields = {"status": "skipped", "skip_rule": decision.rule, "skip_reason": decision.reason, "last_checked_at": now}
        if decision.lead_name:
            fields["lead_name"] = decision.lead_name
        if decision.phone:
            fields["phone"] = normalize_phone(decision.phone)
        if rec is None:
            fields["source"] = source
        self.db.upsert(lead_id, **fields)
        if changed:
            self._event("skipped", lead_id, rule=decision.rule, detail=f"{stage}: {decision.reason}")
            log.info("SKIP %s (%s) rule %s [%s]: %s", decision.lead_name or lead_id, lead_id, decision.rule,
                     RULE_NAMES.get(decision.rule or "", ""), decision.reason)
        if decision.rule == "14":
            await self._duplicate_task(lead_id, decision)

    async def _duplicate_task(self, lead_id: str, decision: Decision) -> None:
        rec = self.db.get(lead_id)
        if rec and rec.get("duplicate_task_created"):
            return
        if await self._task(lead_id, f"Possible duplicate of {decision.duplicate_of}, check before texting."):
            self.db.upsert(lead_id, duplicate_task_created=1)

    # ----------------------------------------------------------------- sending
    def send_wait_seconds(self) -> float:
        """Seconds until the next send is allowed (15s gap, 60 per rolling hour)."""
        now = self.clock()
        times = sorted(parse_dt(t) for t in self.db.send_event_times_since(iso(now - timedelta(hours=1))))
        if not times:
            return 0.0
        wait = (times[-1] + timedelta(seconds=self.s.min_seconds_between_sends) - now).total_seconds()
        if len(times) >= self.s.max_sends_per_hour:
            oldest_to_expire = times[len(times) - self.s.max_sends_per_hour]
            wait = max(wait, (oldest_to_expire + timedelta(hours=1) - now).total_seconds())
        return max(wait, 0.0)

    async def sender_tick(self) -> bool:
        if self.stopped():
            return False
        rec = self.db.next_queued()
        if rec is None or self.send_wait_seconds() > 0:
            return False
        await self._send_one(rec["lead_id"])
        return True

    async def _send_one(self, lead_id: str) -> None:
        async with self._locks[lead_id]:
            rec = self.db.get(lead_id)
            if not rec or rec["status"] != "queued":
                return
            now = self.clock()
            source = rec.get("source")
            try:
                in_view = await self.close.lead_in_view(self.s.smart_view_id, lead_id)
                decision = await self.full_check(lead_id) if in_view else Decision(
                    False, "view", f"no longer in the {self.s.smart_view_name} smart view",
                    lead_id=lead_id, lead_name=rec.get("lead_name") or "")
            except CloseError as exc:
                attempts = (rec.get("attempts") or 0) + 1
                log.error("Pre-send check failed for %s (attempt %s): %s", lead_id, attempts, exc)
                self._event("check_failed", lead_id, detail=f"pre-send: {exc}")
                if attempts >= 5:
                    self.db.upsert(lead_id, status="skipped", skip_rule="api", attempts=attempts,
                                   skip_reason=f"pre-send check failed {attempts} times: {exc}", last_checked_at=iso(now))
                    self._event("skipped", lead_id, rule="api", detail=str(exc))
                else:
                    self.db.upsert(lead_id, attempts=attempts, queued_at=iso(now))
                return
            if not decision.eligible:
                await self._skip(lead_id, decision, source or "queue", stage="pre-send")
                return

            version = rec.get("message_version") or self.rng.choice(VERSIONS)
            zones = zones_for_phone(decision.phone, self.s.default_timezone)
            if not in_send_window(now, zones, self.s.send_window_start_hour, self.s.send_window_end_hour):
                log.info("Send window closed for %s while it was queued; scheduling for the morning instead", lead_id)
                await self._schedule(lead_id, decision, zones, source, version)
                return

            text = render(version, decision.first_name)
            phone = normalize_phone(decision.phone)
            fields = self._lead_fields(decision, zones, source)
            if self.dry_run:
                self.db.upsert(lead_id, status="sent", **fields, message_version=version, message_text=text,
                               sent_at=iso(now), verified_at=iso(now), note_created=1)
                self._event("sent", lead_id, detail=f"[dry run] v{version} to {phone}")
                log.info("[DRY RUN] would SEND %s (%s) %s version %s: %s", decision.lead_name, lead_id, phone, version, text)
                return
            try:
                sms = await self.close.create_sms({
                    "lead_id": lead_id,
                    "contact_id": decision.contact_id,
                    "local_phone": self.s.local_phone,
                    "remote_phone": phone,
                    "text": text,
                    "status": "outbox",
                })
            except CloseError as exc:
                self._event("send_failed", lead_id, detail=str(exc))
                await self._mark_error(lead_id, exc.message, decision=decision, zones=zones,
                                       version=version, text=text, source=source)
                return
            sms_id = sms.get("id")
            self.db.upsert(
                lead_id, status="sent", **fields, message_version=version, message_text=text,
                sms_activity_id=sms_id, sent_at=iso(now), verified_at=None, verify_attempts=0,
                verify_after=iso(now + timedelta(seconds=self.s.error_check_delay_seconds)),
            )
            self._event("sent", lead_id, detail=f"v{version} to {phone} sms={sms_id}")
            log.info("SENT %s (%s) %s version %s sms=%s", decision.lead_name, lead_id, phone, version, sms_id)
            await self._sent_note(lead_id, version)
            if (sms.get("status") or "").lower() == "error":
                await self._mark_error(lead_id, sms.get("error_message") or "error right after sending")

    async def _sent_note(self, lead_id: str, version: int) -> None:
        rec = self.db.get(lead_id)
        if rec and rec.get("note_created"):
            return
        await self._note(lead_id, f"First text sent: Version {version}")
        self.db.upsert(lead_id, note_created=1)

    async def _scheduled_went_out(self, rec: dict, *, verified: bool) -> None:
        now = self.clock()
        self.db.upsert(
            rec["lead_id"], status="sent", sent_at=iso(now),
            verified_at=iso(now) if verified else None,
            verify_after=iso(now + timedelta(seconds=self.s.error_check_delay_seconds)),
            verify_attempts=0,
        )
        self._event("sent", rec["lead_id"], detail=f"scheduled v{rec.get('message_version')} went out sms={rec.get('sms_activity_id')}")
        log.info("SENT (scheduled) %s (%s) version %s", rec.get("lead_name"), rec["lead_id"], rec.get("message_version"))
        await self._sent_note(rec["lead_id"], rec.get("message_version"))

    # ------------------------------------------------------------------ errors
    async def _mark_error(self, lead_id: str, message: str, *, decision: Decision | None = None, zones=None,
                          version=None, text=None, source=None) -> None:
        rec = self.db.get(lead_id)
        if rec and rec["status"] == "error":
            return
        now = self.clock()
        fields = {"status": "error", "error_message": message, "error_at": iso(now), "verified_at": iso(now)}
        if decision is not None:
            fields.update(self._lead_fields(decision, zones or [CENTRAL], source))
        if version is not None:
            fields["message_version"] = version
        if text is not None:
            fields["message_text"] = text
        self.db.upsert(lead_id, **fields)
        rec = self.db.get(lead_id)
        self._event("error", lead_id, detail=f"phone={rec.get('phone')} error={message}")
        log.error("TEXT ERROR %s (%s) phone=%s: %s", rec.get("lead_name"), lead_id, rec.get("phone"), message)
        if not rec.get("error_task_created") and await self._task(lead_id, f"First text failed: {message}"):
            self.db.upsert(lead_id, error_task_created=1)
        await self._check_error_spike(now, message)

    async def _check_error_spike(self, now: datetime, last_message: str) -> None:
        count = self.db.errors_since(iso(now - timedelta(hours=1)))
        if count < self.s.error_stop_threshold or self.dry_run or self.s.stop_file.exists():
            return
        self.s.stop_file.write_text(
            f"Created automatically at {iso(now)}: {count} texts errored within one hour.\n"
            f"Last error: {last_message}\nDelete this file (or send /resume to the bot) to resume.\n"
        )
        pending = len(self.db.rows("scheduled"))
        log.critical("STOP file created: %s texts errored within one hour", count)
        self._event("auto_stop", detail=f"{count} errors in 1h; last: {last_message}")
        await self.telegram.send(
            f"🛑 Close outbound texting STOPPED\n\n{count} first texts errored within the last hour.\n"
            f"Last error: {last_message}\n\nNothing new will be sent or scheduled until you fix the cause and "
            f"send /resume (or delete the STOP file).\n"
            + (f"{pending} texts are still scheduled in Close; run `outbound cancel-scheduled --yes` on the "
               f"server if you want those removed too." if pending else "")
        )

    # ------------------------------------------------- 2-minute backup check
    async def verify_tick(self) -> None:
        now = self.clock()
        for rec in self.db.due_for_verification(iso(now)):
            lead_id = rec["lead_id"]
            async with self._locks[lead_id]:
                rec = self.db.get(lead_id)
                if rec["status"] not in ("sent", "scheduled") or rec.get("verified_at"):
                    continue
                try:
                    sms = await self.close.get_sms(rec["sms_activity_id"])
                except CloseError as exc:
                    log.warning("Backup check for %s failed: %s", lead_id, exc)
                    self.db.upsert(lead_id, verify_after=iso(now + timedelta(seconds=60)))
                    continue
                if sms is None:
                    if rec["status"] == "scheduled":
                        self.db.upsert(lead_id, status="skipped", skip_rule="deleted", verified_at=iso(now),
                                       skip_reason="scheduled SMS was deleted in Close before it went out")
                        self._event("unscheduled", lead_id, rule="deleted", detail="SMS deleted in Close")
                        log.warning("Scheduled SMS for %s was deleted in Close", lead_id)
                    else:
                        self.db.upsert(lead_id, verified_at=iso(now))
                        log.warning("Sent SMS %s for %s no longer exists in Close", rec["sms_activity_id"], lead_id)
                    continue
                status = (sms.get("status") or "").lower()
                if status == "error":
                    await self._mark_error(lead_id, sms.get("error_message") or "unknown error (Close gave no message)")
                elif status in ("outbox", "scheduled", "draft"):
                    attempts = (rec.get("verify_attempts") or 0) + 1
                    if attempts >= 10:
                        self.db.upsert(lead_id, verified_at=iso(now), verify_attempts=attempts)
                        self._event("verify_gave_up", lead_id, detail=f"still {status} after {attempts} checks")
                        log.warning("SMS for %s still %s after %s checks; giving up the backup check", lead_id, status, attempts)
                    else:
                        self.db.upsert(lead_id, verify_attempts=attempts, verify_after=iso(now + timedelta(seconds=60)))
                elif rec["status"] == "scheduled":
                    await self._scheduled_went_out(rec, verified=True)
                else:
                    self.db.upsert(lead_id, verified_at=iso(now))

    # ------------------------------------------------------------ morning check
    async def morning_tick(self) -> None:
        """5 minutes before each scheduled text (7:55 Central, or 7:55 local in the West)."""
        now = self.clock()
        cutoff = now + timedelta(minutes=self.s.morning_check_minutes_before)
        for rec in self.db.due_for_morning_check(iso(cutoff)):
            lead_id = rec["lead_id"]
            scheduled_for = parse_dt(rec["scheduled_for"])
            if scheduled_for and now >= scheduled_for:
                self.db.upsert(lead_id, morning_checked_at=iso(now))
                self._event("morning_missed", lead_id, detail="service was not running at check time")
                log.warning("Missed the morning check for %s (scheduled %s)", lead_id, rec["scheduled_for"])
                continue
            async with self._locks[lead_id]:
                rec = self.db.get(lead_id)
                if rec["status"] != "scheduled" or rec.get("morning_checked_at"):
                    continue
                own = frozenset({rec["sms_activity_id"]}) if rec.get("sms_activity_id") else frozenset()
                try:
                    in_view = await self.close.lead_in_view(self.s.smart_view_id, lead_id)
                    decision = await self.full_check(lead_id, ignore_sms_ids=own) if in_view else Decision(
                        False, "view", f"no longer in the {self.s.smart_view_name} smart view",
                        lead_id=lead_id, lead_name=rec.get("lead_name") or "")
                except CloseError as exc:
                    log.error("Morning check failed for %s (will retry): %s", lead_id, exc)
                    continue
                if decision.eligible:
                    self.db.upsert(lead_id, morning_checked_at=iso(now))
                    self._event("morning_ok", lead_id)
                    log.info("Morning check passed for %s (%s)", rec.get("lead_name"), lead_id)
                    continue
                if not self.dry_run and rec.get("sms_activity_id"):
                    try:
                        await self.close.delete_sms(rec["sms_activity_id"])
                    except CloseError as exc:
                        log.error("Could not delete scheduled SMS for %s (will retry): %s", lead_id, exc)
                        continue
                self.db.upsert(lead_id, status="skipped", skip_rule=decision.rule, morning_checked_at=iso(now),
                               skip_reason=f"morning check: {decision.reason}", last_checked_at=iso(now))
                self._event("unscheduled", lead_id, rule=decision.rule, detail=decision.reason)
                log.info("UNSCHEDULED %s (%s) rule %s: %s", rec.get("lead_name"), lead_id, decision.rule, decision.reason)
                if decision.rule == "14":
                    await self._duplicate_task(lead_id, decision)

    # ------------------------------------------------------------ backup sweep
    async def sweep(self) -> int:
        if self.stopped():
            log.info("STOP file present: sweep paused")
            return 0
        ids = await self.close.view_lead_ids(self.s.smart_view_id)
        recheck = timedelta(minutes=self.s.recheck_skipped_minutes)
        checked = 0
        for lead_id in ids:
            if self.stopped():
                break
            if lead_id in self._pending:
                continue
            rec = self.db.get(lead_id)
            if rec:
                if rec["status"] in SETTLED_STATUSES:
                    continue
                last = parse_dt(rec.get("last_checked_at"))
                if last and self.clock() - last < recheck:
                    continue
            await self.process_lead(lead_id, "sweep", check_view=False)
            checked += 1
        log.info("Sweep: %s leads in %s, %s checked", len(ids), self.s.smart_view_name, checked)
        return checked

    # ------------------------------------------------------------- reporting
    async def weekly_tick(self) -> None:
        from .report import build_report

        now = self.clock()
        local = now.astimezone(CENTRAL)
        if local.weekday() != 0 or local.hour < 9:
            return
        week = local.strftime("%G-W%V")
        if self.db.kv_get("weekly_report_week") == week:
            return
        if self._weekly_next_try and now < self._weekly_next_try:
            return
        text = await build_report(self, now)
        if await self.telegram.send(text):
            self.db.kv_set("weekly_report_week", week)
        else:
            self._weekly_next_try = now + timedelta(minutes=15)

    def status_text(self) -> str:
        counts = self.db.status_counts()
        now = self.clock()
        day_ago = iso(now - timedelta(hours=24))
        last_sent = self.db.last_event("sent")
        state = "🛑 STOPPED (STOP file present)" if self.s.stop_file.exists() else "✅ RUNNING"
        lines = [
            f"{state}{' — DRY RUN' if self.dry_run else ''}"
            + (f" — TEST MODE (only {', '.join(self.s.test_phones)})" if self.s.test_phones else ""),
            f"Queued now: {counts.get('queued', 0)} | Scheduled: {counts.get('scheduled', 0)}",
            f"Sent last 24h: {self.db.count_events_since('sent', day_ago)} | Errors last 24h: {self.db.count_events_since('error', day_ago)}",
            "All time: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "All time: nothing yet",
        ]
        if last_sent:
            lines.append(f"Last text: {parse_dt(last_sent['ts']).astimezone(CENTRAL).strftime('%a %b %d %I:%M %p %Z')}")
        return "\n".join(lines)
