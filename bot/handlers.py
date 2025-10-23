from __future__ import annotations

import datetime
from contextlib import suppress
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .database import User, db
from .keyboards import (
    admin_menu_keyboard,
    main_menu_keyboard,
    subscribe_keyboard,
    support_admin_keyboard,
    withdrawal_actions_keyboard,
)
from .middlewares import mask_sensitive

router = Router()


class WithdrawStates(StatesGroup):
    waiting_for_amount = State()


class AdminBroadcastStates(StatesGroup):
    waiting_for_message = State()


class SupportStates(StatesGroup):
    waiting_for_message = State()


class AdminReplyStates(StatesGroup):
    waiting_for_reply = State()


class AdminGrantStarsStates(StatesGroup):
    waiting_for_details = State()


async def _ensure_user_record(
    telegram_id: int,
    settings: Settings,
    username: Optional[str],
    referred_by: Optional[int] = None,
) -> tuple[User, bool]:
    user = await db.get_user(telegram_id)
    created = False
    if user is None:
        await db.create_user(telegram_id, 0, referred_by, username)
        user = await db.get_user(telegram_id)
        created = True
    elif referred_by and user.referred_by is None and referred_by != telegram_id:
        await db.assign_referrer(telegram_id, referred_by)
        user.referred_by = referred_by

    if user is None:
        raise RuntimeError("Failed to ensure user record")

    if user.username != username:
        await db.update_username(telegram_id, username)
        user.username = username

    return user, created


async def ensure_user(message: Message, settings: Settings) -> User:
    user, _ = await _ensure_user_record(
        message.from_user.id,
        settings,
        message.from_user.username,
    )
    return user


async def ensure_not_banned(message: Message, user: User) -> bool:
    if user.is_banned:
        await message.answer(
            "Ваш аккаунт заблокирован. Свяжитесь с поддержкой для разблокировки."
        )
        return False
    return True


async def _update_admin_controls(
    callback: CallbackQuery, user_id: int, is_banned: bool, request_id: Optional[int]
) -> None:
    try:
        if request_id is not None:
            await callback.message.edit_reply_markup(
                reply_markup=withdrawal_actions_keyboard(
                    request_id, user_id, is_banned
                )
            )
        else:
            await callback.message.edit_reply_markup(
                reply_markup=support_admin_keyboard(user_id, is_banned)
            )
    except TelegramBadRequest:
        pass


async def _is_channel_member(bot: Bot, settings: Settings, telegram_id: int) -> bool:
    member = await bot.get_chat_member(settings.channel_username, telegram_id)
    return member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }


async def _activate_subscription(
    user: User, bot: Bot, settings: Settings
) -> bool:
    await db.set_subscription(user.telegram_id, True)
    user.is_subscribed = True
    start_bonus_awarded = False
    if not user.start_bonus_claimed:
        await db.update_balance(user.telegram_id, settings.start_bonus)
        await db.set_start_bonus_claimed(user.telegram_id, True)
        user.start_bonus_claimed = True
        start_bonus_awarded = True
    if user.referred_by and not user.reward_claimed:
        await db.update_balance(user.referred_by, settings.referral_bonus)
        await db.mark_reward_claimed(user.telegram_id)
        user.reward_claimed = True

        referral_name = f"@{user.username}" if user.username else f"ID {user.telegram_id}"
        with suppress(TelegramBadRequest):
            await bot.send_message(
                user.referred_by,
                (
                    f"Ваш реферал {referral_name} подтвердил подписку. "
                    f"Вам начислено {settings.referral_bonus} ⭐."
                ),
            )

    return start_bonus_awarded


async def _handle_unsubscription(user: User, bot: Bot, settings: Settings) -> None:
    await db.set_subscription(user.telegram_id, False)
    user.is_subscribed = False

    if user.reward_claimed and user.referred_by:
        await db.update_balance(user.telegram_id, -settings.referral_bonus)
        await db.update_balance(user.referred_by, -settings.referral_bonus)
        await db.set_reward_claimed(user.telegram_id, False)
        user.reward_claimed = False

        referral_name = f"@{user.username}" if user.username else f"ID {user.telegram_id}"
        with suppress(TelegramBadRequest):
            await bot.send_message(
                user.referred_by,
                (
                    f"Ваш реферал {referral_name} отписался от канала. "
                    f"С вашего баланса списано {settings.referral_bonus} ⭐."
                ),
            )
        with suppress(TelegramBadRequest):
            await bot.send_message(
                user.telegram_id,
                (
                    "Мы заметили, что вы отписались от канала. "
                    f"{settings.referral_bonus} ⭐ были списаны с вашего баланса и с баланса пригласившего вас пользователя."
                ),
            )


