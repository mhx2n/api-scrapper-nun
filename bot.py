import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
import threading

from flask import Flask
from dotenv import load_dotenv
import requests
import aiohttp
from cachetools import TTLCache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# লোড এনভায়রনমেন্ট ভেরিয়েবল
load_dotenv()

# কনফিগারেশন
BOT_TOKEN = os.getenv('BOT_TOKEN')
PRIVATE_GROUP_ID = int(os.getenv('PRIVATE_GROUP_ID')) if os.getenv('PRIVATE_GROUP_ID') else None
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID')) if os.getenv('ADMIN_USER_ID') else None
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ক্যাশে সেটআপ
cache = TTLCache(maxsize=1000, ttl=300)

# ফ্লাস্ক অ্যাপ
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "True", 200

@flask_app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}, 200

def run_flask():
    """ফ্লাস্ক অ্যাপ চালানোর জন্য"""
    flask_app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

# GitHub API ক্লায়েন্ট
class GitHubAPIClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.base_url = 'https://api.github.com'
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers=self.headers)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def get_live_repos(self) -> List[Dict]:
        """সকল লাইভ রিপোজিটরি খুঁজে বের করে"""
        live_repos = []
        try:
            urls = [
                f'{self.base_url}/search/repositories?q=stars:>1&sort=updated&order=desc&per_page=100',
                f'{self.base_url}/search/repositories?q=pushed:>{datetime.now() - timedelta(days=1)}&sort=updated&order=desc&per_page=100',
                f'{self.base_url}/search/repositories?q=language:python&sort=updated&order=desc&per_page=50',
                f'{self.base_url}/search/repositories?q=language:javascript&sort=updated&order=desc&per_page=50',
            ]

            for url in urls:
                try:
                    async with self.session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            repos = data.get('items', [])
                            for repo in repos:
                                if self._is_live_repo(repo):
                                    live_repos.append(repo)
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error fetching from {url}: {e}")
                    continue

            # ডুপ্লিকেট রিমুভ
            seen = set()
            unique_repos = []
            for repo in live_repos:
                repo_id = repo.get('id')
                if repo_id and repo_id not in seen:
                    seen.add(repo_id)
                    unique_repos.append(repo)

            return unique_repos[:50]

        except Exception as e:
            logger.error(f"GitHub API error: {e}")
            return []

    def _is_live_repo(self, repo: Dict) -> bool:
        """রিপোজিটরি লাইভ কিনা চেক করে"""
        try:
            updated_at = repo.get('updated_at')
            if updated_at:
                updated_at = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                if datetime.now().astimezone() - updated_at < timedelta(hours=24):
                    return True

            if repo.get('stargazers_count', 0) > 1000:
                return True

            if repo.get('open_issues_count', 0) > 50:
                return True

            pushed_at = repo.get('pushed_at')
            if pushed_at:
                pushed_at = datetime.fromisoformat(pushed_at.replace('Z', '+00:00'))
                if datetime.now().astimezone() - pushed_at < timedelta(hours=12):
                    return True

            return False

        except Exception as e:
            logger.error(f"Error checking live status: {e}")
            return False

