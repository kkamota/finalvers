import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from flyerapi import APIError, Flyer

from .database import db
from .subgram import SubgramClient

logger = logging.getLogger(__name__)


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
    LANGUAGE_BLOCK_MESSAGES = {
        "fa": "متأسفانه این ربات برای شما در دسترس نیست.",
        "ar": "للأسف، هذا البوت غير متاح لك.",
        "tr": "Maalesef bu bot size uygun değildir.",
    }

    def __init__(self, client: SubgramClient, *, flyer: Optional[Flyer] = None) -> None:
        super().__init__()
        self._client = client
        self._flyer = flyer
        self._flyer_message_template = {
            "text": "Чтобы продолжить работу с ботом, выполните задания ниже.",
        }

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

        logger.info(
            "SubGram middleware invoked | user_id=%s chat_id=%s was_verified=%s is_callback=%s",
            user.id,
            chat_id,
            was_verified,
            self._is_subgram_callback(event),
        )

        blocked_language_message = self._get_language_block_message(
            getattr(user, "language_code", None)
        )
        if blocked_language_message is not None:
            logger.info(
                "SubGram language gating | user_id=%s chat_id=%s language_code=%s",
                user.id,
                chat_id,
                getattr(user, "language_code", None),
            )
            await self._send_blocking_message(
                bot, event, chat_id, blocked_language_message
            )
            return await self._maybe_call_subgram_handler(handler, event, data)

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
            logger.warning(
                "SubGram response missing | user_id=%s chat_id=%s", user.id, chat_id
            )
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
        logger.info(
            "SubGram status resolved | user_id=%s chat_id=%s status=%s message=%s",
            user.id,
            chat_id,
            status,
            response.get("message"),
        )
        effective_referrer = referred_by or (
            getattr(user_record, "referred_by", None) if user_record is not None else None
        )

        flyer_allowed = True
        if response.get("code") == 404:
            if effective_referrer:
                logger.warning(
                    "SubGram flagged potential fake account | user_id=%s chat_id=%s response=%s",
                    user.id,
                    chat_id,
                    response,
                )
                await self._reward_referrer_for_fake_warning(
                    bot,
                    effective_referrer,
                    user.id,
                    getattr(user, "username", None),
                )
            else:
                logger.info(
                    "SubGram fake-account warning ignored for non-referral user | user_id=%s chat_id=%s",
                    user.id,
                    chat_id,
                )
            flyer_allowed = await self._handle_flyer_fallback(
                bot,
                event,
                chat_id,
                user.id,
                getattr(user, "language_code", None),
            )
            if not flyer_allowed:
                return await self._maybe_call_subgram_handler(handler, event, data)
        if status in self.BLOCKING_STATUSES:
            handled = await self._handle_blocking_status(bot, event, chat_id, response, status)
            if handled:
                return None
            status = "ok"

        if status == "error":
            logger.warning("SubGram API responded with error: %s", response)
            await self._send_blocking_message(
                bot,
                event,
                chat_id,
                response.get("message")
                or "Ошибка при проверке заданий. Попробуйте еще раз позже.",
            )
            return await self._maybe_call_subgram_handler(handler, event, data)

        await db.set_flyer_verified(user.id, True)

        if not was_verified and flyer_allowed:
            await self._trigger_start(event, data)

        if self._is_subgram_callback(event):
            await self._acknowledge_callback(event)
            await self._cleanup_callback_message(event)
            return await handler(event, data)

        return await handler(event, data)

    async def _handle_flyer_fallback(
        self,
        bot: Any,
        event: TelegramObject,
        chat_id: int,
        user_id: int,
        language_code: Optional[str],
    ) -> bool:
        if self._flyer is None:
            logger.warning(
                "SubGram 404 fallback requested but Flyer client is not configured | user_id=%s",
                user_id,
            )
            return True

        try:
            is_allowed = await self._flyer.check(
                user_id,
                language_code=language_code,
                message=dict(self._flyer_message_template),
            )
        except APIError:
            logger.exception(
                "Flyer API error during fallback verification | user_id=%s",
                user_id,
            )
            return True
        except Exception:
            logger.exception(
                "Unexpected error during Flyer fallback verification | user_id=%s",
                user_id,
            )
            return True

        if is_allowed:
            logger.info(
                "Flyer fallback verification succeeded | user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )
            return True

        message_text = (
            "К сожалению мы не можем удостовериться, что ваш аккаунт не фейковый. "
            "Попробуйте позже или выполните задания, которые пришли вам отдельно."
        )
        await self._send_blocking_message(bot, event, chat_id, message_text)
        return False

    async def _reward_referrer_for_fake_warning(
        self,
        bot: Any,
        referrer_id: int,
        referral_id: int,
        referral_username: Optional[str],
        reward: int = 1,
    ) -> None:
        logger.info(
            "SubGram fake-account reward | referrer_id=%s referral_id=%s reward=%s",
            referrer_id,
            referral_id,
            reward,
        )

        awarded = False
        try:
            await db.update_balance(referrer_id, reward)
            awarded = True
        except Exception:
            logger.exception(
                "Failed to reward referrer for SubGram fake-account warning | referrer_id=%s",
                referrer_id,
            )
        if not awarded:
            return

        logger.info(
            "SubGram fake-account reward granted | referrer_id=%s referral_id=%s reward=%s",
            referrer_id,
            referral_id,
            reward,
        )

        referral_name = (
            f"@{referral_username}" if referral_username else f"ID {referral_id}"
        )
        message = (
            f"Ваш приглашенный пользователь {referral_name} не прошел проверку SubGram. "
            f"Вам начислена {reward} ⭐."
        )
        with suppress(TelegramBadRequest):
            await bot.send_message(referrer_id, message)

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
            logger.warning(
                "SubGram blocking status without prompt | status=%s response=%s",
                status,
                response,
            )
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
            logger.info(
                "SubGram remember context | created user_id=%s referred_by=%s",
                telegram_id,
                referred_by,
            )
            await db.create_user(telegram_id, 0, referred_by, username)
            return

        if username is not None and username != user_record.username:
            logger.info(
                "SubGram remember context | updating username user_id=%s old=%s new=%s",
                telegram_id,
                getattr(user_record, "username", None),
                username,
            )
            await db.update_username(telegram_id, username)
            with suppress(AttributeError):
                user_record.username = username

        if (
            referred_by is not None
            and referred_by != telegram_id
            and user_record.referred_by is None
        ):
            logger.info(
                "SubGram remember context | assigning referrer user_id=%s ref=%s",
                telegram_id,
                referred_by,
            )
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
            logger.exception("Failed to send SubGram task prompt to chat_id=%s", chat_id)
        return True

    def _get_language_block_message(self, language_code: Optional[str]) -> Optional[str]:
        if not language_code:
            return None

        normalized = language_code.lower()
        for prefix, message in self.LANGUAGE_BLOCK_MESSAGES.items():
            if normalized.startswith(prefix):
                return message
        return None

    def _build_blocking_prompt(
        self, response: Dict[str, Any], status: Optional[str]
    ) -> Optional[tuple[str, Any]]:
        if status == "warning":
            prompt = self._build_sponsor_prompt(response)
            if prompt is not None:
                return prompt
            logger.info(
                "SubGram warning status without sponsors | response=%s",
                response,
            )
            return None

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
            builder = InlineKeyboardBuilder()
            if reg_url:
                builder.button(
                    text="✅ Пройти регистрацию",
                    web_app=WebAppInfo(url=reg_url),
                )
            prompt = self._build_sponsor_prompt(
                response,
                builder=builder,
                base_text=response.get("message")
                or "Для продолжения, пожалуйста, пройдите быструю регистрацию.",
            )
            if prompt is not None:
                return prompt
            if not reg_url:
                return None
            builder.button(text="Продолжить", callback_data="subgram-op")
            builder.adjust(1)
            text = response.get("message") or (
                "Для продолжения, пожалуйста, пройдите быструю регистрацию."
            )
            return text, builder.as_markup()

        return None

    def _build_sponsor_prompt(
        self,
        response: Dict[str, Any],
        *,
        builder: Optional[InlineKeyboardBuilder] = None,
        base_text: Optional[str] = None,
    ) -> Optional[tuple[str, Any]]:
        additional = response.get("additional") or {}
        sponsors = additional.get("sponsors")
        if not isinstance(sponsors, list):
            return None

        local_builder = builder or InlineKeyboardBuilder()
        tasks: list[str] = []
        for sponsor in sponsors:
            if not isinstance(sponsor, dict):
                continue
            if not sponsor.get("available_now", True):
                logger.debug(
                    "Skipping unavailable sponsor | sponsor=%s", sponsor
                )
                continue
            if sponsor.get("status") not in {None, "unsubscribed"}:
                logger.debug(
                    "Skipping sponsor due to status | sponsor=%s", sponsor
                )
                continue
            link = sponsor.get("link")
            if not link:
                logger.debug(
                    "Skipping sponsor without link | sponsor=%s", sponsor
                )
                continue
            button_text = sponsor.get("button_text") or "Подписаться"
            local_builder.button(text=button_text, url=link)
            name = sponsor.get("resource_name") or button_text
            tasks.append(name)

        if not tasks:
            return None

        local_builder.button(text="✅ Я выполнил", callback_data="subgram-op")
        local_builder.adjust(1)

        message = base_text or response.get("message")
        task_lines = "\n".join(f"• {task}" for task in tasks)
        if message:
            text = (
                f"{message}\n\nЧтобы продолжить, выполните задания:\n{task_lines}\n\n"
                "После выполнения нажмите «✅ Я выполнил»."
            )
        else:
            text = (
                "Чтобы продолжить, выполните задания:\n"
                f"{task_lines}\n\nПосле выполнения нажмите «✅ Я выполнил»."
            )

        return text, local_builder.as_markup()

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
