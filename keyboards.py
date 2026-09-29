from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


DATE_PAGE_SIZE = 14


def main_keyboard(is_admin: bool = False):
    # Kept as a ReplyKeyboard for the main actions.
    from aiogram.types import ReplyKeyboardMarkup

    rows = [
        [KeyboardButton(text="📅 Узнать расписание")],
        [KeyboardButton(text="🔄 Изменить группу")],
    ]

    if is_admin:
        rows.append([KeyboardButton(text="👑 Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def group_input_keyboard():
    from aiogram.types import ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Указать свою группу")],
        ],
        resize_keyboard=True,
    )


def date_picker_keyboard(
    dates: list[date],
    page: int = 0,
    selected_date: date | None = None,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(dates) + DATE_PAGE_SIZE - 1) // DATE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * DATE_PAGE_SIZE
    page_dates = dates[start : start + DATE_PAGE_SIZE]

    builder = InlineKeyboardBuilder()

    for day in page_dates:
        weekday = [
            "пн", "вт", "ср", "чт", "пт", "сб", "вс"
        ][day.weekday()]

        prefix = "✅ " if selected_date == day else "📅 "
        builder.button(
            text=f"{prefix}{day:%d.%m} ({weekday})",
            callback_data=f"schedule_date:{day.isoformat()}",
        )

    builder.adjust(2)

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"schedule_page:{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data=f"schedule_page:{page}",
        )
    )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"schedule_page:{page + 1}",
            )
        )

    builder.row(*navigation)

    return builder.as_markup()


def back_to_dates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К выбору дня",
                    callback_data="schedule_back",
                )
            ]
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить расписание сейчас",
                    callback_data="admin:sync",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус",
                    callback_data="admin:status",
                ),
                InlineKeyboardButton(
                    text="💾 База данных",
                    callback_data="admin:db",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 Доступные даты",
                    callback_data="admin:dates",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="admin:back",
                )
            ],
        ]
    )
