from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiogram import Bot
from fastapi import FastAPI, Request

from .config import Settings
from .database import User, db
from .handlers import run_start_flow

logger = logging.getLogger(__name__)

_SUCCESS_RESPONSE = {"status": True}


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

    @app.post("/subgram_webhook")
    async def subgram_webhook(request: Request) -> dict[str, bool]:
        api_key = request.headers.get("Api-Key")
        if api_key != settings.subgram_api_key:
            logger.warning("Received webhook with invalid Api-Key")
            return _SUCCESS_RESPONSE

        try:
            payload = await request.json()
        except Exception:
            logger.exception("Received invalid JSON payload from SubGram")
            return _SUCCESS_RESPONSE

        events = payload.get("webhooks")
        if not isinstance(events, list):
            logger.info("Webhook payload without events: %s", payload)
            return _SUCCESS_RESPONSE

        for event in events:
            if not isinstance(event, dict):
                continue

            telegram_id = _coerce_int(event.get("user_id"))
            if telegram_id is None:
                logger.warning("SubGram webhook without user_id: %s", event)
                continue

            status = event.get("status")
            username = event.get("username")
            chat_id = telegram_id

            if status in {"subscribed", "notgetted"}:
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
                                "Failed to trigger /start flow for telegram_id=%s",
                                telegram_id,
                            )

                    asyncio.create_task(_run_start())
                continue

            if status == "unsubscribed":
                await db.set_flyer_verified(telegram_id, False)
                user = await _ensure_user_record(telegram_id, username)

                async def _handle_unsubscribe() -> None:
                    from .handlers import _handle_unsubscription
                    from .keyboards import subscribe_keyboard

                    if user is not None:
                        try:
                            await _handle_unsubscription(user, bot, settings)
                        except Exception:  # pragma: no cover - logging best effort
                            logger.exception(
                                "Failed to process unsubscription for telegram_id=%s",
                                telegram_id,
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

                asyncio.create_task(_handle_unsubscribe())
                continue

            logger.info("Unhandled SubGram webhook status: %s", status)

        return _SUCCESS_RESPONSE

    return app
