from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

_DB_PATH = Path("bot_data.sqlite3")


@dataclass(slots=True)
class User:
    telegram_id: int
    balance: int
    referred_by: Optional[int]
    is_subscribed: bool
    reward_claimed: bool
    last_daily_bonus: Optional[str]
    username: Optional[str]
    is_banned: bool
    start_bonus_claimed: bool
    flyer_verified: int


@dataclass(slots=True)
class WithdrawalRequest:
    id: int
    telegram_id: int
    amount: int
    status: str
    created_at: str


class Database:
    def __init__(self, path: Path | str = _DB_PATH) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL,
                referred_by INTEGER,
                is_subscribed INTEGER NOT NULL DEFAULT 0,
                reward_claimed INTEGER NOT NULL DEFAULT 0,
                last_daily_bonus TEXT,
                username TEXT,
                is_banned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await self._ensure_column("users", "username", "TEXT")
        await self._ensure_column("users", "is_banned", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column(
            "users",
            "start_bonus_claimed",
            "INTEGER NOT NULL DEFAULT 1",
        )
        await self._ensure_column(
            "users",
            "flyer_verified",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    async def get_user(self, telegram_id: int) -> Optional[User]:
        row = await self._fetchone(
            "SELECT telegram_id, balance, referred_by, is_subscribed, reward_claimed, last_daily_bonus, username, is_banned, start_bonus_claimed, flyer_verified FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        if row is None:
            return None
        return User(
            telegram_id=row[0],
            balance=row[1],
            referred_by=row[2],
            is_subscribed=bool(row[3]),
            reward_claimed=bool(row[4]),
            last_daily_bonus=row[5],
            username=row[6],
            is_banned=bool(row[7]),
            start_bonus_claimed=bool(row[8]),
            flyer_verified=bool(row[9]),
        )

    async def get_user_by_username(self, username: str) -> Optional[User]:
        row = await self._fetchone(
            """
            SELECT telegram_id, balance, referred_by, is_subscribed, reward_claimed,
                   last_daily_bonus, username, is_banned, start_bonus_claimed, flyer_verified
            FROM users
            WHERE username = ? COLLATE NOCASE
            """,
            (username,),
        )
        if row is None:
            return None
        return User(
            telegram_id=row[0],
            balance=row[1],
            referred_by=row[2],
            is_subscribed=bool(row[3]),
            reward_claimed=bool(row[4]),
            last_daily_bonus=row[5],
            username=row[6],
            is_banned=bool(row[7]),
            start_bonus_claimed=bool(row[8]),
            flyer_verified=bool(row[9]),
        )

    async def create_user(
        self,
        telegram_id: int,
        initial_balance: int,
        referred_by: Optional[int],
        username: Optional[str],
    ) -> None:
        await self._execute(
            """
            INSERT OR IGNORE INTO users (
                telegram_id,
                balance,
                referred_by,
                username,
                start_bonus_claimed
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                initial_balance,
                referred_by,
                username,
                int(initial_balance > 0),
            ),
        )

    async def assign_referrer(self, telegram_id: int, referred_by: Optional[int]) -> None:
        await self._execute(
            "UPDATE users SET referred_by = ? WHERE telegram_id = ? AND referred_by IS NULL",
            (referred_by, telegram_id),
        )

    async def update_username(self, telegram_id: int, username: Optional[str]) -> None:
        await self._execute(
            "UPDATE users SET username = ? WHERE telegram_id = ?",
            (username, telegram_id),
        )

    async def update_balance(self, telegram_id: int, delta: int) -> None:
        await self._execute(
            "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
            (delta, telegram_id),
        )

    async def set_subscription(self, telegram_id: int, subscribed: bool) -> None:
        await self._execute(
            "UPDATE users SET is_subscribed = ? WHERE telegram_id = ?",
            (int(subscribed), telegram_id),
        )

    async def mark_reward_claimed(self, telegram_id: int) -> None:
        await self.set_reward_claimed(telegram_id, True)

    async def set_reward_claimed(self, telegram_id: int, claimed: bool) -> None:
        await self._execute(
            "UPDATE users SET reward_claimed = ? WHERE telegram_id = ?",
            (int(claimed), telegram_id),
        )

    async def set_start_bonus_claimed(self, telegram_id: int, claimed: bool) -> None:
        await self._execute(
            "UPDATE users SET start_bonus_claimed = ? WHERE telegram_id = ?",
            (int(claimed), telegram_id),
        )

    async def set_flyer_verified(self, telegram_id: int, verified: bool) -> None:
        await self._execute(
            "UPDATE users SET flyer_verified = ? WHERE telegram_id = ?",
            (int(verified), telegram_id),
        )

    async def set_last_daily_bonus(self, telegram_id: int, timestamp: str | None) -> None:
        await self._execute(
            "UPDATE users SET last_daily_bonus = ? WHERE telegram_id = ?",
            (timestamp, telegram_id),
        )

    async def list_top_referrers(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = await self._fetchall(
            """
            SELECT referred_by, COUNT(*) as total
            FROM users
            WHERE referred_by IS NOT NULL
                AND is_subscribed = 1
                AND flyer_verified = 1
            GROUP BY referred_by
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row[0], row[1]) for row in rows if row[0] is not None]

    async def add_withdrawal(self, telegram_id: int, amount: int) -> None:
        await self._execute(
            "INSERT INTO withdrawals (telegram_id, amount) VALUES (?, ?)",
            (telegram_id, amount),
        )

    async def list_referrals(self, referrer_id: int, *, limit: Optional[int] = None) -> list[tuple[int, Optional[str]]]:
            # Если есть created_at — сортируй по нему, иначе по telegram_id как прокси «новизны»
            sql = """
                SELECT telegram_id, username
                FROM users
                WHERE referred_by = ?
                ORDER BY rowid DESC
            """
            params = [referrer_id]
            if limit:
                sql += " LIMIT ?"
                params.append(limit)
            rows = await self._fetchall(sql, tuple(params))
            return [(row[0], row[1]) for row in rows]

    async def list_withdrawals(self, status: Optional[str] = None) -> list[WithdrawalRequest]:
        if status:
            query = "SELECT id, telegram_id, amount, status, created_at FROM withdrawals WHERE status = ? ORDER BY created_at DESC"
            params: Iterable[Any] = (status,)
        else:
            query = "SELECT id, telegram_id, amount, status, created_at FROM withdrawals ORDER BY created_at DESC"
            params = ()
        rows = await self._fetchall(query, params)
        return [
            WithdrawalRequest(
                id=row[0],
                telegram_id=row[1],
                amount=row[2],
                status=row[3],
                created_at=row[4],
            )
            for row in rows
        ]

    async def get_withdrawal(self, request_id: int) -> Optional[WithdrawalRequest]:
        row = await self._fetchone(
            "SELECT id, telegram_id, amount, status, created_at FROM withdrawals WHERE id = ?",
            (request_id,),
        )
        if row is None:
            return None
        return WithdrawalRequest(
            id=row[0],
            telegram_id=row[1],
            amount=row[2],
            status=row[3],
            created_at=row[4],
        )

    async def set_withdrawal_status(self, request_id: int, status: str) -> None:
        await self._execute(
            "UPDATE withdrawals SET status = ? WHERE id = ?",
            (status, request_id),
        )

    async def list_all_users(self) -> list[User]:
        rows = await self._fetchall(
            """
            SELECT telegram_id, balance, referred_by, is_subscribed, reward_claimed, last_daily_bonus, username, is_banned, start_bonus_claimed, flyer_verified
            FROM users
            ORDER BY telegram_id
            """,
        )
        return [
            User(
                telegram_id=row[0],
                balance=row[1],
                referred_by=row[2],
                is_subscribed=bool(row[3]),
                reward_claimed=bool(row[4]),
                last_daily_bonus=row[5],
                username=row[6],
                is_banned=bool(row[7]),
                start_bonus_claimed=bool(row[8]),
                flyer_verified=bool(row[9]),
            )
            for row in rows
        ]

    async def set_ban_status(self, telegram_id: int, banned: bool) -> None:
        await self._execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
            (int(banned), telegram_id),
        )

    async def count_users(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM users")
        return int(row[0]) if row else 0

    async def sum_balances(self) -> int:
        row = await self._fetchone("SELECT COALESCE(SUM(balance), 0) FROM users")
        return int(row[0]) if row else 0

    async def _execute(self, query: str, params: Iterable[Any] | None = None) -> None:
        async with self._locked_connection() as conn:
            conn.execute(query, tuple(params) if params else ())
            conn.commit()

    async def _fetchone(self, query: str, params: Iterable[Any] | None = None) -> Optional[tuple[Any, ...]]:
        async with self._locked_connection() as conn:
            cursor = conn.execute(query, tuple(params) if params else ())
            row = cursor.fetchone()
            return row

    async def _fetchall(self, query: str, params: Iterable[Any] | None = None) -> list[tuple[Any, ...]]:
        async with self._locked_connection() as conn:
            cursor = conn.execute(query, tuple(params) if params else ())
            rows = cursor.fetchall()
            return rows

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = await self._fetchall(f"PRAGMA table_info({table})")
        if not any(row[1] == column for row in columns):
            await self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @asynccontextmanager
    async def _locked_connection(self):
        async with self._lock:
            connection = await asyncio.to_thread(self._connect)
            try:
                yield connection
            finally:
                await asyncio.to_thread(connection.close)

    def _connect(self) -> sqlite3.Connection:
        # Добавляем таймаут и включаем WAL
        connection = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
        connection.row_factory = sqlite3.Row

        # Включаем WAL-режим — это позволяет нескольким процессам писать одновременно
        connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA synchronous=NORMAL;")

        return connection



db = Database()
