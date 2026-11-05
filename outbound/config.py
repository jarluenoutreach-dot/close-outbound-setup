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
