"""Follow-up text sequences: the booked-call nurture and the Strategy Session prep texts.

Each sequence watches one private smart view. A lead that shows up in the view is enrolled once
(ever) and gets the steps below, sent from LOCAL_PHONE as the API key's owner. Every step is
re-checked right before it goes out; see SequenceRunner.stop_reason for what ends a run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

# ---- Close IDs (Sales pipeline) ------------------------------------------------
TC_BOOKED = "stat_KzzYLToP5c1mXvgslQqArqo7FYe1yZEvV3AErIhhea1"
TC_COMPLETED = "stat_tB6T6WtDAJkuiuj6YGfOli0PfjSuxhUREMDBuM4DAVe"
SS_BOOKED = "stat_sfJxlhINTVxVEl8EK3dQbXJOs8gzt6RTaCaJ8gL3CrX"
SS_COMPLETED = "stat_oHqiKM3HcBdGuiXgUK34Gqsb5NuKG98L1V0DUZmP3Gz"

# Lead custom field "Setter Lead Owner" (a user field).
SETTER_FIELD = "cf_bIpeW66wo36K6ny1zEe3pQ7tQnxOmiSsU2GwDmchXWv"

DEFAULT_NURTURE_VIEW_ID = "save_vifT1TIiKfAgpoDLSfUumF4OJOisTuUvq42XtI3RY8x"  # Jarluen – TC Completed Nurture (SMS)
DEFAULT_PREP_VIEW_ID = "save_lOa7Gam9Abxs5jM3QNr9VEGwvETDtaOHxdbuxtcjmH0"  # Jarluen – Booked Calls (prep texts)

# Views the sequences watched before a view change. Switching to a new view leaves everyone
# already in it alone, the same as turning a sequence on for the first time.
LEGACY_VIEW_IDS = {
    "nurture": "save_YJX4M0AKp11oDsjeP1oUVSMazgR3nL4bVIE3R2jaVk0",
    "prep": "save_0M5Nb1OnPVBJWZWibZrhCZXeIYgw3PoKCVMLeMAMXHz",  # Jarluen – SS Booked (prep texts)
}

SLIDES_URL = "https://gamma.app/docs/Diamond-Program-qm9r81lcp22mjbd?mode=doc"

# ---- Messages: (with first name, without first name) ---------------------------
MESSAGES: dict[str, tuple[str, str]] = {
    "intro": (
        "Hey {name}, it's Jarluen from Daeron's team. Great connecting! Happy to answer any questions you "
        "have about starting a courier business.",
        "Hey, it's Jarluen from Daeron's team. Great connecting! Happy to answer any questions you have "
        "about starting a courier business.",
    ),
    "nudge": (
        "Hey {name}, just making sure you saw my text yesterday. Any questions I can answer about the "
        "courier business?",
        "Hey, just making sure you saw my text yesterday. Any questions I can answer about the courier "
        "business?",
    ),
    "fedex": (
        "One of our students went from FedEx driver to $10k/month. Here's how: https://youtu.be/MhFYz5KjOBQ "
        "Let me know what you think!",
    ) * 2,
    "question": (
        "Quick question, {name}: what's the #1 thing holding you back right now? Time, money, or not "
        "knowing where to start?",
        "Quick question: what's the #1 thing holding you back right now? Time, money, or not knowing "
        "where to start?",
    ),
    "diamond": (
        "Here's our Diamond Program breakdown: https://gamma.app/docs/Diamond-Program-qm9r81lcp22mjbd?mode=doc "
        "We'll find your path on your Strategy Session.",
    ) * 2,
    "daeron": (
        "This one's a must-watch. Daeron shows how he made $65k in 3 months subcontracting with just his "
        "car: https://www.youtube.com/watch?v=wHtJPqYmBdU",
    ) * 2,
    "calendar": (
        "{name}, is there anything about the courier business you'd like me to explain? I can send the "
        "free resources again if that helps.",
        "Is there anything about the courier business you'd like me to explain? I can send the free "
        "resources again if that helps.",
    ),
    "breakup": (
        "Hey {name}, I don't want to keep bugging you. Should I close out your file for now, or are you "
        "still serious about building your courier business?",
        "Hey, I don't want to keep bugging you. Should I close out your file for now, or are you still "
        "serious about building your courier business?",
    ),
    "slides": (
        "You're all set for your Strategy Session, {name}! Please go through these slides before the "
        f"call: {SLIDES_URL}",
        f"You're all set for your Strategy Session! Please go through these slides before the call: {SLIDES_URL}",
    ),
    "slides_check": (
        "Quick check, {name}: did you get a chance to go through the slides? Jot down any questions so we "
        "can cover them on your call.",
        "Quick check: did you get a chance to go through the slides? Jot down any questions so we can "
        "cover them on your call.",
    ),
}


def render(key: str, first_name: str | None) -> str:
    with_name, without = MESSAGES[key]
    return with_name.format(name=first_name) if first_name else without


@dataclass(frozen=True)
class Step:
    message: str
    delay: timedelta  # after the previous step went out (or after enrolling, for step 1)


DAY = timedelta(days=1)

# Day 0 (on enroll), 1, 2, 4, 6, 8, 11, 15.
NURTURE_STEPS = (
    Step("intro", timedelta(0)),
    Step("nudge", DAY),
    Step("fedex", DAY),
    Step("question", 2 * DAY),
    Step("diamond", 2 * DAY),
    Step("daeron", 2 * DAY),
    Step("calendar", 3 * DAY),
    Step("breakup", 4 * DAY),
)

# Meeting 2+ days out: slides now, check-in a day later. Sooner: slides only.
PREP_LONG_STEPS = (Step("slides", timedelta(0)), Step("slides_check", DAY))
PREP_SHORT_STEPS = (Step("slides", timedelta(0)),)

NURTURE = "nurture"
PREP = "prep"

VARIANT_STEPS = {
    "nurture": NURTURE_STEPS,
    "prep_long": PREP_LONG_STEPS,
    "prep_short": PREP_SHORT_STEPS,
}

SEQUENCE_LABELS = {NURTURE: "Booked nurture", PREP: "SS prep"}
