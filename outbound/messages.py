"""The three first-text versions."""
from __future__ import annotations

VERSIONS = (1, 2, 3)

# (greeting, body). With a first name: "Hey Sam! <body>". Without: "Hey! <body>".
TEMPLATES = {
    1: (
        "Hey",
        "My name is Jarluen. I'm one of the advisors on Daeron Myers' team. Saw you opted in for the "
        "courier business. Were you interested in starting your courier business, or do you already "
        "have a courier business you want to scale?",
    ),
    2: (
        "Hey",
        "This is Jarluen, one of the advisors on Daeron Myers' team. I saw you opted in for the courier "
        "business. Are you looking to start your courier business, or do you already have one you want "
        "to scale?",
    ),
    3: (
        "Hi",
        "Jarluen here, I'm an advisor on Daeron Myers' team. Noticed you opted in about the courier "
        "business. Are you wanting to start one from scratch, or grow a courier business you already have?",
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
