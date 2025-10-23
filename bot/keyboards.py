from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🎁 Ежедневный бонус")],
            [KeyboardButton(text="👥 Реферальная ссылка"), KeyboardButton(text="🏆 Топ приглашений")],
            [KeyboardButton(text="💳 Вывод средств"), KeyboardButton(text="✅ Проверить подписку")],
            [KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
    )


def subscribe_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал", url=f"https://t.me/{channel_username.lstrip('@')}")],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")],
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📜 Запросы на вывод", callback_data="admin_withdrawals")],
            [InlineKeyboardButton(text="➕ Начислить ⭐", callback_data="admin_grant_stars")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔐 Перегенерировать защитный PIN", callback_data="admin_regen_pin")],
        ]
    )


def withdrawal_actions_keyboard(
    request_id: int,
    user_id: int,
    is_banned: bool,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплачено", callback_data=f"withdraw_paid:{request_id}"),
                InlineKeyboardButton(text="❌ Отклонено", callback_data=f"withdraw_rejected:{request_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Разблокировать" if is_banned else "🚫 Заблокировать",
                    callback_data=(
                        f"unblock_user:{user_id}:{request_id}"
                        if is_banned
                        else f"block_user:{user_id}:{request_id}"
                    ),
                )
            ],
        ]
    )


def support_admin_keyboard(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support_reply:{user_id}")],
            [
                InlineKeyboardButton(
                    text="🚫 Разблокировать" if is_banned else "🚫 Заблокировать",
                    callback_data=(
                        f"unblock_user:{user_id}" if is_banned else f"block_user:{user_id}"
                    ),
                )
            ],
        ]
    )
