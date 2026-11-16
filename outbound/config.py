"""Settings. Secrets come only from the .env file (or the environment), never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

# Non-secret IDs from the spec. Each can be overridden in .env.
DEFAULT_MY_USER_ID = "user_9LGnQqtaen7jdlZ3P10lTbiVcjELWdhZw52QHGCQF2w"
DEFAULT_SMART_VIEW_ID = "save_qXqdB9NfSbAyAEkNxorj51QmKP1jMEAa9Vo9petBUmU"
DEFAULT_LOCAL_PHONE = "+19786669462"

from .sequences import DEFAULT_NURTURE_VIEW_ID, DEFAULT_PREP_VIEW_ID  # noqa: E402


@dataclass(frozen=True)
class Settings:
    # Secrets (.env only)
    close_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_signature_key: str = ""
    webhook_id: str = ""

    # Who / what / from where
    my_user_id: str = DEFAULT_MY_USER_ID
    smart_view_id: str = DEFAULT_SMART_VIEW_ID
    smart_view_name: str = "Webinar Workflow"
    local_phone: str = DEFAULT_LOCAL_PHONE
    default_timezone: str = "America/Chicago"

    # Eligibility rules
    lookback_days: int = 60
    other_setter_text_days: int = 7
    long_call_seconds: int = 120

    # Sending
    send_window_start_hour: int = 8
    send_window_end_hour: int = 20
    send_window_end_minute: int = 0
    min_seconds_between_sends: int = 15
    max_sends_per_hour: int = 60
    schedule_spacing_seconds: int = 15

    # Timing
    webhook_delay_seconds: float = 5.0
    sweep_interval_seconds: int = 300
    recheck_skipped_minutes: int = 60
    error_check_delay_seconds: int = 120
    error_stop_threshold: int = 3
    morning_check_minutes_before: int = 5

    # Test mode: when set, only leads with one of these phones are processed at all.
    test_phones: tuple[str, ...] = ()

    # Webinar no-reply nudge (outbound/nudge.py)
    nudge_enabled: bool = True
    nudge_after_hours: int = 24

    # Webinar Day 3 / Day 7 no-reply follow-ups (outbound/noreply.py), in the lead's own time zone
    noreply_enabled: bool = True
    noreply_start_hour: int = 9
    noreply_end_hour: int = 19
    noreply_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Monday=0 ... Saturday=5

    # Follow-up sequences (outbound/sequences.py). Both ON unless turned off in .env.
    nurture_enabled: bool = True
    prep_enabled: bool = True
    nurture_view_id: str = DEFAULT_NURTURE_VIEW_ID
    prep_view_id: str = DEFAULT_PREP_VIEW_ID
    seq_timezone: str = "America/Chicago"
    seq_window_start_hour: int = 9
    seq_window_end_hour: int = 18
    seq_window_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Monday=0 ... Saturday=5
    seq_sweep_interval_seconds: int = 300
    prep_long_hours: int = 48              # meeting this far out or more: slides + check-in
    prep_followup_hours_before: int = 3    # never send the check-in closer than this to the call
    prep_meeting_wait_minutes: int = 30    # wait this long for the meeting to show up in Close

    project_dir: Path = PROJECT_DIR

    @property
    def data_dir(self) -> Path:
        return self.project_dir / "data"

    @property
    def log_dir(self) -> Path:
        return self.project_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "outbound.db"

    @property
    def dry_run_db_path(self) -> Path:
        return self.data_dir / "dry_run.db"

    @property
    def stop_file(self) -> Path:
        return self.project_dir / "STOP"

    @property
    def env_file(self) -> Path:
        return self.project_dir / ".env"


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _days(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return tuple(sorted({_DAY_NAMES[d.strip()[:3]] for d in raw.split(",") if d.strip()}))


def _phones(name: str) -> tuple[str, ...]:
    from .eligibility import normalize_phone

    raw = (os.getenv(name) or "").replace(";", ",")
    return tuple(p for p in (normalize_phone(x.strip()) for x in raw.split(",") if x.strip()) if p)


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    load_dotenv(env_file, override=True)
    return Settings(
        close_api_key=_str("CLOSE_API_KEY"),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        webhook_signature_key=_str("CLOSE_WEBHOOK_SIGNATURE_KEY"),
        webhook_id=_str("CLOSE_WEBHOOK_ID"),
        my_user_id=_str("MY_USER_ID", DEFAULT_MY_USER_ID),
        smart_view_id=_str("SMART_VIEW_ID", DEFAULT_SMART_VIEW_ID),
        smart_view_name=_str("SMART_VIEW_NAME", "Webinar Workflow"),
        local_phone=_str("LOCAL_PHONE", DEFAULT_LOCAL_PHONE),
        default_timezone=_str("DEFAULT_TIMEZONE", "America/Chicago"),
        lookback_days=_int("LOOKBACK_DAYS", 60),
        other_setter_text_days=_int("OTHER_SETTER_TEXT_DAYS", 7),
        long_call_seconds=_int("LONG_CALL_SECONDS", 120),
        send_window_start_hour=_int("SEND_WINDOW_START_HOUR", 8),
        send_window_end_hour=_int("SEND_WINDOW_END_HOUR", 20),
        send_window_end_minute=_int("SEND_WINDOW_END_MINUTE", 0),
        min_seconds_between_sends=_int("MIN_SECONDS_BETWEEN_SENDS", 15),
        max_sends_per_hour=_int("MAX_SENDS_PER_HOUR", 60),
        schedule_spacing_seconds=_int("SCHEDULE_SPACING_SECONDS", 15),
        webhook_delay_seconds=_float("WEBHOOK_DELAY_SECONDS", 5.0),
        sweep_interval_seconds=_int("SWEEP_INTERVAL_SECONDS", 300),
        recheck_skipped_minutes=_int("RECHECK_SKIPPED_MINUTES", 60),
        error_check_delay_seconds=_int("ERROR_CHECK_DELAY_SECONDS", 120),
        error_stop_threshold=_int("ERROR_STOP_THRESHOLD", 3),
        morning_check_minutes_before=_int("MORNING_CHECK_MINUTES_BEFORE", 5),
        test_phones=_phones("TEST_PHONES"),
        nudge_enabled=_bool("NUDGE_ENABLED", True),
        nudge_after_hours=_int("NUDGE_AFTER_HOURS", 24),
        noreply_enabled=_bool("NOREPLY_ENABLED", True),
        noreply_start_hour=_int("NOREPLY_START_HOUR", 9),
        noreply_end_hour=_int("NOREPLY_END_HOUR", 19),
        noreply_days=_days("NOREPLY_DAYS", (0, 1, 2, 3, 4, 5)),
        nurture_enabled=_bool("NURTURE_ENABLED", True),
        prep_enabled=_bool("PREP_ENABLED", True),
        nurture_view_id=_str("NURTURE_VIEW_ID", DEFAULT_NURTURE_VIEW_ID),
        prep_view_id=_str("PREP_VIEW_ID", DEFAULT_PREP_VIEW_ID),
        seq_timezone=_str("SEQ_TIMEZONE", "America/Chicago"),
        seq_window_start_hour=_int("SEQ_WINDOW_START_HOUR", 9),
        seq_window_end_hour=_int("SEQ_WINDOW_END_HOUR", 18),
        seq_window_days=_days("SEQ_WINDOW_DAYS", (0, 1, 2, 3, 4, 5)),
        seq_sweep_interval_seconds=_int("SEQ_SWEEP_INTERVAL_SECONDS", 300),
        prep_long_hours=_int("PREP_LONG_HOURS", 48),
        prep_followup_hours_before=_int("PREP_FOLLOWUP_HOURS_BEFORE", 3),
        prep_meeting_wait_minutes=_int("PREP_MEETING_WAIT_MINUTES", 30),
        project_dir=env_file.resolve().parent,
    )


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in .env, keeping every other line as it is."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