async def _verify_and_activate_subscription(
    bot: Bot, settings: Settings, user: User
) -> tuple[bool, bool, bool]:
    is_member = await _is_channel_member(bot, settings, user.telegram_id)
    if not is_member:
        if user.is_subscribed:
            await _handle_unsubscription(user, bot, settings)
        return False, False, False
    if not user.is_subscribed:
        start_bonus_awarded = await _activate_subscription(user, bot, settings)
        return True, True, start_bonus_awarded
    return True, False, False


async def ensure_subscription_access(
    message: Message, bot: Bot, settings: Settings, user: User
) -> bool:
    is_member, activated, start_bonus_awarded = await _verify_and_activate_subscription(
        bot, settings, user
    )
    if not is_member:
        await message.answer(
            "Бот доступен только после подписки на канал.",
            reply_markup=subscribe_keyboard(settings.channel_username),
        )
        return False
    if activated:
        thanks_message = "Спасибо за подписку! Теперь бот доступен полностью."
        if start_bonus_awarded:
            thanks_message += f" Вам начислено {settings.start_bonus} ⭐ стартового бонуса."
        await message.answer(thanks_message)
    return True


async def _send_start_reply(
    message: Optional[Message],
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message:
    if message is not None:
        return await message.answer(text, **kwargs)
    return await bot.send_message(chat_id, text, **kwargs)


async def run_start_flow(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
    chat_id: int,
    username: Optional[str],
    *,
    referred_by: Optional[int] = None,
    message: Optional[Message] = None,
) -> None:
    user, created = await _ensure_user_record(
        telegram_id,
        settings,
        username,
        referred_by,
    )
    if user.is_banned:
        await _send_start_reply(
            message,
            bot,
            chat_id,
            "Ваш аккаунт заблокирован. Свяжитесь с поддержкой, чтобы восстановить доступ.",
            reply_markup=main_menu_keyboard(),
        )
        return
    if created:
        await _send_start_reply(
            message,
            bot,
            chat_id,
            (
                "Добро пожаловать! Подпишитесь на наш канал, чтобы получить "
                f"стартовый бонус {settings.start_bonus} ⭐."
            ),
            reply_markup=main_menu_keyboard(),
        )
    else:
        await _send_start_reply(
            message,
            bot,
            chat_id,
            "С возвращением!",
            reply_markup=main_menu_keyboard(),
        )

    is_member, activated, start_bonus_awarded = await _verify_and_activate_subscription(
        bot, settings, user
    )
    if not is_member:
        await _send_start_reply(
            message,
            bot,
            chat_id,
            "Поделитесь ботом с друзьями и зарабатывайте звезды!",
            reply_markup=subscribe_keyboard(settings.channel_username),
        )
    elif activated:
        message_text = "Спасибо за подписку! Теперь бот доступен полностью."
        if start_bonus_awarded:
            message_text += f" Вам начислено {settings.start_bonus} ⭐ стартового бонуса."
        await _send_start_reply(message, bot, chat_id, message_text)

    bot_info = await bot.get_me()
    # --- Проверка языка пользователя перед выдачей ссылки ---
    allowed_langs = {"ru", "uk", "kk", "be"}
    user_lang = (message.from_user.language_code or "").lower() if message else "ru"

    if user_lang not in allowed_langs:
        await _send_start_reply(
            message,
            bot,
            chat_id,
            "Реферальная программа доступна только для пользователей из Беларуси, Украины, Казахстана и России.",
        )
        return
    # --- Конец проверки ---

    bot_info = await bot.get_me()
    await _send_start_reply(
        message,
        bot,
        chat_id,
        "Ваша персональная ссылка: https://t.me/{username}?start=ref{tg_id}".format(
            username=bot_info.username,
            tg_id=telegram_id,
        ),
    )



@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, bot: Bot, settings: Settings) -> None:
    telegram_id = message.from_user.id
    args = command.args or ""
    referred_by: Optional[int] = None
    if args.startswith("ref") and args[3:].isdigit():
        referred_by = int(args[3:])
        if referred_by == telegram_id:
            referred_by = None

    await run_start_flow(
        bot,
        settings,
        telegram_id,
        message.chat.id,
        message.from_user.username,
        referred_by=referred_by,
        message=message,
    )


