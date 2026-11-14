"""In-memory stand-ins for Close and Telegram used by the tests."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from outbound.close_api import CloseError
from outbound.config import DEFAULT_LOCAL_PHONE, Settings
from outbound.eligibility import lead_phones, normalize_phone

ME = "user_me"
OTHER = "user_other"


class FakeClose:
    def __init__(self, clock=None, local_phone=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.local_phone = local_phone or DEFAULT_LOCAL_PHONE
        self.leads: dict[str, dict] = {}
        self.view: set[str] = set()
        self.activities: dict[tuple[str, str], list[dict]] = {}
        self.sms: dict[str, dict] = {}
        self.created_sms: list[dict] = []
        self.deleted_sms: list[str] = []
        self.notes: list[tuple[str, str]] = []
        self.tasks: list[tuple[str, str]] = []
        self.last_lookback_minutes: int | None = None
        self.fail_create_sms: str | None = None
        self.calls = 0
        self._ids = itertools.count(1)
        self.opps: dict[str, list[dict]] = {}
        self.views: dict[str, set[str]] = {}

    # helpers for tests
    def add_lead(self, lead_id, name="Sam Smith", phone="+13125550100", status="Potential", in_view=True, contact_name=None):
        contacts = []
        if phone is not None:
            contacts.append({"id": f"cont_{lead_id}", "name": contact_name if contact_name is not None else name,
                             "phones": [{"phone": phone, "type": "mobile"}]})
        self.leads[lead_id] = {"id": lead_id, "display_name": name, "status_label": status, "contacts": contacts}
        if in_view:
            self.view.add(lead_id)
        return self.leads[lead_id]

    def add_activity(self, kind, lead_id, **fields):
        fields.setdefault("id", f"{kind}_{next(self._ids)}")
        if kind == "sms":
            fields.setdefault("local_phone", self.local_phone)
        self.activities.setdefault((kind, lead_id), []).append(fields)
        if kind == "sms":
            self.sms[fields["id"]] = fields
        return fields

    # CloseClient interface
    async def get_lead(self, lead_id):
        self.calls += 1
        return self.leads.get(lead_id)

    async def list_activities(self, kind, lead_id):
        self.calls += 1
        return list(self.activities.get((kind, lead_id), []))

    async def get_lead_full(self, lead_id):
        self.calls += 1
        return self.leads.get(lead_id)

    async def list_opportunities(self, lead_id):
        return list(self.opps.get(lead_id, []))

    def add_opp(self, lead_id, status_id, user_id=ME):
        self.opps.setdefault(lead_id, []).append({"id": f"oppo_{next(self._ids)}", "lead_id": lead_id,
                                                  "status_id": status_id, "user_id": user_id})

    async def lead_in_view(self, view_id, lead_id):
        self.calls += 1
        return lead_id in self.view

    async def view_lead_ids(self, view_id):
        if view_id in self.views:
            return sorted(self.views[view_id])
        return sorted(self.view)

    async def recent_candidate_ids_fast(self, lookback_minutes=15, limit=50):
        # The fake has no notion of Close's search-index lag or a separate
        # fast path — it's a plain in-memory model, so this is equivalent to
        # view_lead_ids for test purposes. Records the lookback it was asked
        # for so tests can confirm the engine widens it after a pause.
        self.last_lookback_minutes = lookback_minutes
        return sorted(self.view)

    async def find_lead_ids_by_phone(self, phone):
        return [lid for lid, lead in self.leads.items() if normalize_phone(phone) in lead_phones(lead)]

    async def create_sms(self, payload):
        if self.fail_create_sms:
            raise CloseError(400, self.fail_create_sms)
        sms = {**payload, "id": f"acti_{next(self._ids)}", "direction": "outbound", "user_id": ME,
               "date_created": self.clock().isoformat()}
        self.created_sms.append(sms)
        self.sms[sms["id"]] = sms
        self.activities.setdefault(("sms", payload["lead_id"]), []).append(sms)
        return dict(sms)

    async def get_sms(self, sms_id):
        return dict(self.sms[sms_id]) if sms_id in self.sms else None

    async def delete_sms(self, sms_id):
        self.deleted_sms.append(sms_id)
        sms = self.sms.pop(sms_id, None)
        if sms:
            self.activities[("sms", sms["lead_id"])].remove(sms)

    async def create_note(self, lead_id, text):
        self.notes.append((lead_id, text))

    async def create_task(self, lead_id, text, *, assigned_to, date):
        assert assigned_to == ME
        self.tasks.append((lead_id, text))


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.sent_with_buttons: list[tuple[str, list[tuple[str, str]]]] = []
        self.token = "fake-token"
        self.chat_id = "1"

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    async def send(self, text, chat_id=None):
        self.sent.append(text)
        return True

    async def send_with_buttons(self, text, buttons, chat_id=None):
        self.sent_with_buttons.append((text, buttons))
        return True


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(close_api_key="test", my_user_id=ME, smart_view_id="save_test", project_dir=tmp_path,
                    telegram_bot_token="", **overrides)
