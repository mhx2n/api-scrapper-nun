import asyncio
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional

from flask import Flask
from dotenv import load_dotenv
import requests
import aiohttp
from cachetools import TTLCache

# python-telegram-bot 13.x এর ইম্পোর্ট
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

# লোড এনভায়রনমেন্ট
load_dotenv()

# কনফিগারেশন
BOT_TOKEN = os.getenv('BOT_TOKEN')
PRIVATE_GROUP_ID = int(os.getenv('PRIVATE_GROUP_ID')) if os.getenv('PRIVATE_GROUP_ID') else None
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID')) if os.getenv('ADMIN_USER_ID') else None
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')

# লগিং
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ক্যাশে
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
    """ফ্লাস্ক অ্যাপ চালু"""
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

    async def get_live_repos(self) -> List[Dict]:
        """লাইভ রিপোজিটরি সংগ্রহ"""
        live_repos = []
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                urls = [
                    f'{self.base_url}/search/repositories?q=stars:>1&sort=updated&order=desc&per_page=50',
                    f'{self.base_url}/search/repositories?q=pushed:>{datetime.now() - timedelta(days=1)}&sort=updated&order=desc&per_page=50',
                ]

                for url in urls:
                    try:
                        async with session.get(url) as response:
                            if response.status == 200:
                                data = await response.json()
                                repos = data.get('items', [])
                                for repo in repos:
                                    if self._is_live_repo(repo):
                                        live_repos.append(repo)
                            await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error(f"Error fetching {url}: {e}")
                        continue

            # ডুপ্লিকেট রিমুভ
            seen = set()
            unique = []
            for repo in live_repos:
                repo_id = repo.get('id')
                if repo_id and repo_id not in seen:
                    seen.add(repo_id)
                    unique.append(repo)

            return unique[:50]

        except Exception as e:
            logger.error(f"GitHub API error: {e}")
            return []

    def _is_live_repo(self, repo: Dict) -> bool:
        """লাইভ চেক"""
        try:
            # গত ২৪ ঘন্টায় আপডেট?
            updated = repo.get('updated_at')
            if updated:
                updated = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                if datetime.now().astimezone() - updated < timedelta(hours=24):
                    return True

            # অনেক স্টার?
            if repo.get('stargazers_count', 0) > 1000:
                return True

            # সাম্প্রতিক পুশ?
            pushed = repo.get('pushed_at')
            if pushed:
                pushed = datetime.fromisoformat(pushed.replace('Z', '+00:00'))
                if datetime.now().astimezone() - pushed < timedelta(hours=12):
                    return True

            return False

        except Exception as e:
            logger.error(f"Live check error: {e}")
            return False

