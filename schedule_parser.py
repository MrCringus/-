from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

from database import ScheduleDatabase


logger = logging.getLogger(__name__)

CORPUS_PAGES = {
    1: "https://altag.ru/student/schedule/rescheduling-1",
    2: "https://altag.ru/student/schedule/rescheduling-2",
    3: "https://altag.ru/student/schedule/rescheduling-3",
}

RU_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}

CALL_SCHEDULE = {
    0: {
        1: "08:50–10:20",
        2: "10:30–12:00",
        3: "12:20–13:50",
        4: "14:50–16:20",
        5: "16:40–18:10",
        6: "18:15–19:45",
    },
    1: {
        1: "08:00–09:30",
        2: "09:40–11:10",
        3: "11:30–13:00",
        4: "13:10–14:40",
        5: "14:50–16:20",
        6: "16:40–18:10",
        7: "18:15–19:45",
    },
    2: {
        1: "08:00–09:30",
        2: "09:40–11:10",
        3: "11:30–13:00",
        4: "13:10–14:40",
        5: "14:50–16:20",
        6: "16:40–18:10",
        7: "18:15–19:45",
    },
    3: {
        1: "08:00–09:30",
        2: "09:40–11:10",
        3: "11:30–13:00",
        4: "13:10–14:40",
        5: "14:50–16:20",
        6: "16:40–18:10",
        7: "18:15–19:45",
    },
    4: {
        1: "08:00–09:30",
        2: "09:40–11:10",
        3: "11:30–13:00",
        4: "13:10–14:40",
        5: "14:50–16:20",
        6: "16:40–18:10",
        7: "18:15–19:45",
    },
    5: {
        1: "08:00–09:30",
        2: "09:40–11:10",
        3: "11:30–13:00",
        4: "13:05–14:35",
        5: "14:55–16:25",
        6: "16:30–18:00",
    },
}

