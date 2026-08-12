import asyncio
import html
import logging
from collections import Counter
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
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


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# TELEGRAM
# =========================================================

router = Router()

scanner = GitHubScanner(GITHUB_TOKEN)

scan_lock = asyncio.Lock()

bot_paused = False

seen = set()

stats = {
    "total_scans": 0,
    "total_results": 0,
    "new_results": 0,
    "last_scan": None,
    "last_error": None,
}


# =========================================================
# SECURITY
# =========================================================

def owner_only(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == OWNER_ID
    )


# =========================================================
# SAFE HTML
# =========================================================

def esc(value) -> str:
    return html.escape(str(value))


# =========================================================
# /START
# =========================================================

@router.message(Command("start"))
async def start_handler(message: Message):

    if not owner_only(message):
        return

    status = "⏸ Paused" if bot_paused else "🟢 Running"

    await message.answer(
        "<b>🔐 PRIVATE GITHUB MONITOR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 Status: {status}\n"
        "👑 Access: Owner Only\n"
        "🛡 Secret values: REDACTED\n"
        "🧪 Live validation: Disabled\n\n"
        "<b>Commands</b>\n"
        "├ /scan — Run scan now\n"
        "├ /status — Bot status\n"
        "├ /stats — Scan statistics\n"
        "├ /pause — Pause automatic scans\n"
        "└ /resume — Resume automatic scans\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# /STATUS
# =========================================================

@router.message(Command("status"))
async def status_handler(message: Message):

    if not owner_only(message):
        return

    status = "⏸ Paused" if bot_paused else "🟢 Running"

    last_scan = (
        stats["last_scan"]
        or "Never"
    )

    await message.answer(
        "<b>📡 SYSTEM STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 Bot: {status}\n"
        "❤️ Health: OK\n"
        f"⏱ Interval: {SCAN_INTERVAL}s\n"
        f"🔎 Search limit: {SEARCH_LIMIT}\n"
        f"📦 Seen: {len(seen)}\n"
        f"🔄 Last scan: {esc(last_scan)}\n\n"
        "🔐 Secret values: REDACTED\n"
        "🧪 Live validation: Disabled\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# /STATS
# =========================================================

@router.message(Command("stats"))
async def stats_handler(message: Message):

    if not owner_only(message):
        return

    await message.answer(
        "<b>📊 SCANNER STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔄 Total scans: {stats['total_scans']}\n"
        f"📦 Total results: {stats['total_results']}\n"
        f"🆕 New results: {stats['new_results']}\n"
        f"🗂 Tracked findings: {len(seen)}\n\n"
        f"🕐 Last scan:\n"
        f"{esc(stats['last_scan'] or 'Never')}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# /PAUSE
# =========================================================

@router.message(Command("pause"))
async def pause_handler(message: Message):

    global bot_paused

    if not owner_only(message):
        return

    bot_paused = True

    await message.answer(
        "⏸ <b>Automatic scanning paused.</b>\n\n"
        "Use /resume to continue."
    )


# =========================================================
# /RESUME
# =========================================================

@router.message(Command("resume"))
async def resume_handler(message: Message):

    global bot_paused

    if not owner_only(message):
        return

    bot_paused = False

    await message.answer(
        "▶️ <b>Automatic scanning resumed.</b>"
    )


# =========================================================
# SCAN
# =========================================================

async def run_scan():

    global stats

    if scan_lock.locked():
        logger.warning(
            "Scan already running."
        )
        return []

    async with scan_lock:

        logger.info(
            "Starting GitHub scan..."
        )

        queries = [
            {
                "provider": "xAI",
                "query": '"xai-"',
            },
            {
                "provider": "Groq",
                "query": '"gsk_"',
            },
            {
                "provider": "Anthropic",
                "query": '"sk-ant-"',
            },
            {
                "provider": "Google",
                "query": '"AIza"',
            },
            {
                "provider": "OpenAI",
                "query": '"sk-"',
            },
            {
                "provider": "GitHub",
                "query": '"ghp_"',
            },
        ]

        results = await scanner.scan_queries(
            queries,
            SEARCH_LIMIT,
        )

        stats["total_scans"] += 1
        stats["total_results"] += len(results)

        new_results = []

        for result in results:

            unique_id = (
                f"{result['provider']}|"
                f"{result['repository']}|"
                f"{result['path']}|"
                f"{result.get('fingerprint', '')}"
            )

            if unique_id in seen:
                continue

            seen.add(unique_id)

            new_results.append(
                result
            )

        stats["new_results"] += len(
            new_results
        )

        stats["last_scan"] = (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
        )

        logger.info(
            "Scan complete. Results=%d New=%d",
            len(results),
            len(new_results),
        )

        return new_results


# =========================================================
# FORMAT FINDING
# =========================================================

def format_finding(finding) -> str:

    provider = esc(
        finding.get(
            "provider",
            "Unknown"
        )
    )

    repository = esc(
        finding.get(
            "repository",
            "unknown"
        )
    )

    path = esc(
        finding.get(
            "path",
            "unknown"
        )
    )

    url = finding.get(
        "url",
        ""
    )

    fingerprint = esc(
        finding.get(
            "fingerprint",
            "N/A"
        )
    )

    return (
        "🚨 <b>POTENTIAL CREDENTIAL EXPOSURE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🏷 <b>Provider:</b> "
        f"<code>{provider}</code>\n"

        "🔑 <b>Type:</b> "
        "<code>API credential</code>\n"

        "📊 <b>Status:</b> "
        "<code>DETECTED</code>\n"

        "🧪 <b>Validation:</b> "
        "<code>NOT PERFORMED</code>\n\n"

        f"📦 <b>Repository:</b>\n"
        f"<code>{repository}</code>\n\n"

        f"📄 <b>File:</b>\n"
        f"<code>{path}</code>\n\n"

        f"🆔 <b>Fingerprint:</b>\n"
        f"<code>{fingerprint}</code>\n\n"

        f"🔗 <a href=\"{esc(url)}\">"
        f"Open source on GitHub</a>\n\n"

        "🔒 <b>Secret:</b> "
        "<code>REDACTED</code>\n"

        "⚠️ Credential value is intentionally hidden.\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# SUMMARY
# =========================================================

def format_summary(findings) -> str:

    providers = Counter(
        item.get(
            "provider",
            "Unknown"
        )
        for item in findings
    )

    lines = [
        "<b>📊 GITHUB SECURITY SCAN</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🆕 <b>New findings:</b> {len(findings)}",
        "",
        "<b>🏷 Providers detected</b>",
    ]

    for provider, count in providers.most_common():

        lines.append(
            f"├ {esc(provider)}: "
            f"<b>{count}</b>"
        )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🔐 Secret values: REDACTED",
        "🧪 Live validation: Disabled",
        "🛡 Duplicate filtering: Enabled",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ])

    return "\n".join(lines)


# =========================================================
# TELEGRAM SAFE SEND
# =========================================================

async def safe_send(
    bot: Bot,
    text: str,
):

    max_attempts = 5

    for attempt in range(
        max_attempts
    ):

        try:

            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=text,
                disable_web_page_preview=True,
            )

            # Small controlled delay.
            await asyncio.sleep(1.5)

            return True

        except TelegramRetryAfter as error:

            retry_after = int(
                error.retry_after
            ) + 2

            logger.warning(
                "Telegram flood control. "
                "Sleeping %s seconds.",
                retry_after,
            )

            await asyncio.sleep(
                retry_after
            )

        except TelegramBadRequest as error:

            logger.error(
                "Telegram BadRequest: %s",
                error,
            )

            return False

        except Exception:

            logger.exception(
                "Telegram send failed."
            )

            await asyncio.sleep(
                3
            )

    return False


# =========================================================
# SEND FINDINGS
# =========================================================

async def send_findings(
    bot: Bot,
    findings,
):

    if not findings:
        return

    # First send one summary.
    await safe_send(
        bot,
        format_summary(findings),
    )

    # Maximum findings per Telegram message.
    batch_size = 5

    for index in range(
        0,
        len(findings),
        batch_size,
    ):

        batch = findings[
            index:index + batch_size
        ]

        blocks = []

        for number, finding in enumerate(
            batch,
            start=index + 1,
        ):

            provider = esc(
                finding.get(
                    "provider",
                    "Unknown"
                )
            )

            repository = esc(
                finding.get(
                    "repository",
                    "unknown"
                )
            )

            path = esc(
                finding.get(
                    "path",
                    "unknown"
                )
            )

            url = finding.get(
                "url",
                ""
            )

            fingerprint = esc(
                finding.get(
                    "fingerprint",
                    "N/A"
                )
            )

            block = (
                f"<b>#{number} • "
                f"{provider}</b>\n"
                f"📦 <code>{repository}</code>\n"
                f"📄 <code>{path}</code>\n"
                f"🆔 <code>{fingerprint}</code>\n"
                f"🔗 <a href=\"{esc(url)}\">"
                f"Source</a>\n"
                "🔒 Secret: <code>REDACTED</code>"
            )

            blocks.append(block)

        text = (
            "<b>🔎 FINDINGS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n\n".join(blocks)
            + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        await safe_send(
            bot,
            text,
        )


# =========================================================
# MANUAL SCAN
# =========================================================

@router.message(Command("scan"))
async def scan_handler(message: Message):

    if not owner_only(message):
        return

    if scan_lock.locked():

        await message.answer(
            "⏳ <b>Scan already running.</b>\n\n"
            "Please wait."
        )

        return

    await message.answer(
        "🔎 <b>Starting GitHub scan...</b>\n\n"
        "Please wait."
    )

    try:

        findings = await run_scan()

        if findings:

            await send_findings(
                message.bot,
                findings,
            )

            await message.answer(
                "✅ <b>Scan completed.</b>\n\n"
                f"🆕 New findings: "
                f"<b>{len(findings)}</b>"
            )

        else:

            await message.answer(
                "✅ <b>Scan completed.</b>\n\n"
                "🟢 No new findings."
            )

    except Exception:

        logger.exception(
            "Manual scan failed."
        )

        await message.answer(
            "❌ <b>Scan failed.</b>\n\n"
            "Check Render logs."
        )


# =========================================================
# BACKGROUND SCANNER
# =========================================================

async def scanner_loop(bot: Bot):

    await asyncio.sleep(15)

    while True:

        try:

            if bot_paused:

                logger.info(
                    "Scanner paused."
                )

            else:

                findings = await run_scan()

                if findings:

                    await send_findings(
                        bot,
                        findings,
                    )

                else:

                    logger.info(
                        "No new findings."
                    )

        except asyncio.CancelledError:

            logger.info(
                "Scanner task cancelled."
            )

            raise

        except Exception as error:

            stats["last_error"] = str(
                error
            )

            logger.exception(
                "Scanner cycle failed."
            )

        await asyncio.sleep(
            SCAN_INTERVAL
        )


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service": "github-secret-monitor",
            "bot": "running",
            "scanner": (
                "paused"
                if bot_paused
                else "running"
            ),
        }
    )


# =========================================================
# HEALTH SERVER
# =========================================================

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

    runner = web.AppRunner(
        app
    )

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

    return runner


# =========================================================
# MAIN
# =========================================================

async def main():

    validate_config()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dp = Dispatcher()

    dp.include_router(
        router
    )

    health_runner = (
        await start_health_server()
    )

    scanner_task = asyncio.create_task(
        scanner_loop(bot)
    )

    logger.info(
        "Telegram bot started."
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=(
                dp.resolve_used_update_types()
            ),
        )

    finally:

        scanner_task.cancel()

        try:

            await scanner_task

        except asyncio.CancelledError:

            pass

        await health_runner.cleanup()

        await bot.session.close()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped."
        )
