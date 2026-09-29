from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class ScheduleDatabase:
    def __init__(self, database_url: str):
        self.database_url = database_url

        connect_args = {}
        engine_kwargs: dict[str, Any] = {
            "pool_pre_ping": True,
        }

        if database_url.startswith("postgresql+asyncpg://"):
            # Neon gives us a libpq-style sslmode=require parameter, while
            # asyncpg expects TLS through connect_args. Remove sslmode from
            # the URL so it is not passed as an unsupported asyncpg keyword.
            original = urlsplit(database_url)
            query_parts = []
            if original.query:
                for part in original.query.split("&"):
                    if not part.lower().startswith("sslmode="):
                        query_parts.append(part)

            clean_query = "&".join(query_parts)
            database_url = original._replace(query=clean_query).geturl()
            self.database_url = database_url
            connect_args["ssl"] = "require"

            # Small, bounded pool for a single Render instance talking to Neon.
            # Recycle idle connections periodically because managed databases
            # can close long-idle connections.
            engine_kwargs.update(
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
                pool_recycle=1800,
                pool_use_lifo=True,
            )

        engine_kwargs["connect_args"] = connect_args
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            **engine_kwargs,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

    async def init(self) -> None:
        if self.database_url.startswith("sqlite+aiosqlite:///"):
            path = Path(self.database_url.replace("sqlite+aiosqlite:///", "", 1))
            path.parent.mkdir(parents=True, exist_ok=True)

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS bot_users (
                        telegram_id BIGINT PRIMARY KEY,
                        group_name TEXT NOT NULL,
                        group_key TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            )

            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS schedule_documents (
                        schedule_date TEXT NOT NULL,
                        corpus INTEGER NOT NULL,
                        source_url TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (schedule_date, corpus)
                    )
                    """
                )
            )

            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS schedule_entries (
                        entry_id TEXT PRIMARY KEY,
                        schedule_date TEXT NOT NULL,
                        corpus INTEGER NOT NULL,
                        group_name TEXT NOT NULL,
                        group_key TEXT NOT NULL,
                        pair_number INTEGER NOT NULL,
                        pair_time TEXT,
                        lesson_text TEXT NOT NULL,
                        room TEXT,
                        source_url TEXT NOT NULL
                    )
                    """
                )
            )

            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS sync_state (
                        state_key TEXT PRIMARY KEY,
                        state_value TEXT
                    )
                    """
                )
            )

            # The bot's hot query filters by date + group and orders by
            # corpus/pair. This index lets PostgreSQL satisfy both parts
            # without an extra sort.
            await conn.execute(
                text("DROP INDEX IF EXISTS idx_schedule_entries_lookup")
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_schedule_entries_date_group_order "
                    "ON schedule_entries "
                    "(schedule_date, group_key, corpus, pair_number, entry_id)"
                )
            )

            # Used when validating a group entered by a user.
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_schedule_entries_group_key "
                    "ON schedule_entries (group_key)"
                )
            )

            # Used by the date picker and by corpus cleanup during sync.
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_schedule_documents_active_date "
                    "ON schedule_documents (active, schedule_date)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_schedule_documents_corpus_date "
                    "ON schedule_documents (corpus, schedule_date)"
                )
            )

    async def get_user_group(self, telegram_id: int) -> str | None:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT group_name FROM bot_users "
                    "WHERE telegram_id = :telegram_id"
                ),
                {"telegram_id": telegram_id},
            )
            row = result.first()
            return row[0] if row else None

    async def find_group(self, group_name: str) -> str | None:
        """Return the canonical group name if it exists in saved schedule data."""
        group_key = self.group_key(group_name)

        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT group_name
                    FROM schedule_entries
                    WHERE group_key = :group_key
                    GROUP BY group_name
                    ORDER BY COUNT(*) DESC, group_name
                    LIMIT 1
                    """
                ),
                {"group_key": group_key},
            )
            row = result.first()
            return row[0] if row else None

    async def set_user_group(self, telegram_id: int, group_name: str) -> None:
        group_key = self.group_key(group_name)
        now = datetime.now(timezone.utc).isoformat()

        async with self.session_factory.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO bot_users (
                        telegram_id, group_name, group_key, updated_at
                    )
                    VALUES (
                        :telegram_id, :group_name, :group_key, :updated_at
                    )
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET
                        group_name = excluded.group_name,
                        group_key = excluded.group_key,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "telegram_id": telegram_id,
                    "group_name": group_name,
                    "group_key": group_key,
                    "updated_at": now,
                },
            )

    async def prune_before(self, cutoff: date) -> None:
        cutoff_value = cutoff.isoformat()
        async with self.session_factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM schedule_entries "
                    "WHERE schedule_date < :cutoff"
                ),
                {"cutoff": cutoff_value},
            )
            await session.execute(
                text(
                    "DELETE FROM schedule_documents "
                    "WHERE schedule_date < :cutoff"
                ),
                {"cutoff": cutoff_value},
            )

    async def get_available_dates(self) -> list[date]:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT schedule_date "
                    "FROM schedule_documents "
                    "WHERE active = 1 "
                    "ORDER BY schedule_date"
                )
            )
            values = [row[0] for row in result.fetchall()]

        return [date.fromisoformat(value) for value in values]

    async def get_schedule_for_group(
        self,
        group_name: str,
        target_date: date,
    ) -> list[dict[str, Any]]:
        key = self.group_key(group_name)

        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        schedule_date,
                        corpus,
                        group_name,
                        pair_number,
                        pair_time,
                        lesson_text,
                        room,
                        source_url
                    FROM schedule_entries
                    WHERE schedule_date = :schedule_date
                      AND group_key = :group_key
                    ORDER BY corpus, pair_number, entry_id
                    """
                ),
                {
                    "schedule_date": target_date.isoformat(),
                    "group_key": key,
                },
            )

            columns = result.keys()
            return [dict(zip(columns, row)) for row in result.fetchall()]

    async def get_document_hash(
        self,
        target_date: date,
        corpus: int,
    ) -> str | None:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT content_hash FROM schedule_documents "
                    "WHERE schedule_date = :schedule_date "
                    "AND corpus = :corpus"
                ),
                {
                    "schedule_date": target_date.isoformat(),
                    "corpus": corpus,
                },
            )
            row = result.first()
            return row[0] if row else None

    async def upsert_document(
        self,
        target_date: date,
        corpus: int,
        source_url: str,
        content_hash: str,
        fetched_at: str,
        active: bool = True,
    ) -> None:
        async with self.session_factory.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO schedule_documents (
                        schedule_date, corpus, source_url, content_hash,
                        fetched_at, last_seen_at, active
                    )
                    VALUES (
                        :schedule_date, :corpus, :source_url, :content_hash,
                        :fetched_at, :last_seen_at, :active
                    )
                    ON CONFLICT (schedule_date, corpus)
                    DO UPDATE SET
                        source_url = excluded.source_url,
                        content_hash = excluded.content_hash,
                        fetched_at = excluded.fetched_at,
                        last_seen_at = excluded.last_seen_at,
                        active = excluded.active
                    """
                ),
                {
                    "schedule_date": target_date.isoformat(),
                    "corpus": corpus,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "fetched_at": fetched_at,
                    "last_seen_at": fetched_at,
                    "active": 1 if active else 0,
                },
            )

    async def replace_entries(
        self,
        target_date: date,
        corpus: int,
        entries: list[dict[str, Any]],
    ) -> None:
        schedule_date = target_date.isoformat()

        async with self.session_factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM schedule_entries "
                    "WHERE schedule_date = :schedule_date "
                    "AND corpus = :corpus"
                ),
                {
                    "schedule_date": schedule_date,
                    "corpus": corpus,
                },
            )

            if not entries:
                return

            await session.execute(
                text(
                    """
                    INSERT INTO schedule_entries (
                        entry_id, schedule_date, corpus,
                        group_name, group_key, pair_number,
                        pair_time, lesson_text, room, source_url
                    )
                    VALUES (
                        :entry_id, :schedule_date, :corpus,
                        :group_name, :group_key, :pair_number,
                        :pair_time, :lesson_text, :room, :source_url
                    )
                    """
                ),
                entries,
            )

    async def set_active_for_corpus(
        self,
        corpus: int,
        active_dates: list[date],
    ) -> None:
        if not active_dates:
            return

        date_values = [value.isoformat() for value in active_dates]

        async with self.session_factory.begin() as session:
            # We only deactivate dates after a successful non-empty scan.
            placeholders = ", ".join(
                f":d{i}" for i in range(len(date_values))
            )
            params = {
                "corpus": corpus,
                **{f"d{i}": value for i, value in enumerate(date_values)},
            }

            await session.execute(
                text(
                    "UPDATE schedule_documents SET active = 0 "
                    "WHERE corpus = :corpus "
                    f"AND schedule_date NOT IN ({placeholders})"
                ),
                params,
            )

    async def set_sync_state(
        self,
        last_success_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        async with self.session_factory.begin() as session:
            values = {}
            if last_success_at is not None:
                values["last_success_at"] = last_success_at
            if last_error is not None:
                values["last_error"] = last_error

            for key, value in values.items():
                await session.execute(
                    text(
                        """
                        INSERT INTO sync_state (state_key, state_value)
                        VALUES (:key, :value)
                        ON CONFLICT (state_key)
                        DO UPDATE SET state_value = excluded.state_value
                        """
                    ),
                    {"key": key, "value": value},
                )

    async def get_database_stats(self) -> dict[str, int | str | None]:
        async with self.session_factory() as session:
            users = await session.execute(
                text("SELECT COUNT(*) FROM bot_users")
            )
            documents = await session.execute(
                text("SELECT COUNT(*) FROM schedule_documents")
            )
            active_documents = await session.execute(
                text(
                    "SELECT COUNT(*) FROM schedule_documents "
                    "WHERE active = 1"
                )
            )
            entries = await session.execute(
                text("SELECT COUNT(*) FROM schedule_entries")
            )
            corpora = await session.execute(
                text("SELECT COUNT(DISTINCT corpus) FROM schedule_documents")
            )
            date_range = await session.execute(
                text(
                    "SELECT MIN(schedule_date), MAX(schedule_date) "
                    "FROM schedule_documents WHERE active = 1"
                )
            )
            date_row = date_range.first()

            return {
                "users": int(users.scalar_one() or 0),
                "documents": int(documents.scalar_one() or 0),
                "active_documents": int(active_documents.scalar_one() or 0),
                "entries": int(entries.scalar_one() or 0),
                "corpora": int(corpora.scalar_one() or 0),
                "min_date": date_row[0] if date_row else None,
                "max_date": date_row[1] if date_row else None,
            }

    async def get_sync_state(self) -> dict[str, str | None]:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT state_key, state_value FROM sync_state"
                )
            )
            return {row[0]: row[1] for row in result.fetchall()}

    async def close(self) -> None:
        await self.engine.dispose()

    @staticmethod
    def group_key(value: str) -> str:
        return (
            value.strip()
            .replace("–", "-")
            .replace("—", "-")
            .replace("−", "-")
            .replace(" ", "")
            .casefold()
        )
