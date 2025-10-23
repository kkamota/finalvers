from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SubgramClient:
    """Асинхронный клиент SubGram для проверки обязательных подписок."""

    _BASE_URL = "https://api.subgram.org"
    _GET_SPONSORS_PATH = "/get-sponsors"

    def __init__(self, api_key: str, *, request_timeout: float = 10.0) -> None:
        self.api_key = api_key
        self._request_timeout = request_timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self._request_timeout)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def get_sponsors(
        self,
        user_id: int,
        chat_id: int,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {"user_id": user_id, "chat_id": chat_id}
        payload.update({key: value for key, value in kwargs.items() if value is not None})

        logger.info(
            "SubGram request prepared | user_id=%s chat_id=%s payload=%s",
            user_id,
            chat_id,
            json.dumps(payload, ensure_ascii=False),
        )

        headers = {"Auth": self.api_key}
        session = await self._get_session()

        try:
            async with session.post(
                f"{self._BASE_URL}{self._GET_SPONSORS_PATH}",
                headers=headers,
                json=payload,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except aiohttp.ContentTypeError:
                    text = await response.text()
                    logger.error(
                        "SubGram API returned non-JSON response (status %s): %s",
                        response.status,
                        text,
                    )
                    return None

                logger.info(
                    "SubGram response received | user_id=%s chat_id=%s http_status=%s body=%s",
                    user_id,
                    chat_id,
                    response.status,
                    json.dumps(data, ensure_ascii=False),
                )
                if response.status >= 400:
                    logger.warning(
                        "SubGram API responded with HTTP %s: %s",
                        response.status,
                        data,
                    )
                return data
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout while requesting SubGram sponsors for user_id=%s", user_id
            )
        except aiohttp.ClientError:
            logger.exception(
                "HTTP error while requesting SubGram sponsors for user_id=%s", user_id
            )
        except Exception:
            logger.exception(
                "Unexpected error while requesting SubGram sponsors for user_id=%s",
                user_id,
            )
        return None