GROUP_RE = re.compile(
    r"^[А-ЯЁA-Z]{1,8}\s*[-–—]?\s*\d{3,5}[А-ЯЁA-Z]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Lesson:
    pair_number: int
    pair_time: str | None
    lesson_text: str
    room: str | None
    group_name: str
    group_key: str


@dataclass(frozen=True)
class SyncStats:
    corpora: int
    documents: int
    changed_documents: int
    entries: int
    failed_documents: int


class ScheduleParseError(Exception):
    pass


class ScheduleParser:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36 "
                    "ALTAG-Schedule-Bot/2.0"
                ),
                "Accept": (
                    "text/html,application/pdf,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            }
        )
        self._html_cache: dict[str, tuple[float, str]] = {}

    def normalize_group(self, value: str) -> str | None:
        value = (
            value.strip()
            .replace("–", "-")
            .replace("—", "-")
            .replace("−", "-")
        )
        value = re.sub(r"\s+", "", value)

        match = re.fullmatch(
            r"([А-ЯЁA-Z]{1,8})-?(\d{3,5}[А-ЯЁA-Z]?)",
            value,
            re.IGNORECASE,
        )
        if not match:
            return None

        letters = match.group(1)
        digits = match.group(2)
        return f"{letters}-{digits}"

    def get_html(self, url: str) -> str:
        cached = self._html_cache.get(url)
        if cached and time.time() - cached[0] < 300:
            return cached[1]

        try:
            response = self.session.get(url, timeout=(15, 45))
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise ScheduleParseError(
                f"Не удалось подключиться к {url}"
            ) from exc

        html = response.text
        self._html_cache[url] = (time.time(), html)
        return html

    def collect_scheduled_pdf_links(self, page_url: str) -> list[tuple[date, str]]:
        """Return PDF links with their calendar dates from the HTML page.

        The public calendar places the PDF links under month headings such as
        "Сентябрь - 2026". We read the closest preceding month/year text for
        each numeric day link, so old months can be skipped without downloading
        every historical PDF.
        """
        soup = BeautifulSoup(self.get_html(page_url), "html.parser")
        month_pattern = re.compile(
            r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма(?:й|я)?\w*|"
            r"июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|"
            r"ноябр\w*|декабр\w*)\s*[-–—]\s*(20\d{2})\b",
            re.IGNORECASE,
        )

        result: list[tuple[date, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, anchor["href"].strip())
            if ".pdf" not in href.lower() or href in seen:
                continue

            day_match = re.fullmatch(
                r"\s*(\d{1,2})\s*",
                anchor.get_text(" ", strip=True),
            )
            if not day_match:
                continue

            previous_month_nodes = anchor.find_all_previous(
                string=month_pattern,
                limit=1,
            )
            if not previous_month_nodes:
                continue

            month_match = month_pattern.search(str(previous_month_nodes[0]))
            if not month_match:
                continue

            month_stem = month_match.group(1).casefold()
            year = int(month_match.group(2))
            month_number = None
            for stem, number in RU_MONTHS.items():
                if month_stem.startswith(stem):
                    month_number = number
                    break

            if month_number is None:
                continue

            try:
                target_date = date(year, month_number, int(day_match.group(1)))
            except ValueError:
                continue

            result.append((target_date, href))
            seen.add(href)

        return result

    # Backward-compatible helper for any old code/tests that only need URLs.
    def collect_pdf_links(self, page_url: str) -> list[str]:
        return [
            href for _, href in self.collect_scheduled_pdf_links(page_url)
        ]

    def download_pdf(self, url: str) -> bytes:
        max_attempts = 3
        delays = [2, 5, 10]
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "PDF DOWNLOAD | attempt=%s/%s | url=%s",
                    attempt,
                    max_attempts,
                    url,
                )
                response = self.session.get(url, timeout=(15, 90))
                response.raise_for_status()

                if not response.content.startswith(b"%PDF"):
                    content_type = response.headers.get("Content-Type", "")
                    if "pdf" not in content_type.lower():
                        raise ScheduleParseError(
                            f"URL не вернул PDF: {url}"
                        )

                logger.info(
                    "PDF DOWNLOAD OK | attempt=%s/%s | size=%s KB | url=%s",
                    attempt,
                    max_attempts,
                    round(len(response.content) / 1024),
                    url,
                )
                return response.content

            except (requests.exceptions.RequestException, ScheduleParseError) as exc:
                last_error = exc
                logger.warning(
                    "PDF DOWNLOAD FAILED | attempt=%s/%s | url=%s | error=%s",
                    attempt,
                    max_attempts,
                    url,
                    exc,
                )
                if attempt < max_attempts:
                    time.sleep(delays[attempt - 1])

        raise ScheduleParseError(
            f"Не удалось скачать PDF после {max_attempts} попыток: {url}"
        ) from last_error

    def extract_pdf_date(self, pdf_bytes: bytes) -> date | None:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text_parts = []
            for page in pdf.pages[:2]:
                text_parts.append(page.extract_text() or "")

        text = "\n".join(text_parts)

        match = re.search(
            r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(20\d{2})",
            text,
        )
        if match:
            return date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
            )

        lowered = text.casefold()
        year_match = re.search(r"\b(20\d{2})\b", lowered)
        year = int(year_match.group(1)) if year_match else None

        if year is not None:
            for stem, month_number in RU_MONTHS.items():
                if stem in lowered:
                    day_match = re.search(
                        rf"\b(\d{{1,2}})\s+{re.escape(stem)}",
                        lowered,
                    )
                    if day_match:
                        return date(
                            year,
                            month_number,
                            int(day_match.group(1)),
                        )

        return None

    def extract_all_lessons(
        self,
        pdf_bytes: bytes,
        target_date: date,
        source_url: str,
    ) -> list[dict]:
        table_settings = {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
            "snap_tolerance": 3,
            "join_tolerance": 3,
            "edge_min_length": 3,
            "min_words_vertical": 1,
            "min_words_horizontal": 1,
            "text_x_tolerance": 2,
            "text_y_tolerance": 3,
        }

        result: list[dict] = []

        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables(table_settings)

                    for table in tables:
                        if not table:
                            continue

                        header_idx = None
                        group_columns: list[tuple[int, str, int | None]] = []

                        for idx, row in enumerate(table[:8]):
                            if not row:
                                continue

                            found_groups = []
                            for col_idx, cell in enumerate(row):
                                group_name = self._extract_group_name(cell)
                                if group_name:
                                    found_groups.append((col_idx, group_name))

                            if found_groups:
                                header_idx = idx
                                group_columns = [
                                    (
                                        col_idx,
                                        group_name,
                                        (
                                            col_idx + 1
                                            if col_idx + 1 < len(row)
                                            else None
                                        ),
                                    )
                                    for col_idx, group_name in found_groups
                                ]
                                break

                        if header_idx is None or not group_columns:
                            continue

                        for row in table[header_idx + 1:]:
                            if not row:
                                continue

                            pair_number = self._extract_pair_number(row[0])
                            if pair_number is None:
                                continue

                            pair_time = CALL_SCHEDULE.get(
                                target_date.weekday(),
                                {},
                            ).get(pair_number)

                            for group_col, group_name, room_col in group_columns:
                                if group_col >= len(row):
                                    continue

                                lesson_text = self._clean_cell(row[group_col])
                                if not lesson_text:
                                    continue

                                # Ignore repeated headers.
                                if self._extract_group_name(lesson_text):
                                    continue

                                room = None
                                if room_col is not None and room_col < len(row):
                                    room = self._clean_cell(row[room_col]) or None

                                canonical = self.normalize_group(group_name)
                                if not canonical:
                                    continue

                                result.append(
                                    {
                                        "group_name": canonical,
                                        "group_key": ScheduleDatabase.group_key(
                                            canonical
                                        ),
                                        "pair_number": pair_number,
                                        "pair_time": pair_time,
                                        "lesson_text": lesson_text,
                                        "room": room,
                                        "source_url": source_url,
                                    }
                                )

        except Exception as exc:
            raise ScheduleParseError(
                f"Ошибка чтения таблиц PDF: {exc}"
            ) from exc

        # Remove duplicate rows introduced by repeated tables/pages.
        unique: dict[tuple, dict] = {}
        for row in result:
            key = (
                row["group_key"],
                row["pair_number"],
                row["pair_time"],
                row["lesson_text"],
                row["room"],
            )
            unique[key] = row

        return list(unique.values())

    def _extract_group_name(self, cell: str | None) -> str | None:
        if not cell:
            return None

        clean = self._clean_cell(cell)

        # Usually one group is in one cell. We also tolerate a single-line
        # group followed by accidental whitespace.
        if "\n" not in clean and GROUP_RE.fullmatch(clean):
            return self.normalize_group(clean)

        for part in clean.splitlines():
            part = part.strip()
            if GROUP_RE.fullmatch(part):
                return self.normalize_group(part)

        return None

    @staticmethod
    def _extract_pair_number(cell: str | None) -> int | None:
        if not cell:
            return None

        clean = ScheduleParser._clean_cell(cell)
        match = re.fullmatch(r"(\d{1,2})", clean)
        return int(match.group(1)) if match else None

    @staticmethod
    def _clean_cell(value: str | None) -> str:
        if not value:
            return ""

        value = value.replace("\xa0", " ")
        value = value.replace("\u00ad", "")
        value = value.replace("\u2010", "-")
        value = value.replace("\u2011", "-")

        lines = []
        for line in value.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)

        return "\n".join(lines)


