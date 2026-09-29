import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from database import ScheduleDatabase
from keyboards import (
    admin_keyboard,
    back_to_dates_keyboard,
    date_picker_keyboard,
    group_input_keyboard,
    main_keyboard,
)
from schedule_parser import ScheduleParseError, ScheduleSyncer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

db = ScheduleDatabase(settings.database_url)
syncer = ScheduleSyncer(db)

_sync_task: asyncio.Task | None = None
_sync_lock = asyncio.Lock()

# In-memory cache for the hot user path. Schedule data only changes after a
# successful sync, so we invalidate the cache at the end of every sync.
# The limit prevents an unexpectedly large number of groups/dates from using
# unbounded RAM on Render.
_SCHEDULE_CACHE_MAX = 2048
_schedule_cache: dict[tuple[str, date], list[dict]] = {}
_dates_cache: list[date] | None = None


def invalidate_schedule_cache() -> None:
    global _dates_cache
    _schedule_cache.clear()
    _dates_cache = None


async def get_cached_schedule(group: str, target_date: date) -> list[dict]:
    key = (db.group_key(group), target_date)
    cached = _schedule_cache.get(key)
    if cached is not None:
        return cached

    rows = await db.get_schedule_for_group(group, target_date)
    if len(_schedule_cache) >= _SCHEDULE_CACHE_MAX:
        # Drop an arbitrary old item. This is intentionally simple: the cache
        # is only an optimization, never the source of truth.
        _schedule_cache.pop(next(iter(_schedule_cache)))
    _schedule_cache[key] = rows
    return rows


async def get_cached_dates() -> list[date]:
    global _dates_cache
    if _dates_cache is None:
        _dates_cache = await db.get_available_dates()
    return _dates_cache


class Form(StatesGroup):
    waiting_for_group = State()


def college_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def is_admin_user(user_id: int) -> bool:
    return user_id in settings.admin_user_ids


