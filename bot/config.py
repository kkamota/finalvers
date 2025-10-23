from dataclasses import dataclass
from typing import Sequence
import os


@dataclass(slots=True)
class Settings:
    bot_token: str
    channel_username: str
    admin_ids: Sequence[int]
    subgram_api_key: str
    min_withdrawal: int = 15
    start_bonus: int = 3
    referral_bonus: int = 3
    daily_bonus: int = 3
    webhook_host: str = "5.8.18.37"
    webhook_port: int = 50000


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "7968942203:AAGGFBRqSNWWpvAueFct54dQ3UthnJbPCRc")
    channel = os.getenv("CHANNEL_USERNAME", "@giftsauctionsru")
    raw_admins = os.getenv("ADMIN_IDS", "5838432507")
    admin_ids = tuple(
        int(admin_id.strip())
        for admin_id in raw_admins.split(",")
        if admin_id.strip().isdigit()
    )
    subgram_api_key = os.getenv(
        "SUBGRAM_API_KEY",
        "8f1d306a74aee60166624a41dde06af397c4161c2b1c72befd61472143077c5d",
    )
    webhook_host = os.getenv("WEBHOOK_HOST", "5.8.18.37")
    webhook_port = int(os.getenv("WEBHOOK_PORT", "50000"))

    return Settings(
        bot_token=token,
        channel_username=channel,
        admin_ids=admin_ids or (123456789,),
        subgram_api_key=subgram_api_key,
        webhook_host=webhook_host,
        webhook_port=webhook_port,
    )