# বট ক্লাস
class TelegramBot:
    def __init__(self, token: str, group_id: int, admin_id: int):
        self.token = token
        self.group_id = group_id
        self.admin_id = admin_id
        self.last_sent_repos: Set[int] = set()
        self.application = None
        self.github_client = GitHubAPIClient(GITHUB_TOKEN)
        self.is_running = True

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/start কমান্ড হ্যান্ডলার"""
        if not update.effective_user:
            return
            
        if update.effective_user.id != self.admin_id:
            await update.message.reply_text("⛔ আপনি এই বট ব্যবহার করার অনুমতি পাবেন না!")
            return

        keyboard = [
            [InlineKeyboardButton("📊 লাইভ রিপোজিটরি দেখুন", callback_data='view_live')],
            [InlineKeyboardButton("🔄 ম্যানুয়াল স্ক্র্যাপ", callback_data='manual_scrape')],
            [InlineKeyboardButton("📈 পরিসংখ্যান", callback_data='stats')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🚀 **GitHub লাইভ API স্ক্র্যাপার বট**\n\n"
            "এই বট GitHub থেকে লাইভ রিপোজিটরি স্ক্র্যাপ করে এবং "
            "আপনার প্রাইভেট গ্রুপে পাঠায়।\n\n"
            "🔹 স্বয়ংক্রিয়ভাবে প্রতি ১ ঘন্টায় স্ক্র্যাপ হয়\n"
            "🔹 শুধুমাত্র লাইভ এবং সক্রিয় রিপোজিটরি\n"
            "🔹 সম্পূর্ণ প্রাইভেট এবং সুরক্ষিত\n\n"
            "নিচের বাটন ব্যবহার করে নিয়ন্ত্রণ করুন:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """বাটন কলব্যাক হ্যান্ডলার"""
        if not update.effective_user or not update.callback_query:
            return
            
        query = update.callback_query
        await query.answer()

        if update.effective_user.id != self.admin_id:
            await query.edit_message_text("⛔ আপনি এই অপারেশন করতে পারবেন না!")
            return

        if query.data == 'view_live':
            await self.view_live_repos(query)
        elif query.data == 'manual_scrape':
            await self.manual_scrape(query)
        elif query.data == 'stats':
            await self.show_stats(query)

    async def view_live_repos(self, query):
        """লাইভ রিপোজিটরি দেখায়"""
        await query.edit_message_text("🔄 লাইভ রিপোজিটরি খোঁজা হচ্ছে...")
        
        try:
            async with self.github_client as client:
                repos = await client.get_live_repos()
                
                if not repos:
                    await query.edit_message_text("❌ কোনো লাইভ রিপোজিটরি পাওয়া যায়নি!")
                    return

                # শুধু নতুন রিপোজিটরি
                new_repos = [r for r in repos if r.get('id') not in self.last_sent_repos]
                
                if not new_repos:
                    await query.edit_message_text("📭 কোনো নতুন লাইভ রিপোজিটরি পাওয়া যায়নি!")
                    return

                # গ্রুপে পাঠান
                sent_count = 0
                for repo in new_repos[:10]:
                    message = self._format_repo_message(repo)
                    await self.send_message_to_group(message)
                    self.last_sent_repos.add(repo.get('id'))
                    sent_count += 1
                    await asyncio.sleep(0.5)

                await query.edit_message_text(f"✅ {sent_count}টি নতুন লাইভ রিপোজিটরি পাঠানো হয়েছে!")

        except Exception as e:
            logger.error(f"Error in view_live_repos: {e}")
            await query.edit_message_text(f"❌ ত্রুটি: {str(e)}")

    async def manual_scrape(self, query):
        """ম্যানুয়াল স্ক্র্যাপ"""
        await query.edit_message_text("🔄 স্ক্র্যাপিং শুরু হচ্ছে...")
        await self.view_live_repos(query)

    async def show_stats(self, query):
        """পরিসংখ্যান দেখায়"""
        stats = f"""
📊 **বট পরিসংখ্যান**

🤖 বট স্ট্যাটাস: ✅ চালু
📁 মোট ক্যাশেড রিপো: {len(cache)}
🔄 শেষ স্ক্র্যাপ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
👑 অ্যাডমিন আইডি: {self.admin_id}

🔹 প্রতি ১ ঘন্টায় স্বয়ংক্রিয় স্ক্র্যাপ
🔹 সর্বোচ্চ ৫০টি রিপোজিটরি প্রতি বার
🔹 শুধুমাত্র লাইভ রিপোজিটরি পাঠানো হয়
        """
        await query.edit_message_text(stats, parse_mode='Markdown')

    def _format_repo_message(self, repo: Dict) -> str:
        """রিপোজিটরি ফরম্যাট করে"""
        name = repo.get('full_name', 'N/A')
        description = repo.get('description', 'No description') or 'No description'
        stars = repo.get('stargazers_count', 0)
        forks = repo.get('forks_count', 0)
        language = repo.get('language', 'Unknown')
        url = repo.get('html_url', '#')
        updated_at = repo.get('updated_at', 'N/A')
        open_issues = repo.get('open_issues_count', 0)

        return f"""