@router.callback_query(F.data.startswith("subgram"))
async def handle_subgram_callback(callback: CallbackQuery) -> None:
    """Пустой обработчик, позволяющий корректно учитывать колбэки SubGram."""
    return None


@router.message(F.text == "💰 Баланс")
async def show_balance(message: Message, settings: Settings, bot: Bot) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        return
    await message.answer(f"На вашем балансе {user.balance} ⭐")


@router.message(F.text == "🎁 Ежедневный бонус")
async def daily_bonus(message: Message, settings: Settings, bot: Bot) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        return
    if not getattr(user, "flyer_verified", False):
        await message.answer(
            "Получить ежедневный бонус можно после выполнения заданий из SubGram."
        )
        return
    now = datetime.datetime.utcnow()
    last_bonus = None
    if user.last_daily_bonus:
        last_bonus = datetime.datetime.fromisoformat(user.last_daily_bonus)

    if last_bonus and (now - last_bonus) < datetime.timedelta(hours=24):
        remaining = datetime.timedelta(hours=24) - (now - last_bonus)
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes = remainder // 60
        await message.answer(
            f"Следующий бонус будет доступен через {hours} ч {minutes} мин."
        )
        return

    await db.update_balance(user.telegram_id, settings.daily_bonus)
    await db.set_last_daily_bonus(user.telegram_id, now.isoformat())
    await message.answer(
        f"Вы получили {settings.daily_bonus} ⭐ ежедневного бонуса!"
    )


@router.message(F.text == "👥 Реферальная ссылка")
async def referral_link(message: Message, bot: Bot, settings: Settings) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        return
    bot_info = await bot.get_me()
    await message.answer(
        "Поделитесь этой ссылкой: https://t.me/{username}?start=ref{tg_id}".format(
            username=bot_info.username,
            tg_id=user.telegram_id,
        )
    )


@router.message(F.text == "🏆 Топ приглашений")
async def top_referrers(message: Message, settings: Settings, bot: Bot) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        return
    top = await db.list_top_referrers()
    if not top:
        await message.answer("Пока нет приглашений. Будьте первым!")
        return
    lines = ["Топ приглашений:"]
    for index, (telegram_id, total) in enumerate(top, start=1):
        masked = mask_sensitive(str(telegram_id))
        lines.append(f"{index}. {masked} — {total} друзей")
    await message.answer("\n".join(lines))


@router.message(F.text == "✅ Проверить подписку")
async def check_subscription(message: Message, bot: Bot, settings: Settings) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    is_member, activated, start_bonus_awarded = await _verify_and_activate_subscription(
        bot, settings, user
    )
    if not is_member:
        await message.answer(
            "Пожалуйста, подпишитесь на канал, чтобы получать награды.",
            reply_markup=subscribe_keyboard(settings.channel_username),
        )
        return

    if activated:
        response = "Спасибо за подписку! Награды активированы."
        if start_bonus_awarded:
            response += f" Вам начислено {settings.start_bonus} ⭐ стартового бонуса."
        if user.referred_by and user.reward_claimed:
            response += f" Вашему другу начислено {settings.referral_bonus} ⭐ за приглашение."
        await message.answer(response)
    else:
        await message.answer("Подписка уже подтверждена.")


@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(
    callback: CallbackQuery, bot: Bot, settings: Settings
) -> None:
    user, _ = await _ensure_user_record(
        callback.from_user.id,
        settings,
        callback.from_user.username,
    )
    if user.is_banned:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        with suppress(TelegramBadRequest):
            await callback.message.answer(
                "Ваш аккаунт заблокирован. Свяжитесь с поддержкой для разблокировки."
            )
        return
    is_member, activated, start_bonus_awarded = await _verify_and_activate_subscription(
        bot, settings, user
    )
    if not is_member:
        await callback.answer(
            "Подпишитесь на канал, чтобы продолжить.",
            show_alert=True,
        )
        await callback.message.answer(
            "Пожалуйста, подпишитесь на канал, чтобы получать награды.",
            reply_markup=subscribe_keyboard(settings.channel_username),
        )
        return

    if activated:
        response = "Спасибо за подписку! Награды активированы."
        if start_bonus_awarded:
            response += f" Вам начислено {settings.start_bonus} ⭐ стартового бонуса."
        if user.referred_by and user.reward_claimed:
            response += f" Вашему другу начислено {settings.referral_bonus} ⭐ за приглашение."
    else:
        response = "Подписка уже подтверждена."

    await callback.message.answer(response)
    await callback.answer("Готово!")


