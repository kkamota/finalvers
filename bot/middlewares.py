import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from flyerapi import APIError, Flyer

from .database import db


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


class FlyerCheckMiddleware(BaseMiddleware):
    def __init__(self, flyer: Flyer, *, message_template: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.flyer = flyer
        self._message_template = message_template or {
            "text": "Чтобы продолжить работу с ботом, выполните задания ниже.",
        }

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        language_code = getattr(user, "language_code", None)
        message_payload = dict(self._message_template)

        user_record = await db.get_user(user.id)
        referred_by = self._extract_referred_by(event, user.id)
        username = getattr(user, "username", None)
        if user_record and getattr(user_record, "flyer_verified", 0):
            return await handler(event, data)

        was_verified = bool(getattr(user_record, "flyer_verified", 0)) if user_record else False


        try:
            is_allowed = await self.flyer.check(
                user.id,
                language_code=language_code,
                message=message_payload,
            )
        except APIError:
            logging.exception("Flyer API returned an error during check")
            return await handler(event, data)
        except Exception:
            logging.exception("Unexpected error during Flyer verification")
            return await handler(event, data)

        if not is_allowed:
            await self._remember_user_context(
                user.id,
                username,
                referred_by,
                user_record,
            )
            await self._notify_verification_required(event)
            return None

        if user_record is None:
            await db.create_user(
                user.id,
                0,
                referred_by,
                username,
            )
        else:
            if (
                referred_by is not None
                and referred_by != user.id
                and user_record.referred_by is None
            ):
                await db.assign_referrer(user.id, referred_by)
                with suppress(AttributeError):
                    user_record.referred_by = referred_by
            if username is not None and username != user_record.username:
                await db.update_username(user.id, username)
                with suppress(AttributeError):
                    user_record.username = username
        await db.set_flyer_verified(user.id, True)

        if not was_verified:
            await self._trigger_start(event, data)

        return await handler(event, data)

    async def _notify_verification_required(self, event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer()

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