# বট ক্লাস
class GitHubBot:
    def __init__(self, token: str, group_id: int, admin_id: int):
        self.token = token
        self.group_id = group_id
        self.admin_id = admin_id
        self.last_sent = set()
        self.updater = None
        self.bot = None
        self.is_running = True

    def start(self):
        """বট স্টার্ট"""
        try:
            # আপডেটার তৈরি
            self.updater = Updater(token=self.token, use_context=True)
            self.bot = self.updater.bot
            
            dp = self.updater.dispatcher
            
            # হ্যান্ডলার যোগ
            dp.add_handler(CommandHandler("start", self.start_command))
            dp.add_handler(CommandHandler("help", self.help_command))
            dp.add_handler(CallbackQueryHandler(self.button_callback))
            
            # এরর হ্যান্ডলার
            dp.add_error_handler(self.error_handler)
            
            # স্টার্টআপ মেসেজ
            threading.Thread(target=self.send_startup_message, daemon=True).start()
            
            # শিডিউলড টাস্ক
            threading.Thread(target=self.run_scheduler, daemon=True).start()
            
            logger.info("Bot started successfully!")
            
            # পোলিং স্টার্ট
            self.updater.start_polling()
            
            # আইডল (বট চালু রাখতে)
            self.updater.idle()
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise

    def send_startup_message(self):
        """স্টার্টআপ মেসেজ"""
        try:
            time.sleep(3)
            self.bot.send_message(
                chat_id=self.group_id,
                text="🚀 **GitHub লাইভ API স্ক্র্যাপার বট চালু হয়েছে!**\n\n"
                     "🔹 প্রতি ১ ঘন্টায় স্বয়ংক্রিয়ভাবে স্ক্র্যাপ হবে\n"
                     "🔹 লাইভ রিপোজিটরি এই গ্রুপে পাঠানো হবে\n"
                     "🔹 /start কমান্ড দিয়ে কন্ট্রোল প্যানেল দেখুন",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Startup message error: {e}")

    def run_scheduler(self):
        """শিডিউলার"""
        while self.is_running:
            try:
                logger.info("Starting scheduled scrape...")
                self.scrape_and_send()
                logger.info("Scheduled scrape completed")
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time.sleep(3600)  # ১ ঘন্টা

    def scrape_and_send(self):
        """স্ক্র্যাপ এবং সেন্ড"""
        try:
            # সিঙ্ক্রোনাসভাবে রান
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            repos = loop.run_until_complete(self.get_live_repos())
            loop.close()
            
            if not repos:
                return
                
            # নতুন রিপোজিটরি
            new_repos = [r for r in repos if r.get('id') not in self.last_sent]
            
            if not new_repos:
                return
                
            # গ্রুপে পাঠান
            for repo in new_repos[:10]:
                message = self.format_repo_message(repo)
                try:
                    self.bot.send_message(
                        chat_id=self.group_id,
                        text=message,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )
                    self.last_sent.add(repo.get('id'))
                    time.sleep(0.5)
                except Exception as e:
                    logger.error(f"Send error: {e}")
                    
            logger.info(f"Sent {len(new_repos[:10])} new repos")
            
        except Exception as e:
            logger.error(f"Scrape and send error: {e}")

    async def get_live_repos(self) -> List[Dict]:
        """লাইভ রিপোজিটরি পাওয়ার জন্য async মেথড"""
        client = GitHubAPIClient(GITHUB_TOKEN)
        return await client.get_live_repos()

    def format_repo_message(self, repo: Dict) -> str:
        """মেসেজ ফরম্যাট"""
        name = repo.get('full_name', 'N/A')
        desc = repo.get('description', 'No description') or 'No description'
        stars = repo.get('stargazers_count', 0)
        forks = repo.get('forks_count', 0)
        lang = repo.get('language', 'Unknown')
        url = repo.get('html_url', '#')
        updated = repo.get('updated_at', 'N/A')
        issues = repo.get('open_issues_count', 0)

        return f"""
🚀 **{name}**

📝 {desc[:200]}...

⭐ {stars} Stars | 🍴 {forks} Forks
💻 Language: {lang}
🐛 Open Issues: {issues}
🔄 Updated: {updated[:10]}

🔗 [View on GitHub]({url})
"""

    def start_command(self, update: Update, context: CallbackContext):
        """স্টার্ট কমান্ড"""
        if not update.effective_user:
            return
            
        if update.effective_user.id != self.admin_id:
            update.message.reply_text("⛔ আপনি এই বট ব্যবহার করার অনুমতি পাবেন না!")
            return

        keyboard = [
            [InlineKeyboardButton("📊 লাইভ রিপোজিটরি দেখুন", callback_data='view_live')],
            [InlineKeyboardButton("🔄 ম্যানুয়াল স্ক্র্যাপ", callback_data='manual_scrape')],
            [InlineKeyboardButton("📈 পরিসংখ্যান", callback_data='stats')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        update.message.reply_text(
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

    def help_command(self, update: Update, context: CallbackContext):
        """হেল্প কমান্ড"""
        self.start_command(update, context)

    def button_callback(self, update: Update, context: CallbackContext):
        """বাটন কলব্যাক"""
        if not update.effective_user or not update.callback_query:
            return
            
        query = update.callback_query
        query.answer()

        if update.effective_user.id != self.admin_id:
            query.edit_message_text("⛔ আপনি এই অপারেশন করতে পারবেন না!")
            return

        if query.data == 'view_live':
            self.view_live_repos(query)
        elif query.data == 'manual_scrape':
            self.manual_scrape(query)
        elif query.data == 'stats':
            self.show_stats(query)

    def view_live_repos(self, query):
        """লাইভ রিপোজিটরি দেখান"""
        query.edit_message_text("🔄 লাইভ রিপোজিটরি খোঁজা হচ্ছে...")
        
        try:
            # থ্রেডেডভাবে স্ক্র্যাপ
            import threading
            def scrape_and_reply():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    repos = loop.run_until_complete(self.get_live_repos())
                    loop.close()
                    
                    if not repos:
                        self.bot.edit_message_text(
                            chat_id=query.message.chat_id,
                            message_id=query.message.message_id,
                            text="❌ কোনো লাইভ রিপোজিটরি পাওয়া যায়নি!"
                        )
                        return
                        
                    new_repos = [r for r in repos if r.get('id') not in self.last_sent]
                    
                    if not new_repos:
                        self.bot.edit_message_text(
                            chat_id=query.message.chat_id,
                            message_id=query.message.message_id,
                            text="📭 কোনো নতুন লাইভ রিপোজিটরি পাওয়া যায়নি!"
                        )
                        return
                        
                    sent = 0
                    for repo in new_repos[:10]:
                        message = self.format_repo_message(repo)
                        try:
                            self.bot.send_message(
                                chat_id=self.group_id,
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )
                            self.last_sent.add(repo.get('id'))
                            sent += 1
                            time.sleep(0.3)
                        except Exception as e:
                            logger.error(f"Send error: {e}")
                            
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=f"✅ {sent}টি নতুন লাইভ রিপোজিটরি পাঠানো হয়েছে!"
                    )
                    
                except Exception as e:
                    logger.error(f"Scrape thread error: {e}")
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=f"❌ ত্রুটি: {str(e)}"
                    )
            
            thread = threading.Thread(target=scrape_and_reply, daemon=True)
            thread.start()
            
        except Exception as e:
            logger.error(f"View live error: {e}")
            query.edit_message_text(f"❌ ত্রুটি: {str(e)}")

    def manual_scrape(self, query):
        """ম্যানুয়াল স্ক্র্যাপ"""
        self.view_live_repos(query)

    def show_stats(self, query):
        """পরিসংখ্যান"""
        stats = f"""
📊 **বট পরিসংখ্যান**

🤖 বট স্ট্যাটাস: ✅ চালু
📁 ক্যাশেড রিপো: {len(cache)}
🔄 শেষ স্ক্র্যাপ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
👑 অ্যাডমিন: {self.admin_id}

🔹 প্রতি ১ ঘন্টায় স্ক্র্যাপ
🔹 সর্বোচ্চ ৫০টি রিপোজিটরি
🔹 শুধুমাত্র লাইভ রিপোজিটরি
        """
        query.edit_message_text(stats, parse_mode='Markdown')

    def error_handler(self, update: Update, context: CallbackContext):
        """এরর হ্যান্ডলার"""
        logger.error(f"Update {update} caused error {context.error}")

# মেইন
def main():
    """মেইন ফাংশন"""
    try:
        # চেক ভেরিয়েবল
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN missing")
        if not PRIVATE_GROUP_ID:
            raise ValueError("PRIVATE_GROUP_ID missing")
        if not ADMIN_USER_ID:
            raise ValueError("ADMIN_USER_ID missing")
        if not GITHUB_TOKEN:
            raise ValueError("GITHUB_TOKEN missing")

        # ফ্লাস্ক থ্রেড
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("Flask server started on port 8080")

        # বট স্টার্ট
        bot = GitHubBot(BOT_TOKEN, PRIVATE_GROUP_ID, ADMIN_USER_ID)
        bot.start()
        
    except Exception as e:
        logger.error(f"Main error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    import time
    main()