@router.message(F.text == "💳 Вывод средств")
async def withdrawal_request(message: Message, settings: Settings, bot: Bot, state: FSMContext) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        return
    referrals = await db.list_referrals(user.telegram_id, limit=10)
    if referrals:
        invited_lines = [
            f"• @{username}" if username else f"• ID {ref_id}"
            for ref_id, username in referrals
        ]
        await message.answer(
            "Ваши приглашенные друзья (последние 10):\n" + "\n".join(invited_lines)
        )
    else:
        await message.answer("Вы еще не пригласили друзей.")

    if user.balance < settings.min_withdrawal:
        await message.answer(
            f"Минимальная сумма вывода {settings.min_withdrawal} ⭐. На вашем балансе {user.balance} ⭐."
        )
        return
    await state.set_state(WithdrawStates.waiting_for_amount)
    await message.answer(
        "Введите сумму для вывода (не менее {minimum} ⭐):".format(
            minimum=settings.min_withdrawal
        )
    )


@router.message(WithdrawStates.waiting_for_amount)
async def process_withdraw_amount(
    message: Message, settings: Settings, bot: Bot, state: FSMContext
) -> None:
    user = await ensure_user(message, settings)
    if not await ensure_not_banned(message, user):
        await state.clear()
        return
    if not await ensure_subscription_access(message, bot, settings, user):
        await state.clear()
        return
    try:
        amount = int(message.text)
    except (TypeError, ValueError):
        await message.answer("Введите целое число.")
        return

    if amount < settings.min_withdrawal:
        await message.answer(
            f"Минимальная сумма вывода {settings.min_withdrawal} ⭐. Попробуйте снова."
        )
        return

    if amount > user.balance:
        await message.answer("Недостаточно средств для вывода.")
        return

    await db.update_balance(user.telegram_id, -amount)
    await db.add_withdrawal(user.telegram_id, amount)
    await state.clear()
    await message.answer(
        "Заявка на вывод создана. Администратор свяжется с вами в ближайшее время."
    )


@router.message(F.text == "🆘 Поддержка")
async def support_entry(message: Message, settings: Settings, state: FSMContext) -> None:
    await ensure_user(message, settings)
    await state.clear()
    await state.set_state(SupportStates.waiting_for_message)
    await message.answer(
        "Опишите вашу проблему одним сообщением. Для отмены отправьте /cancel или 'отмена'."
    )


@router.message(SupportStates.waiting_for_message)
async def support_message(
    message: Message, settings: Settings, state: FSMContext, bot: Bot
) -> None:
    user = await ensure_user(message, settings)
    text = message.text or message.caption or ""
    if text.strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Обращение отменено.")
        return

    if not text.strip():
        await message.answer("Пожалуйста, отправьте текстовое сообщение.")
        return

    await state.clear()

    display = f"@{user.username}" if user.username else f"ID {user.telegram_id}"
    status_line = "заблокирован" if user.is_banned else "активен"
    support_text = (
        "Новое обращение в поддержку\n"
        f"От: {display} (ID {user.telegram_id})\n"
        f"Статус: {status_line}\n\n"
        f"{text}"
    )

    notified = False
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                support_text,
                reply_markup=support_admin_keyboard(user.telegram_id, user.is_banned),
            )
            notified = True
        except (TelegramBadRequest, TelegramForbiddenError):
            continue

    if notified:
        await message.answer(
            "Ваше обращение отправлено администрации. Ожидайте ответа в этом чате."
        )
    else:
        await message.answer(
            "Не удалось доставить сообщение администраторам. Попробуйте позже."
        )


@router.callback_query(F.data.startswith("support_reply"))
async def support_reply_start(
    callback: CallbackQuery, settings: Settings, state: FSMContext
) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    _, user_id_raw = callback.data.split(":", 1)
    target_id = int(user_id_raw)
    await state.clear()
    await state.set_state(AdminReplyStates.waiting_for_reply)
    await state.update_data(reply_target=target_id)
    await callback.message.answer(
        (
            "Введите ответ для пользователя ID {user_id}.\n"
            "Для отмены отправьте /cancel или 'отмена'."
        ).format(user_id=target_id),
        reply_markup=admin_menu_keyboard(),
    )
    await callback.answer()


