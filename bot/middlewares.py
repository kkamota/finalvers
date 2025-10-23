import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .database import db
from .subgram import SubgramClient


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0) -> None:
        super().__init__()
        self.rate_limit = rate_limit
        self._user_timestamps: Dict[int, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        async with self._lock:
            now = asyncio.get_running_loop().time()
            last_time = self._user_timestamps[user.id]
            if now - last_time < self.rate_limit:
                return
            self._user_timestamps[user.id] = now
        return await handler(event, data)


class SubgramCheckMiddleware(BaseMiddleware):
    """Промежуточный слой, который проверяет пользователя через SubGram."""

    BLOCKING_STATUSES = {"warning", "gender", "age", "register"}

    def __init__(self, client: SubgramClient) -> None:
        super().__init__()
        self._client = client

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot = data.get("bot")
        if user is None or bot is None:
            return await handler(event, data)

        chat_id = self._resolve_chat_id(event)
        if chat_id is None:
            return await handler(event, data)

        user_record = await db.get_user(user.id)
        was_verified = bool(user_record and getattr(user_record, "flyer_verified", 0))

        if was_verified and not self._is_subgram_callback(event):
            return await handler(event, data)

        referred_by = self._extract_referred_by(event, user.id)
        username = getattr(user, "username", None)

        if was_verified and self._is_subgram_callback(event):
            await self._acknowledge_callback(event)
            await self._cleanup_callback_message(event)
            return await handler(event, data)

        await self._remember_user_context(user.id, username, referred_by, user_record)

        extra_params = self._extract_additional_parameters(event)
        user_payload: Dict[str, Any] = {
            "first_name": getattr(user, "first_name", None),
            "username": username,
            "language_code": getattr(user, "language_code", None),
        }
        is_premium = getattr(user, "is_premium", None)
        if is_premium is not None:
            user_payload["is_premium"] = bool(is_premium)
        user_payload.update(extra_params)

        user_payload.setdefault("action", "subscribe")

        response = await self._client.get_sponsors(
            user_id=user.id,
            chat_id=chat_id,
            **user_payload,
        )

        if response is None:
            await self._send_blocking_message(
                bot,
                event,
                chat_id,
                (
                    "Не удалось проверить выполнение заданий. "
                    "Повторите попытку позже."
                ),
            )
            return await self._maybe_call_subgram_handler(handler, event, data)

        status = response.get("status")
        if status in self.BLOCKING_STATUSES:
            handled = await self._handle_blocking_status(bot, event, chat_id, response, status)
            if handled:
                return None
            status = "ok"

        if status == "error":
            logging.warning("SubGram API responded with error: %s", response)
            await self._send_blocking_message(
                bot,
                event,
                chat_id,
                response.get("message")
                or "Ошибка при проверке заданий. Попробуйте еще раз позже.",
            )
            return await self._maybe_call_subgram_handler(handler, event, data)

        await db.set_flyer_verified(user.id, True)

        if not was_verified:
            await self._trigger_start(event, data)

        if self._is_subgram_callback(event):
            await self._acknowledge_callback(event)
            await self._cleanup_callback_message(event)
            return await handler(event, data)

        return await handler(event, data)

    async def _handle_blocking_status(
        self,
        bot: Any,
        event: TelegramObject,
        chat_id: int,
        response: Dict[str, Any],
        status: Optional[str],
    ) -> bool:
        prompt = self._build_blocking_prompt(response, status)
        if prompt is None:
            text = "Выполните обязательные задания, чтобы продолжить пользоваться ботом."
            return await self._send_blocking_message(bot, event, chat_id, text)

        text, markup = prompt
        return await self._send_blocking_message(bot, event, chat_id, text, markup)

    async def _remember_user_context(
        self,
        telegram_id: int,
        username: Optional[str],
        referred_by: Optional[int],
        user_record: Optional[Any],
    ) -> None:
        if user_record is None:
            await db.create_user(telegram_id, 0, referred_by, username)
            return

        if username is not None and username != user_record.username:
            await db.update_username(telegram_id, username)
            with suppress(AttributeError):
                user_record.username = username

        if (
            referred_by is not None
            and referred_by != telegram_id
            and user_record.referred_by is None
        ):
            await db.assign_referrer(telegram_id, referred_by)
            with suppress(AttributeError):
                user_record.referred_by = referred_by

    def _extract_additional_parameters(self, event: TelegramObject) -> Dict[str, Any]:
        if not isinstance(event, CallbackQuery) or not event.data:
            return {}

        data = event.data
        if data.startswith("subgram_gender_"):
            return {"gender": data.split("_")[2]}
        if data.startswith("subgram_age_"):
            return {"age": data.split("_")[2]}
        return {}

    async def _trigger_start(
        self, event: TelegramObject, data: Dict[str, Any]
    ) -> None:
        bot = data.get("bot")
        if bot is None:
            return

        chat_id: Optional[int] = None
        user = data.get("event_from_user")
        settings = data.get("settings")

        if isinstance(event, Message):
            if (event.text or "").startswith("/start"):
                return
            chat_id = event.chat.id
            trigger_message: Optional[Message] = event
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id
            trigger_message = None
        else:
            trigger_message = None

        if chat_id is None or user is None or settings is None:
            return

        with suppress(Exception):
            from .handlers import run_start_flow

            await run_start_flow(
                bot,
                settings,
                user.id,
                chat_id,
                getattr(user, "username", None),
                message=trigger_message,
            )

    def _resolve_chat_id(self, event: TelegramObject) -> Optional[int]:
        if isinstance(event, Message):
            return event.chat.id
        if isinstance(event, CallbackQuery) and event.message:
            return event.message.chat.id
        return None

    def _is_subgram_callback(self, event: TelegramObject) -> bool:
        return isinstance(event, CallbackQuery) and bool(event.data and event.data.startswith("subgram"))

    async def _acknowledge_callback(self, event: CallbackQuery) -> None:
        text = "⏳ Проверяем задания..." if event.data == "subgram-op" else None
        with suppress(Exception):
            await event.answer(text)

    async def _cleanup_callback_message(self, event: TelegramObject) -> None:
        if not isinstance(event, CallbackQuery) or event.message is None:
            return
        with suppress(TelegramBadRequest):
            await event.message.delete()

    async def _maybe_call_subgram_handler(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if self._is_subgram_callback(event):
            return await handler(event, data)
        return None

    async def _send_blocking_message(
        self,
        bot: Any,
        event: TelegramObject,
        chat_id: int,
        text: str,
        markup: Optional[Any] = None,
    ) -> bool:
        if self._is_subgram_callback(event):
            await self._acknowledge_callback(event)
            await self._cleanup_callback_message(event)

        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:
            logging.exception("Failed to send SubGram task prompt to chat_id=%s", chat_id)
        return True

    def _build_blocking_prompt(
        self, response: Dict[str, Any], status: Optional[str]
    ) -> Optional[tuple[str, Any]]:
        if status == "warning":
            additional = response.get("additional") or {}
            sponsors = additional.get("sponsors") or []
            builder = InlineKeyboardBuilder()
            tasks: list[str] = []
            for sponsor in sponsors:
                if not sponsor:
                    continue
                if not sponsor.get("available_now"):
                    continue
                if sponsor.get("status") != "unsubscribed":
                    continue
                link = sponsor.get("link")
                if not link:
                    continue
                button_text = sponsor.get("button_text") or "Подписаться"
                builder.button(text=button_text, url=link)
                name = sponsor.get("resource_name") or button_text
                tasks.append(name)

            if not tasks:
                return None

            builder.button(text="✅ Я выполнил", callback_data="subgram-op")
            builder.adjust(1)

            task_lines = "\n".join(f"• {task}" for task in tasks)
            text = (
                "Чтобы продолжить, выполните задания:\n"
                f"{task_lines}\n\nПосле выполнения нажмите «✅ Я выполнил»."
            )
            return text, builder.as_markup()

        if status == "gender":
            builder = InlineKeyboardBuilder()
            builder.button(text="Мужской", callback_data="subgram_gender_male")
            builder.button(text="Женский", callback_data="subgram_gender_female")
            builder.adjust(2)
            return "Укажите ваш пол:", builder.as_markup()

        if status == "age":
            builder = InlineKeyboardBuilder()
            age_categories = {
                "c1": "Младше 10",
                "c2": "11-13",
                "c3": "14-15",
                "c4": "16-17",
                "c5": "18-24",
                "c6": "25 и старше",
            }
            for code, label in age_categories.items():
                builder.button(text=label, callback_data=f"subgram_age_{code}")
            builder.adjust(2)
            return "Укажите ваш возраст:", builder.as_markup()

        if status == "register":
            additional = response.get("additional") or {}
            reg_url = additional.get("registration_url")
            if not reg_url:
                return None
            builder = InlineKeyboardBuilder()
            builder.button(
                text="✅ Пройти регистрацию",
                web_app=WebAppInfo(url=reg_url),
            )
            builder.button(text="Продолжить", callback_data="subgram-op")
            builder.adjust(1)
            text = "Для продолжения, пожалуйста, пройдите быструю регистрацию."
            return text, builder.as_markup()

        return None

    def _extract_referred_by(
        self, event: TelegramObject, telegram_id: int
    ) -> Optional[int]:
        if not isinstance(event, Message):
            return None

        text = (event.text or "").strip()
        if not text.startswith("/start"):
            return None

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return None

        arg = parts[1].strip().split()[0]
        if not arg.lower().startswith("ref"):
            return None

        suffix = arg[3:]
        if not suffix.isdigit():
            return None

        referred_by = int(suffix)
        if referred_by == telegram_id:
            return None

        return referred_by


def mask_sensitive(text: str) -> str:
    if len(text) <= 6:
        return "*" * len(text)
    return text[:3] + "*" * (len(text) - 6) + text[-3:]
