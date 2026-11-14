"""The human-in-the-loop follow-up sequence: every message here is sent
verbatim, chosen by a human tapping a Telegram button after reading the
lead's actual reply. Nothing here is AI-generated or adapted automatically —
the button only ever sends the exact text below.

Each step defines the buttons to offer *after* a reply at that step, where
each button is (label, next_step, message_to_send).
"""
from __future__ import annotations

MSG_2 = "Cool, what state are you from and what do you do for work?"
MSG_3 = "Awesome and what made you interested in starting your own courier business?"
MSG_4_POSITIVE = (
    "Love that. Are you looking for coaching and mentorship to help you get started, "
    "or are you planning to figure things out on your own?"
)
MSG_4_EMPATHETIC = (
    "I'm sorry to hear that. Are you looking for coaching and mentorship to help you get started, "
    "or are you planning to figure things out on your own?"
)
PATH_A = (
    "Hey, are you free for a quick 5-10 min call right now so we can talk more about your "
    "situation and your goals with the courier business?"
)
PATH_B = (
    "Totally makes sense, a lot of people like to start that way. We have a self-paced academy "
    "that walks you through starting and running your courier business step by step, at your own "
    "pace. Want me to send you the details?"
)
PATH_C = (
    "Here's some free resources that can help you. Check out these videos and let me know what "
    "you think.\nYouTube:\nhttps://youtube.com/@daeronmyers?si=zxukFRuV8FCreEwg\n"
    "What is a courier business (1 min)\nhttps://youtube.com/shorts/oKJuLAM_QuY?si=wiHHR1zp0W-3OORt\n"
    "Intro to the courier business\nhttps://youtu.be/Ar4HGbnZuCo?si=hG3P25KJSduBZmS9\n"
    "How to level up your Courier Business (10 mins)\nhttps://www.youtube.com/watch?v=4HhQVInk8yE\n"
    "Subcontracting secrets to make $65k in 3 months with my car (1 hour)\n"
    "https://www.youtube.com/watch?v=wHtJPqYmBdU\nHere is also a free training:\n"
    "https://event.j2webinarjam.com/go/replay/2/3ymx2ini29qb42q8c5\nIf you feel like this is a good "
    "fit for you, let me know and we can coordinate a 1 on 1 call."
)

# step -> [(button_label, next_step, message_text), ...]
# "opener" is implicit: the first text already went out before this sequence starts.
STEPS: dict[str, list[tuple[str, str, str]]] = {
    "opener": [("Msg 2", "msg2", MSG_2)],
    "msg2": [("Msg 3", "msg3", MSG_3)],
    "msg3": [
        ("Msg 4 (positive)", "msg4", MSG_4_POSITIVE),
        ("Msg 4 (empathetic)", "msg4", MSG_4_EMPATHETIC),
    ],
    "msg4": [
        ("Path A: Coaching", "done", PATH_A),
        ("Path B: Self-paced", "done", PATH_B),
        ("Path C: Not ready", "done", PATH_C),
    ],
}

STEP_ORDER = ["opener", "msg2", "msg3", "msg4", "done"]


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


# Exact pre-written text -> the step reached once that text has gone out.
# Lets us notice a human sent the next script line directly in Close
# (bypassing the Telegram buttons) and catch followup_step back up to match,
# instead of leaving it pointing at a step the conversation already passed.
_TEXT_TO_STEP = {
    _norm(MSG_2): "msg2",
    _norm(MSG_3): "msg3",
    _norm(MSG_4_POSITIVE): "msg4",
    _norm(MSG_4_EMPATHETIC): "msg4",
    _norm(PATH_A): "done",
    _norm(PATH_B): "done",
    _norm(PATH_C): "done",
}


def sync_step(current_step: str, outbound_texts: list[str]) -> str:
    """Return the furthest step reached, given what's already been sent."""
    best = current_step if current_step in STEP_ORDER else "opener"
    best_rank = STEP_ORDER.index(best)
    for text in outbound_texts:
        step = _TEXT_TO_STEP.get(_norm(text))
        if step and STEP_ORDER.index(step) > best_rank:
            best, best_rank = step, STEP_ORDER.index(step)
    return best
