from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional, Tuple

from aiogram import Bot
from fastapi import FastAPI, Request

from .config import Settings
from .database import User, db
from .handlers import run_start_flow

logger = logging.getLogger(__name__)

_SUCCESS_RESPONSE = {"status": True}


def _extract_first(payload: dict[str, Any], paths: Iterable[Tuple[str, ...]]) -> Any:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                break
            found = False
            if key in current:
                current = current.get(key)
                found = True
            elif isinstance(key, str):
                for candidate, value in current.items():
                    if isinstance(candidate, str) and candidate.strip() == key:
                        current = value
                        found = True
                        break
            if not found:
                break
        else:
            return current
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _extract_telegram_id(payload: dict[str, Any]) -> Optional[int]:
    raw = _extract_first(
        payload,
        (
            ("telegram_id",),
            ("chat_id",),
            ("user_id",),
            ("data", "telegram_id"),
            ("data", "chat_id"),
            ("data", "user_id"),
            ("data", "user", "id"),
        ),
    )
    return _coerce_int(raw)


def _extract_chat_id(payload: dict[str, Any], fallback: int) -> int:
    raw = _extract_first(
        payload,
        (
            ("chat_id",),
            ("data", "chat_id"),
        ),
    )
    chat_id = _coerce_int(raw)
    return chat_id if chat_id is not None else fallback


def _extract_username(payload: dict[str, Any]) -> Optional[str]:
    raw = _extract_first(
        payload,
        (
            ("username",),
            ("data", "username"),
            ("data", "user", "username"),
        ),
    )
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        return raw or None
    return None


async def _ensure_user_record(telegram_id: int, username: Optional[str]) -> Optional[User]:
    user = await db.get_user(telegram_id)
    if user is None:
        await db.create_user(telegram_id, 0, None, username)
        user = await db.get_user(telegram_id)
    elif username and user.username != username:
        await db.update_username(telegram_id, username)
        user.username = username
    return user


def create_app(bot: Bot, settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.post("/flyer_webhook")
    async def flyer_webhook(request: Request) -> dict[str, bool]:
        try:
            payload = await request.json()
        except Exception:
            logger.exception("Received invalid JSON payload from Flyer")
            return _SUCCESS_RESPONSE

        event_type = payload.get("type")
        if event_type == "test":
            return _SUCCESS_RESPONSE

        telegram_id = _extract_telegram_id(payload)
        if telegram_id is None:
            logger.warning("Webhook event without telegram_id: %s", payload)
            return _SUCCESS_RESPONSE

        username = _extract_username(payload)
        chat_id = _extract_chat_id(payload, telegram_id)

        if event_type == "sub_completed":
            user = await _ensure_user_record(telegram_id, username)
            already_verified = bool(user and user.flyer_verified)

            await db.set_flyer_verified(telegram_id, True)

            if not already_verified:
                async def _run_start() -> None:
                    try:
                        await run_start_flow(
                            bot,
                            settings,
                            telegram_id,
                            chat_id,
                            username,
                        )
                    except Exception:  # pragma: no cover - logging best effort
                        logger.exception(
                            "Failed to trigger /start flow for telegram_id=%s", telegram_id
                        )

                asyncio.create_task(_run_start())

            return _SUCCESS_RESPONSE

        if event_type == "new_status":
            status = _extract_first(payload, (("data", "status"),))
            if status == "abort":
                await db.set_flyer_verified(telegram_id, False)

                user = await _ensure_user_record(telegram_id, username)

                async def _handle_abort() -> None:
                    from .handlers import _handle_unsubscription
                    from .keyboards import subscribe_keyboard

                    if user is not None:
                        try:
                            await _handle_unsubscription(user, bot, settings)
                        except Exception:  # pragma: no cover - logging best effort
                            logger.exception(
                                "Failed to process unsubscription for telegram_id=%s", telegram_id
                            )

                    try:
                        await bot.send_message(
                            chat_id,
                            (
                                "Мы заметили, что вы отписались от обязательных каналов. "
                                "Подпишитесь снова, чтобы продолжить пользоваться ботом."
                            ),
                            reply_markup=subscribe_keyboard(settings.channel_username),
                        )
                    except Exception:  # pragma: no cover - logging best effort
                        logger.exception(
                            "Failed to notify user about unsubscription, telegram_id=%s",
                            telegram_id,
                        )

                asyncio.create_task(_handle_abort())

            return _SUCCESS_RESPONSE

        logger.info("Unhandled Flyer webhook event type: %s", event_type)
        return _SUCCESS_RESPONSE

    return app