def next_sync_text() -> str:
    now = college_now()
    base = now.replace(minute=0, second=0, microsecond=0)
    step = settings.sync_interval_hours
    next_hour = ((base.hour // step) + 1) * step

    if next_hour >= 24:
        next_run = datetime.combine(
            base.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=now.tzinfo,
        )
    else:
        next_run = base.replace(hour=next_hour)

    return next_run.strftime("%d.%m.%Y %H:%M")


def format_admin_status(
    sync_state: dict[str, str | None],
    stats: dict[str, int | str | None],
) -> str:
    last_sync = sync_state.get("last_success_at") or "нет данных"
    last_error = sync_state.get("last_error") or "нет"
    if len(last_error) > 300:
        last_error = last_error[:300] + "…"
    min_date = stats.get("min_date") or "—"
    max_date = stats.get("max_date") or "—"

    return (
        "👑 <b>Статус бота</b>\n\n"
        f"🕐 Последняя успешная синхронизация: <code>{last_sync}</code>\n"
        f"⏭ Следующая плановая: <code>{next_sync_text()}</code>\n"
        f"⚠️ Последняя ошибка: <code>{last_error}</code>\n"
        f"📅 Диапазон дат в БД: <code>{min_date}</code> — <code>{max_date}</code>"
    )


def format_db_stats(stats: dict[str, int | str | None]) -> str:
    return (
        "💾 <b>База данных</b>\n\n"
        f"👤 Пользователей: <b>{stats['users']}</b>\n"
        f"🏢 Корпусов: <b>{stats['corpora']}</b>\n"
        f"📄 Активных PDF: <b>{stats['active_documents']}</b>\n"
        f"📚 Всего записей занятий: <b>{stats['entries']}</b>\n"
        f"🗃 Документов в БД: <b>{stats['documents']}</b>"
    )


async def run_sync(reason: str):
    if _sync_lock.locked():
        logger.info("Sync is already running; skipping (%s)", reason)
        return None

    async with _sync_lock:
        logger.info("Starting schedule sync: %s", reason)
        try:
            stats = await syncer.sync_all(college_now().date() - timedelta(days=1))
            await db.set_sync_state(
                last_success_at=college_now().isoformat(),
                last_error="",
            )
            # DB is now authoritative again; discard cached rows/date list.
            invalidate_schedule_cache()
            logger.info(
                "Schedule sync finished: corpora=%s, documents=%s, "
                "changed=%s, entries=%s, failed=%s",
                stats.corpora,
                stats.documents,
                stats.changed_documents,
                stats.entries,
                stats.failed_documents,
            )
            return stats
        except Exception as exc:
            await db.set_sync_state(
                last_error=str(exc)[:1000],
            )
            logger.exception("Schedule sync failed")


def next_six_hour_boundary(now: datetime) -> datetime:
    base = now.replace(minute=0, second=0, microsecond=0)
    next_hour = ((base.hour // settings.sync_interval_hours) + 1) * settings.sync_interval_hours

    if next_hour >= 24:
        next_day = base.date() + timedelta(days=1)
        return datetime.combine(
            next_day,
            datetime.min.time(),
            tzinfo=now.tzinfo,
        )

    return base.replace(hour=next_hour)


async def schedule_sync_loop() -> None:
    await run_sync("startup")

    while True:
        now = college_now()
        next_run = next_six_hour_boundary(now)
        seconds = max(1, (next_run - now).total_seconds())

        logger.info(
            "Next automatic schedule sync: %s",
            next_run.isoformat(),
        )
        await asyncio.sleep(seconds)
        await run_sync("scheduled 6-hour refresh")


def format_date_ru(value: date) -> str:
    weekdays = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    return f"{weekdays[value.weekday()].capitalize()}, {value:%d.%m.%Y}"


def format_schedule_message(
    target_date: date,
    group: str,
    rows: list[dict],
) -> str:
    lines = [
        f"📅 <b>{format_date_ru(target_date)}</b>",
        f"👥 <b>Группа:</b> {group}",
        "",
    ]

    if not rows:
        lines.append("ℹ️ На эту дату для твоей группы занятий в базе нет.")
        return "\n".join(lines)

    corpora = sorted({row["corpus"] for row in rows})

    if len(corpora) == 1:
        lines.append(f"🏢 <b>Корпус:</b> №{corpora[0]}")
        lines.append("")

    current_corpus = None
    for row in rows:
        if len(corpora) > 1 and row["corpus"] != current_corpus:
            current_corpus = row["corpus"]
            lines.append(f"🏢 <b>Корпус №{current_corpus}</b>")
            lines.append("")

        pair = row["pair_number"]
        time_text = row["pair_time"]
        if time_text:
            title = f"{pair} пара · {time_text}"
        else:
            title = f"{pair} пара"

        lines.append(f"🔹 <b>{title}</b>")
        lines.append(row["lesson_text"])

        if row["room"]:
            lines.append(f"🏫 Каб. {row['room']}")

        lines.append("")

    return "\n".join(lines).rstrip()


@dp.message(F.text == "👑 Админ-панель")
async def admin_button(message: Message) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    stats = await db.get_database_stats()
    state = await db.get_sync_state()

    await message.answer(
        format_admin_status(state, stats),
        reply_markup=admin_keyboard(),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    group = await db.get_user_group(message.from_user.id)

    if group:
        await message.answer(
            f"👋 Привет!\n\n"
            f"Твоя группа: <b>{group}</b>\n\n"
            "Расписание обновляется автоматически каждые 6 часов. "
            "После обновления новые дни появляются в календаре.",
            reply_markup=main_keyboard(is_admin_user(message.from_user.id)),
        )
    else:
        await message.answer(
            "👋 Привет!\n\n"
            "Сначала укажи свою группу. После этого можно будет "
            "выбирать любой доступный день из сохранённого расписания.",
            reply_markup=group_input_keyboard(),
        )


@dp.message(F.text == "/myid")
async def my_id(message: Message) -> None:
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>"
    )


@dp.message(F.text == "/admin")
async def admin_command(message: Message) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    stats = await db.get_database_stats()
    state = await db.get_sync_state()

    await message.answer(
        format_admin_status(state, stats),
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "👥 Указать свою группу")
@dp.message(F.text == "🔄 Изменить группу")
async def start_group_input(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_for_group)
    await message.answer(
        "✏️ Напиши номер своей группы.\n\n"
        "Например:\n"
        "<code>Пк-2313</code>",
    )


@dp.message(Form.waiting_for_group, F.text)
async def save_group(message: Message, state: FSMContext) -> None:
    group = syncer.parser.normalize_group(message.text)

    if not group:
        await message.answer(
            "❌ Не смог распознать номер группы.\n\n"
            "Пример: <code>Пк-2313</code>"
        )
        return

    # Проверяем группу по реально загруженному расписанию.
    # Если такой группы нет в БД, произвольный текст не сохраняем.
    existing_group = await db.find_group(group)

    if not existing_group:
        await message.answer(
            "❌ Такой группы не найдено в расписании.\n\n"
            "Пожалуйста, укажи существующую группу.\n\n"
            "Например: <code>Пк-2313</code>"
        )
        return

    # Сохраняем официальное написание группы из расписания.
    group = existing_group
    await db.set_user_group(message.from_user.id, group)
    await state.clear()

    await message.answer(
        f"✅ Группа сохранена: <b>{group}</b>\n\n"
        "Теперь нажми «📅 Узнать расписание» и выбери нужный день.",
        reply_markup=main_keyboard(is_admin_user(message.from_user.id)),
    )


@dp.message(F.text == "📅 Узнать расписание")
async def choose_schedule_date(message: Message) -> None:
    group = await db.get_user_group(message.from_user.id)

    if not group:
        await message.answer(
            "Сначала укажи свою группу.",
            reply_markup=group_input_keyboard(),
        )
        return

    dates = await get_cached_dates()

    if not dates:
        status = await db.get_sync_state()
        last_success = status.get("last_success_at") or "ещё не было"
        await message.answer(
            "ℹ️ В базе пока нет расписания.\n\n"
            f"Последнее успешное обновление: {last_success}\n\n"
            "Бот загружает расписание автоматически каждые 6 часов."
        )
        return

    await message.answer(
        "📅 <b>Выбери день</b>",
        reply_markup=date_picker_keyboard(dates, page=0),
    )


@dp.callback_query(F.data.startswith("schedule_page:"))
async def schedule_page(callback: CallbackQuery) -> None:
    try:
        page = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Не удалось открыть страницу.", show_alert=True)
        return

    dates = await get_cached_dates()

    if not dates:
        await callback.answer("В базе пока нет расписания.", show_alert=True)
        return

    await callback.message.edit_reply_markup(
        reply_markup=date_picker_keyboard(dates, page=page)
    )
    await callback.answer()


@dp.callback_query(F.data == "schedule_back")
async def schedule_back(callback: CallbackQuery) -> None:
    dates = await get_cached_dates()

    if not dates:
        await callback.answer("В базе пока нет расписания.", show_alert=True)
        return

    await callback.message.edit_text(
        "📅 <b>Выбери день</b>",
        reply_markup=date_picker_keyboard(dates, page=0),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("schedule_date:"))
async def schedule_date_selected(callback: CallbackQuery) -> None:
    group = await db.get_user_group(callback.from_user.id)

    if not group:
        await callback.answer("Сначала укажи группу.", show_alert=True)
        return

    raw_date = callback.data.split(":", 1)[1]

    try:
        target_date = date.fromisoformat(raw_date)
    except ValueError:
        await callback.answer("Некорректная дата.", show_alert=True)
        return

    # ВАЖНО: здесь нет обращения к сайту. Сначала проверяем RAM-кэш,
    # а при промахе читаем расписание из PostgreSQL/SQLite.
    rows = await get_cached_schedule(group, target_date)

    text = format_schedule_message(target_date, group, rows)
    await callback.message.edit_text(
        text,
        reply_markup=back_to_dates_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def run_manual_sync_for_admin(
    chat_id: int,
    message_id: int,
) -> None:
    if _sync_lock.locked():
        await bot.edit_message_text(
            "⏳ Синхронизация уже выполняется.\n\n"
            "Дождись её завершения и открой админ-панель снова.",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=admin_keyboard(),
        )
        return

    stats = await run_sync("manual admin refresh")

    if stats is None:
        text = "⚠️ Синхронизация не запустилась."
    else:
        text = (
            "✅ <b>Синхронизация завершена</b>\n\n"
            f"🏢 Корпусов: <b>{stats.corpora}</b>\n"
            f"📄 PDF: <b>{stats.documents}</b>\n"
            f"🔄 Изменённых: <b>{stats.changed_documents}</b>\n"
            f"📚 Новых/обновлённых записей: <b>{stats.entries}</b>\n"
            f"⚠️ Ошибок: <b>{stats.failed_documents}</b>"
        )

    await bot.edit_message_text(
        text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin:sync")
async def admin_sync(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    if _sync_lock.locked():
        await callback.answer(
            "Синхронизация уже идёт.",
            show_alert=True,
        )
        return

    await callback.answer("Синхронизация запущена.")
    await callback.message.edit_text(
        "🔄 <b>Синхронизация запущена</b>\n\n"
        "Бот проверяет актуальные и будущие расписания всех трёх корпусов.\n"
        "Это может занять некоторое время.",
    )

    asyncio.create_task(
        run_manual_sync_for_admin(
            callback.message.chat.id,
            callback.message.message_id,
        )
    )


@dp.callback_query(F.data == "admin:status")
async def admin_status(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    stats = await db.get_database_stats()
    state = await db.get_sync_state()
    await callback.message.edit_text(
        format_admin_status(state, stats),
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:db")
async def admin_db(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    stats = await db.get_database_stats()
    await callback.message.edit_text(
        format_db_stats(stats),
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:dates")
async def admin_dates(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    dates = await get_cached_dates()
    if not dates:
        text = "📅 <b>Доступные даты</b>\n\nВ базе пока нет расписания."
    else:
        text = (
            "📅 <b>Доступные даты</b>\n\n"
            + ", ".join(day.strftime("%d.%m.%Y") for day in dates)
        )

    await callback.message.edit_text(
        text,
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    await callback.message.delete()
    await callback.answer()


@dp.message()
async def fallback(message: Message) -> None:
    group = await db.get_user_group(message.from_user.id)

    if group:
        await message.answer(
            "Выбери действие с кнопки ниже.",
            reply_markup=main_keyboard(is_admin_user(message.from_user.id)),
        )
    else:
        await message.answer(
            "Сначала укажи свою группу.",
            reply_markup=group_input_keyboard(),
        )


async def on_startup() -> None:
    global _sync_task

    await db.init()
    invalidate_schedule_cache()

    if settings.base_url:
        webhook_url = f"{settings.base_url.rstrip('/')}{settings.webhook_path}"
        await bot.set_webhook(
            webhook_url,
            secret_token=settings.webhook_secret,
        )
        logger.info("Webhook set: %s", webhook_url)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Running in local polling mode")

    _sync_task = asyncio.create_task(schedule_sync_loop())


async def on_shutdown() -> None:
    global _sync_task

    if _sync_task:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass
        _sync_task = None

    await db.close()

    if settings.base_url:
        await bot.delete_webhook()

    await bot.session.close()
    logger.info("Bot stopped")


dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


async def run_polling() -> None:
    await dp.start_polling(bot)


def run_webhook() -> None:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        status = await db.get_sync_state()
        return web.json_response(
            {
                "ok": True,
                "service": "altag-schedule-bot",
                "time": college_now().isoformat(),
                "last_sync": status.get("last_success_at"),
                "last_sync_error": status.get("last_error"),
            }
        )

    app.router.add_get("/health", health)

    request_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    )
    request_handler.register(app, path=settings.webhook_path)

    setup_application(app, dp, bot=bot)

    web.run_app(
        app,
        host="0.0.0.0",
        port=settings.port,
        access_log=logger,
    )


def main() -> None:
    if settings.base_url:
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
