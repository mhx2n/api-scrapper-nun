import asyncio
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import (
    BOT_TOKEN,
    OWNER_ID,
    TARGET_CHAT_ID,
    GITHUB_TOKEN,
    SCAN_INTERVAL,
    SEARCH_LIMIT,
    PORT,
    validate_config,
)

from scanner import GitHubScanner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

router = Router()

scanner = GitHubScanner(GITHUB_TOKEN)

seen = set()


def owner_only(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == OWNER_ID
    )


@router.message(Command("start"))
async def start_handler(message: Message):

    if not owner_only(message):
        return

    await message.answer(
        "🔐 Private GitHub Secret Monitor\n\n"
        "Status: 🟢 Online\n"
        "Access: 👑 Owner only\n\n"
        "/scan - Run scan\n"
        "/status - Show status"
    )


@router.message(Command("status"))
async def status_handler(message: Message):

    if not owner_only(message):
        return

    await message.answer(
        "🟢 Bot Status: Online\n"
        f"⏱ Scan interval: {SCAN_INTERVAL}s\n"
        f"📦 Seen findings: {len(seen)}"
    )


async def run_scan():

    queries = [
        '"xai-"',
        '"gsk_"',
        '"sk-ant-"',
        '"AIza"',
    ]

    results = await scanner.scan_queries(
        queries,
        SEARCH_LIMIT,
    )

    new_results = []

    for result in results:

        unique_id = (
            f"{result['repository']}:"
            f"{result['path']}"
        )

        if unique_id in seen:
            continue

        seen.add(unique_id)
        new_results.append(result)

    return new_results


async def send_findings(bot: Bot, findings):

    for finding in findings:

        text = (
            "🚨 <b>Potential Secret Exposure</b>\n\n"
            f"🏷 Type: <code>API credential</code>\n"
            f"📦 Repository: "
            f"<code>{finding['repository']}</code>\n"
            f"📄 File: "
            f"<code>{finding['path']}</code>\n\n"
            f"🔗 <a href=\"{finding['url']}\">"
            f"View source</a>\n\n"
            "⚠️ Secret value intentionally hidden."
        )

        try:
            await bot.send_message(
                TARGET_CHAT_ID,
                text,
            )

        except Exception:
            logger.exception(
                "Failed to send Telegram alert"
            )


async def scanner_loop(bot: Bot):

    await asyncio.sleep(10)

    while True:

        try:
            logger.info("Starting GitHub scan...")

            findings = await run_scan()

            if findings:
                await send_findings(
                    bot,
                    findings,
                )

                logger.info(
                    "New findings: %d",
                    len(findings),
                )
            else:
                logger.info(
                    "No new findings."
                )

        except Exception:
            logger.exception(
                "Scanner cycle failed"
            )

        await asyncio.sleep(
            SCAN_INTERVAL
        )


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "github-secret-monitor",
    })


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server running on port %s",
        PORT,
    )


async def main():

    validate_config()

    bot = Bot(BOT_TOKEN)

    dp = Dispatcher()

    dp.include_router(router)

    await start_health_server()

    asyncio.create_task(
        scanner_loop(bot)
    )

    logger.info(
        "Telegram bot started."
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
