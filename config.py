import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


load_dotenv()


def build_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL", "").strip()

    if not explicit_url:
        path = Path(
            os.getenv(
                "DATABASE_PATH",
                str(Path(__file__).resolve().parent / "data" / "schedule.db"),
            )
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    if explicit_url.startswith("postgres://"):
        explicit_url = "postgresql://" + explicit_url[len("postgres://"):]

    if explicit_url.startswith("postgresql://"):
        parts = urlsplit(explicit_url)

        query = parse_qsl(parts.query, keep_blank_values=True)
        cleaned_query = []

        for key, value in query:
            if key in {"sslmode", "channel_binding"}:
                continue

            cleaned_query.append((key, value))

        cleaned = urlunsplit(
            (
                parts.scheme + "+asyncpg",
                parts.netloc,
                parts.path,
                urlencode(cleaned_query),
                parts.fragment,
            )
        )

        return cleaned

    if explicit_url.startswith("sqlite:///"):
        return explicit_url.replace(
            "sqlite:///",
            "sqlite+aiosqlite:///",
            1,
        )

    return explicit_url


@dataclass(frozen=True)
class Settings:
    bot_token: str
    timezone: str
    database_url: str
    base_url: str
    webhook_path: str
    webhook_secret: str
    port: int
    sync_interval_hours: int
    admin_user_ids: frozenset[int]


def build_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()

    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Put it into .env locally or "
            "Environment Variables on Render."
        )

    admin_user_ids_raw = os.getenv("ADMIN_USER_IDS", "").strip()

    admin_user_ids = frozenset(
        int(user_id.strip())
        for user_id in admin_user_ids_raw.split(",")
        if user_id.strip()
    )

    return Settings(
        bot_token=bot_token,
        timezone=os.getenv("TIMEZONE", "Asia/Barnaul").strip(),
        database_url=build_database_url(),
        base_url=(
            os.getenv("RENDER_EXTERNAL_URL", "").strip()
            or os.getenv("BASE_URL", "").strip()
        ),
        webhook_path=os.getenv(
            "WEBHOOK_PATH",
            "/telegram/webhook",
        ).strip(),
        webhook_secret=os.getenv(
            "WEBHOOK_SECRET",
            "change-me",
        ).strip(),
        port=int(os.getenv("PORT", "10000")),
        sync_interval_hours=int(
            os.getenv("SYNC_INTERVAL_HOURS", "6")
        ),
        admin_user_ids=admin_user_ids,
    )


settings = build_settings()