class ScheduleSyncer:
    def __init__(self, database: ScheduleDatabase):
        self.database = database
        self.parser = ScheduleParser()

    async def sync_all(self, cutoff_date: date) -> SyncStats:
        import asyncio

        total_documents = 0
        changed_documents = 0
        total_entries = 0
        failed_documents = 0

        # Keep only yesterday, today and future dates in the local database.
        await self.database.prune_before(cutoff_date)

        for corpus, page_url in CORPUS_PAGES.items():
            try:
                scheduled_links = await asyncio.to_thread(
                    self.parser.collect_scheduled_pdf_links,
                    page_url,
                )
            except Exception as exc:
                failed_documents += 1
                logger.exception(
                    "SYNC PAGE ERROR | corpus=%s | page=%s | %s",
                    corpus,
                    page_url,
                    exc,
                )
                continue

            # Critical optimization: historical dates are never downloaded.
            relevant_links = [
                (target_date, pdf_url)
                for target_date, pdf_url in scheduled_links
                if target_date >= cutoff_date
            ]

            logger.info(
                "SYNC CORPUS | corpus=%s | links=%s | relevant=%s | cutoff=%s",
                corpus,
                len(scheduled_links),
                len(relevant_links),
                cutoff_date.isoformat(),
            )

            active_dates: set[date] = set()

            for calendar_date, pdf_url in relevant_links:
                try:
                    pdf_bytes = await asyncio.to_thread(
                        self.parser.download_pdf,
                        pdf_url,
                    )
                    pdf_date = self.parser.extract_pdf_date(pdf_bytes)

                    if pdf_date is None:
                        logger.warning(
                            "SYNC PDF SKIP | corpus=%s | url=%s | "
                            "cannot determine PDF date",
                            corpus,
                            pdf_url,
                        )
                        failed_documents += 1
                        continue

                    if pdf_date < cutoff_date:
                        # Defensive check in case the site/calendar is wrong.
                        continue

                    active_dates.add(pdf_date)
                    total_documents += 1

                    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
                    old_hash = await self.database.get_document_hash(
                        pdf_date,
                        corpus,
                    )

                    fetched_at = datetime.now().astimezone().isoformat()

                    if old_hash == content_hash:
                        await self.database.upsert_document(
                            pdf_date,
                            corpus,
                            pdf_url,
                            content_hash,
                            fetched_at,
                            active=True,
                        )
                        continue

                    parsed_entries = await asyncio.to_thread(
                        self.parser.extract_all_lessons,
                        pdf_bytes,
                        pdf_date,
                        pdf_url,
                    )

                    entry_rows = []
                    for item in parsed_entries:
                        raw_key = "|".join(
                            [
                                pdf_date.isoformat(),
                                str(corpus),
                                item["group_key"],
                                str(item["pair_number"]),
                                item["lesson_text"],
                                item["room"] or "",
                            ]
                        )
                        entry_id = hashlib.sha256(
                            raw_key.encode("utf-8")
                        ).hexdigest()

                        entry_rows.append(
                            {
                                "entry_id": entry_id,
                                "schedule_date": pdf_date.isoformat(),
                                "corpus": corpus,
                                **item,
                            }
                        )

                    await self.database.replace_entries(
                        pdf_date,
                        corpus,
                        entry_rows,
                    )
                    await self.database.upsert_document(
                        pdf_date,
                        corpus,
                        pdf_url,
                        content_hash,
                        fetched_at,
                        active=True,
                    )

                    changed_documents += 1
                    total_entries += len(entry_rows)

                except Exception as exc:
                    failed_documents += 1
                    logger.exception(
                        "SYNC PDF ERROR | corpus=%s | pdf=%s | %s",
                        corpus,
                        pdf_url,
                        exc,
                    )

            if active_dates:
                await self.database.set_active_for_corpus(
                    corpus,
                    sorted(active_dates),
                )

        return SyncStats(
            corpora=len(CORPUS_PAGES),
            documents=total_documents,
            changed_documents=changed_documents,
            entries=total_entries,
            failed_documents=failed_documents,
        )