@router.message(AdminReplyStates.waiting_for_reply)
async def support_reply_send(
    message: Message, settings: Settings, state: FSMContext, bot: Bot
) -> None:
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Доступ запрещен.")
        return

    text = message.text or message.caption or ""
    if text.strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Ответ отменен.", reply_markup=admin_menu_keyboard())
        return

    data = await state.get_data()
    target_id = data.get("reply_target")
    if not target_id:
        await state.clear()
        await message.answer(
            "Не удалось определить адресата сообщения.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if not text.strip():
        await message.answer("Пожалуйста, отправьте текст ответа или /cancel.")
        return

    admin_name = (
        f"@{message.from_user.username}" if message.from_user.username else "Администратор"
    )
    reply_text = (
        "Сообщение от поддержки:\n"
        f"{text}\n\n"
        f"Ответил: {admin_name}"
    )

    try:
        await bot.send_message(target_id, reply_text)
    except TelegramForbiddenError:
        await message.answer(
            "Не удалось отправить сообщение: пользователь заблокировал бота.",
            reply_markup=admin_menu_keyboard(),
        )
    except TelegramBadRequest:
        await message.answer(
            "Не удалось отправить сообщение. Попробуйте изменить текст.",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await message.answer(
            "Ответ отправлен пользователю.", reply_markup=admin_menu_keyboard()
        )
    finally:
        await state.clear()


def _parse_target_payload(payload: str) -> tuple[int, Optional[int]]:
    parts = payload.split(":")
    user_id = int(parts[1])
    request_id: Optional[int] = None
    if len(parts) > 2 and parts[2].isdigit():
        request_id = int(parts[2])
    return user_id, request_id


async def _set_ban_status(
    bot: Bot, user_id: int, banned: bool
) -> Optional[User]:
    user = await db.get_user(user_id)
    if user is None:
        return None
    if user.is_banned == banned:
        return user
    await db.set_ban_status(user_id, banned)
    user.is_banned = banned
    notify_text = (
        "Ваш аккаунт заблокирован. Свяжитесь с поддержкой, чтобы узнать подробности."
        if banned
        else "Ваш аккаунт разблокирован. Вы снова можете пользоваться ботом."
    )
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(user_id, notify_text)
    return user


@router.callback_query(F.data.startswith("block_user"))
async def block_user_callback(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    user_id, request_id = _parse_target_payload(callback.data)
    user = await _set_ban_status(callback.bot, user_id, True)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    await _update_admin_controls(callback, user_id, True, request_id)
    await callback.answer("Пользователь заблокирован")


@router.callback_query(F.data.startswith("unblock_user"))
async def unblock_user_callback(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    user_id, request_id = _parse_target_payload(callback.data)
    user = await _set_ban_status(callback.bot, user_id, False)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    await _update_admin_controls(callback, user_id, False, request_id)
    await callback.answer("Пользователь разблокирован")


@router.message(Command("admin"))
async def admin_panel(message: Message, settings: Settings) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Доступ запрещен.")
        return
    await message.answer("Админ-панель", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    total_users = await db.count_users()
    total_balance = await db.sum_balances()
    await callback.message.edit_text(
        f"Всего пользователей: {total_users}\n"
        f"Общий баланс: {total_balance} ⭐",
        reply_markup=admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    requests = await db.list_withdrawals(status="pending")
    if not requests:
        await callback.answer("Нет ожидающих заявок", show_alert=True)
        return
    for request in requests:
        user = await db.get_user(request.telegram_id)
        if user and user.username:
            user_line = f"@{user.username} (ID {user.telegram_id})"
        else:
            user_line = f"ID {request.telegram_id}"

        referrals = await db.list_referrals(request.telegram_id, limit=10)
        if referrals:
            referrals_lines = "\n".join(
                f"• @{username}" if username else f"• ID {ref_id}"
                for ref_id, username in referrals
            )
            referrals_block = f"\nПриглашенные друзья (последние 10):\n{referrals_lines}"
        else:
            referrals_block = "\nПриглашенные друзья: нет"

        status_line = "Заблокирован" if (user and user.is_banned) else "Активен"

        await callback.message.answer(
            (
                f"Заявка #{request.id}\n"
                f"Пользователь: {user_line}\n"
                f"Сумма: {request.amount} ⭐\n"
                f"Создана: {request.created_at}\n"
                f"Статус пользователя: {status_line}{referrals_block}"
            ),
            reply_markup=withdrawal_actions_keyboard(
                request.id,
                request.telegram_id,
                bool(user and user.is_banned),
            ),
        )
    await callback.answer()


async def _update_withdrawal_status(callback: CallbackQuery, status: str, bot: Bot) -> None:
    _, raw_id = callback.data.split(":", 1)
    request_id = int(raw_id)
    request = await db.get_withdrawal(request_id)
    if request is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    await db.set_withdrawal_status(request_id, status)

    status_label = {
        "paid": "Выплачено",
        "rejected": "Отклонено",
    }.get(status, status)

    try:
        await callback.message.edit_text(
            callback.message.text + f"\nСтатус обновлен: {status_label}",
        )
    except TelegramBadRequest:
        pass

    user_message = {
        "paid": "✅ Ваша заявка на вывод выплачена.",
        "rejected": "❌ Ваша заявка на вывод отклонена. Свяжитесь с поддержкой для уточнения деталей.",
    }.get(status, f"Статус вашей заявки изменен: {status_label}.")

    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(
            request.telegram_id,
            (
                f"Заявка #{request.id} на вывод {request.amount} ⭐:\n"
                f"{user_message}"
            ),
        )

    await callback.answer("Статус обновлен")


@router.callback_query(F.data.startswith("withdraw_paid"))
async def withdrawal_paid(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await _update_withdrawal_status(callback, "paid", callback.bot)


@router.callback_query(F.data.startswith("withdraw_rejected"))
async def withdrawal_rejected(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await _update_withdrawal_status(callback, "rejected", callback.bot)


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminBroadcastStates.waiting_for_message)
    await callback.message.edit_text(
        "Отправьте текст сообщения, которое нужно разослать всем пользователям.\n"
        "Для отмены введите /cancel.",
        reply_markup=admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_grant_stars")
async def admin_grant_stars_start(
    callback: CallbackQuery, settings: Settings, state: FSMContext
) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminGrantStarsStates.waiting_for_details)
    await callback.message.edit_text(
        (
            "Введите через пробел ID пользователя или @username и количество ⭐,\n"
            "например: 123456789 50.\n"
            "Для отмены введите /cancel."
        ),
        reply_markup=admin_menu_keyboard(),
    )
    await callback.answer()


@router.message(AdminGrantStarsStates.waiting_for_details)
async def admin_grant_stars_apply(
    message: Message, settings: Settings, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Доступ запрещен.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Укажите данные в формате: ID количество")
        return

    if text.lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer(
            "Начисление отменено.", reply_markup=admin_menu_keyboard()
        )
        return

    parts = text.split()
    if len(parts) != 2:
        await message.answer(
            "Некорректный формат. Укажите ID (или @username) и количество через пробел."
        )
        return

    user_ref, amount_text = parts
    if user_ref.startswith("@"):
        username = user_ref.lstrip("@")
        user = await db.get_user_by_username(username)
    else:
        try:
            telegram_id = int(user_ref)
        except ValueError:
            await message.answer("ID пользователя должен быть числом.")
            return
        user = await db.get_user(telegram_id)

    if user is None:
        await message.answer("Пользователь не найден в базе данных.")
        return

    try:
        amount = int(amount_text)
    except ValueError:
        await message.answer("Количество ⭐ должно быть целым числом.")
        return

    if amount <= 0:
        await message.answer("Количество ⭐ должно быть положительным.")
        return

    await db.update_balance(user.telegram_id, amount)

    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await message.bot.send_message(
            user.telegram_id,
            (
                f"Администрация начислила {amount} ⭐ на ваш баланс.\n"
                "Если у вас есть вопросы, свяжитесь с поддержкой."
            ),
        )

    await state.clear()
    await message.answer(
        (
            f"У пользователя ID {user.telegram_id} начислено {amount} ⭐.\n"
            "Уведомление отправлено."
        ),
        reply_markup=admin_menu_keyboard(),
    )


@router.message(AdminBroadcastStates.waiting_for_message)
async def admin_broadcast_send(
    message: Message,
    settings: Settings,
    state: FSMContext,
) -> None:
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Доступ запрещен.")
        return

    text = message.text or ""
    if text.strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=admin_menu_keyboard())
        return

    users = await db.list_all_users()
    sent = 0
    for user in users:
        try:
            await message.send_copy(user.telegram_id)
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            continue

    await state.clear()
    await message.answer(
        f"Рассылка отправлена {sent} пользователям.",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin_regen_pin")
async def regen_pin(callback: CallbackQuery, settings: Settings) -> None:
    import secrets

    new_pin = secrets.token_hex(3)
    await callback.answer(f"Новый защитный PIN: {new_pin}", show_alert=True)
    await callback.message.edit_text(
        "PIN обновлен. Передайте его только проверенным модераторам.",
        reply_markup=admin_menu_keyboard(),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
