"""The first-text versions."""
from __future__ import annotations

# Version 3 is defined below but dropped from rotation for now — it failed
# carrier content filtering consistently (6 for 6) while 1 and 2 stayed clean.
VERSIONS = (1, 2)
# New sends only ever start on PRIMARY_VERSIONS. Version 1 (the exact
# qualifying script) got flagged ~79% of the time (22 of 28) once live, while
# version 2 (same question, different wording) passed 12/12 — so new leads
# lead with 2, and 1 is now fallback-only: still tried automatically if 2
# somehow gets flagged too, just never the first attempt.
PRIMARY_VERSIONS = (2,)

# Question 1 of the real outbound qualifying script (no webinar follow-up —
# straight into qualifying, per how this actually runs). Version 1 is the
# script verbatim; version 2 says the same thing differently.
# (greeting, body). With a first name: "Hey Sam! <body>". Without: "Hey! <body>".
TEMPLATES = {
    1: (
        "Hey",
        "My name is Jarluen. I'm one of the advisors on Daeron Myers' team. Saw you opted in for the "
        "courier business. Were you interested in starting your courier business, or do you already "
        "have a courier business you want to scale?",
    ),
    2: (
        "Hi",
        "Jarluen here, advisor on Daeron Myers' team. You opted in about the courier business recently "
        "- curious if you're starting from scratch, or already running one you'd like to grow?",
    ),
}

_NOT_A_NAME = {"unknown", "lead", "test", "none", "null", "na", "customer", "mr", "mrs", "ms", "dr", "sir"}


def clean_first_name(full_name: str | None) -> str | None:
    """First word of the contact name, if it looks like a real first name."""
    if not full_name:
        return None
    token = full_name.strip().split()[0].strip(",.;:") if full_name.strip() else ""
    letters = token.replace("-", "").replace("'", "")
    if len(letters) < 2 or not letters.isalpha() or token.casefold().strip(".") in _NOT_A_NAME:
        return None
    if token.isupper() or token.islower():
        token = token[0].upper() + token[1:].lower()
    return token


def render(version: int, first_name: str | None) -> str:
    greeting, body = TEMPLATES[version]
    if first_name:
        return f"{greeting} {first_name}! {body}"
    return f"{greeting}! {body}"