🚀 **{name}**

📝 {description[:200]}...

⭐ {stars} Stars | 🍴 {forks} Forks
💻 Language: {language}
🐛 Open Issues: {open_issues}
🔄 Updated: {updated_at[:10]}

🔗 [View on GitHub]({url})
"""

    async def send_message_to_group(self, message: str):
        """গ্রুপে মেসেজ পাঠায়"""
        try:
            if self.application and self.application.bot:
                await self.application.bot.send_message(
                    chat_id=self.group_id,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Error sending message to group: {e}")

    async def scheduled_scrape(self):
        """শিডিউল্ড স্ক্র্যাপ"""
        while self.is_running:
            try:
                logger.info("Starting scheduled scrape...")
                async with self.github_client as client:
                    repos = await client.get_live_repos()
                    
                    if repos:
                        new_repos = [r for r in repos if r.get('id') not in self.last_sent_repos]
                        
                        if new_repos:
                            for repo in new_repos[:10]:
                                message = self._format_repo_message(repo)
                                await self.send_message_to_group(message)
                                self.last_sent_repos.add(repo.get('id'))
                                await asyncio.sleep(0.5)
                            
                            logger.info(f"Sent {len(new_repos[:10])} new live repos")
                    else:
                        logger.info("No live repos found")
                        
            except Exception as e:
                logger.error(f"Error in scheduled scrape: {e}")
                
            await asyncio.sleep(3600)  # ১ ঘন্টা

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """এরর হ্যান্ডলার"""
        logger.error(f"Update {update} caused error {context.error}")
        if update and update.effective_user and update.effective_user.id == self.admin_id:
            try:
                await update.message.reply_text(f"❌ ত্রুটি: {str(context.error)}")
            except:
                pass

    async def post_init(self, application: Application):
        """বট স্টার্ট হওয়ার পর"""
        self.application = application
        
        # স্টার্টআপ মেসেজ
        await asyncio.sleep(3)
        await self.send_message_to_group(
            "🚀 **GitHub লাইভ API স্ক্র্যাপার বট চালু হয়েছে!**\n\n"
            "🔹 প্রতি ১ ঘন্টায় স্বয়ংক্রিয়ভাবে স্ক্র্যাপ হবে\n"
            "🔹 লাইভ রিপোজিটরি এই গ্রুপে পাঠানো হবে\n"
            "🔹 /start কমান্ড দিয়ে কন্ট্রোল প্যানেল দেখুন"
        )
        
        # শিডিউলড টাস্ক শুরু
        asyncio.create_task(self.scheduled_scrape())

    def run(self):
        """বট রান করে"""
        try:
            # অ্যাপ্লিকেশন তৈরি
            self.application = Application.builder().token(self.token).build()

            # হ্যান্ডলার যোগ
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.start_command))
            self.application.add_handler(CallbackQueryHandler(self.button_callback))
            
            # এরর হ্যান্ডলার
            self.application.add_error_handler(self.error_handler)

            # পোস্ট ইনিট
            self.application.post_init = self.post_init

            logger.info("Bot started successfully!")
            
            # বট রান (পোলিং)
            self.application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise

# মেইন ফাংশন
async def main():
    """মেইন ফাংশন"""
    try:
        # ফ্লাস্ক থ্রেড শুরু
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("Flask server started on port 8080")

        # চেক ভেরিয়েবল
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN not set in environment variables")
        if not PRIVATE_GROUP_ID:
            raise ValueError("PRIVATE_GROUP_ID not set in environment variables")
        if not ADMIN_USER_ID:
            raise ValueError("ADMIN_USER_ID not set in environment variables")
        if not GITHUB_TOKEN:
            raise ValueError("GITHUB_TOKEN not set in environment variables")

        # বট চালু
        bot = TelegramBot(BOT_TOKEN, PRIVATE_GROUP_ID, ADMIN_USER_ID)
        bot.run()
        
    except Exception as e:
        logger.error(f"Main error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
