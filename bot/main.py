import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import uvicorn

from .config import Settings, load_settings
from .database import db
from .handlers import register_handlers
from .middlewares import SubgramCheckMiddleware, ThrottlingMiddleware
from .subgram import SubgramClient
from .webhook import create_app


async def on_startup(bot: Bot) -> None:
    logging.info("Bot started as %s", (await bot.get_me()).username)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings: Settings = load_settings()

    await db.setup()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    subgram_client = SubgramClient(settings.subgram_api_key)

    dp = Dispatcher()
    dp.workflow_data.update(settings=settings, subgram_client=subgram_client)

    dp.message.middleware(SubgramCheckMiddleware(subgram_client))
    dp.callback_query.middleware(SubgramCheckMiddleware(subgram_client))
    dp.message.middleware(ThrottlingMiddleware(rate_limit=0.5))

    async def _close_subgram_client() -> None:
        await subgram_client.close()

    dp.shutdown.register(_close_subgram_client)

    register_handlers(dp)

    dp.startup.register(on_startup)

    app = create_app(bot, settings)
    config = uvicorn.Config(
        app,
        host=settings.webhook_host,
        port=settings.webhook_port,
        loop="asyncio",
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = False

    server_task = asyncio.create_task(server.serve())

    try:
        await dp.start_polling(bot)
    finally:
        if not server.should_exit:
            server.should_exit = True
        with suppress(asyncio.CancelledError):
            await server_task


if __name__ == "__main__":
    asyncio.run(main())
