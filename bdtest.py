import asyncio
import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from tqdm import tqdm

DB_PATH = "bot_data.sqlite3"  # путь к вашей базе
BOT_TOKEN = "7937912782:AAHTYpDRhG5rwe8CalAjfzVlXwkAPNIgwa0"  # вставьте токен бота
# Список каналов, на которые пользователь обязан быть подписан:
REQUIRED_CHANNELS = ["@giftsauctionsru"]

# Имя вашей таблицы с пользователями и названия колонок:
USERS_TABLE = "users"
USER_ID_COL = "telegram_id"      # телеграм id пользователя
IS_SUB_COL = "is_subscribed"     # флаг подписки (0/1)

# Если у вас хранится реферер в users.referrer_id — это стандартно
REFERRER_COL = "referrer_id"     # кто пригласил (telegram_id пригласившего)

# --- Вспомогательные функции ---

async def ensure_schema(conn: aiosqlite.Connection):
    """
    Добавляет колонку is_subscribed, если её нет.
    """
    # Проверим, есть ли колонка
    cursor = await conn.execute(f"PRAGMA table_info({USERS_TABLE});")
    cols = [row[1] async for row in cursor]  # row[1] = name
    await cursor.close()

    if IS_SUB_COL not in cols:
        # Добавляем колонку с дефолтом 0
        await conn.execute(f"ALTER TABLE {USERS_TABLE} ADD COLUMN {IS_SUB_COL} INTEGER NOT NULL DEFAULT 0;")
        await conn.commit()

async def get_all_user_ids(conn: aiosqlite.Connection):
    """
    Получить всех пользователей (telegram_id).
    """
    cursor = await conn.execute(f"SELECT {USER_ID_COL} FROM {USERS_TABLE};")
    rows = await cursor.fetchall()
    await cursor.close()
    return [r[0] for r in rows]

async def is_user_member_of_all(bot: Bot, user_id: int | str) -> bool:
    """
    Проверить, состоит ли user во всех каналах из REQUIRED_CHANNELS.
    Считаем подписанным, если статус не 'left' и не 'kicked'.
    """
    for chan in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=chan, user_id=int(user_id))
            # Статусы, считающиеся ОК: 'creator','administrator','member','restricted'(если не покинул)
            status = getattr(member, "status", None)
            if status in ("left", "kicked"):
                return False
        except TelegramAPIError:
            # Если бот не может проверить (например, не админ/не член приватного канала), считаем не подписан.
            # Можно заменить логикой "continue" при другой политике.
            return False
    return True

async def update_subscription_flag(conn: aiosqlite.Connection, user_id: int | str, is_sub: bool):
    await conn.execute(
        f"UPDATE {USERS_TABLE} SET {IS_SUB_COL} = ? WHERE {USER_ID_COL} = ?;",
        (1 if is_sub else 0, user_id),
    )

async def main():
    bot = Bot(BOT_TOKEN)
    async with aiosqlite.connect(DB_PATH) as conn:
        # Ускорим апдейты
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")

        await ensure_schema(conn)

        user_ids = await get_all_user_ids(conn)
        print(f"Найдено пользователей: {len(user_ids)}")

        # Прогресс-бар
        for uid in tqdm(user_ids, total=len(user_ids), desc="Проверяем подписки", unit="польз.", ncols=80):
            # На всякий случай нормализуем к int
            try:
                uid_int = int(uid)
            except Exception:
                uid_int = uid

            # Проверка подписки
            is_sub = await is_user_member_of_all(bot, uid_int)
            await update_subscription_flag(conn, uid, is_sub)

            # Периодически коммитим и делаем небольшую паузу, чтобы не упереться в лимиты
            await conn.commit()
            await asyncio.sleep(0.1)

        # Финальный коммит
        await conn.commit()

    await bot.session.close()
    print("Готово. Все пользователи проверены и помечены в БД.")

if __name__ == "__main__":
    asyncio.run(main())
