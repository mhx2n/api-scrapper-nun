import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

TARGET_CHAT_ID_RAW = os.getenv("TARGET_CHAT_ID", "").strip()

try:
    TARGET_CHAT_ID = int(TARGET_CHAT_ID_RAW)
except ValueError:
    TARGET_CHAT_ID = TARGET_CHAT_ID_RAW

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

SCAN_INTERVAL = max(
    300,
    int(os.getenv("SCAN_INTERVAL", "900"))
)

SEARCH_LIMIT = max(
    1,
    min(100, int(os.getenv("SEARCH_LIMIT", "30")))
)

PORT = int(os.getenv("PORT", "10000"))


def validate_config():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not OWNER_ID:
        missing.append("OWNER_ID")

    if not TARGET_CHAT_ID_RAW:
        missing.append("TARGET_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )
