import os
import logging
import pickle
import re
import pytz
from datetime import datetime
from threading import Thread, Lock
from enum import Enum, auto
from typing import Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    TwoFactorRequired,
    ChallengeRequired,
    ClientError,
    BadPassword,
    PleaseWaitFewMinutes
)

# ==============================================
# КОНФИГУРАЦИЯ
# ==============================================

class Config:
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    STORAGE_FILE = 'instagram_accounts.dat'
    TEMP_DIR = 'tmp'
    SCHEDULER_INTERVAL = 10
    MAX_LOGIN_ATTEMPTS = 3

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================
# МОДЕЛИ ДАННЫХ
# ==============================================

class State(Enum):
    SELECT_ACCOUNT = auto()
    INPUT_USERNAME = auto()
    INPUT_PASSWORD = auto()
    SELECT_2FA_METHOD = auto()
    INPUT_2FA_CODE = auto()
    INPUT_TARGET_ACCOUNT = auto()
    INPUT_POST_CAPTION = auto()
    INPUT_POST_TIME = auto()
    INPUT_STORY_TIME = auto()

class AccountData:
    def __init__(self, username: str, client: Client, verification_method: Optional[str] = None):
        self.username = username
        self.client = client
        self.user_id = client.user_id
        self.verification_method = verification_method
        self.last_used = datetime.now()

# ==============================================
# МЕНЕДЖЕР ДАННЫХ
# ==============================================

class DataManager:
    def __init__(self):
        self.accounts: Dict[str, AccountData] = {}
        self.post_queue = []
        self.stories_queue = []
        self.lock = Lock()
        self.load_accounts()

    def load_accounts(self):
        try:
            with open(Config.STORAGE_FILE, 'rb') as f:
                data = pickle.load(f)
                with self.lock:
                    for username, acc_data in data.items():
                        client = Client()
                        if 'session' in acc_data:
                            client.set_settings(acc_data['session'])
                        self.accounts[username] = AccountData(
                            username=username,
                            client=client,
                            verification_method=acc_data.get('verification_method')
                        )
        except (FileNotFoundError, EOFError):
            logger.info("No accounts file found, creating new one")

    def save_accounts(self):
        with self.lock:
            data_to_save = {}
            for username, account in self.accounts.items():
                data_to_save[username] = {
                    'session': account.client.get_settings(),
                    'verification_method': account.verification_method,
                    'last_used': account.last_used
                }
            with open(Config.STORAGE_FILE, 'wb') as f:
                pickle.dump(data_to_save, f)

# ==============================================
# INSTAGRAM МЕНЕДЖЕР
# ==============================================

class InstagramManager:
    @staticmethod
    async def login(username: str, password: str, verification_code: str = None, method: str = None) -> Client:
        client = Client()
        try:
            if method == 'email':
                # Специальная обработка для email 2FA
                client.login(username, password, verification_code=verification_code)
            elif verification_code and method:
                client.login(username, password)
                client.get_totp_two_factor_code = lambda: verification_code
                client.handle_two_factor_login(verification_code)
            else:
                client.login(username, password)
            return client
        except Exception as e:
            logger.error(f"Login failed: {e}")
            raise

    @staticmethod
    async def get_2fa_methods(username: str, password: str) -> List[str]:
        client = Client()
        try:
            client.login(username, password)
            return []
        except TwoFactorRequired as e:
            return getattr(e, 'allowed_methods', ['app', 'sms', 'whatsapp', 'call', 'email'])
        except Exception as e:
            logger.error(f"2FA methods check failed: {e}")
            return []

# ==============================================
# ОСНОВНОЙ БОТ
# ==============================================

class InstagramBot:
    def __init__(self):
        self.data_manager = DataManager()
        self.application = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await update.message.reply_text(
            "👋 Привет! Я бот для отложенного постинга в Instagram.\n\n"
            "📌 Доступные команды:\n"
            "/add_account - добавить аккаунт\n"
            "/accounts - список аккаунтов\n"
            "/add_post - добавить пост\n"
            "/add_story - добавить сторис\n"
            "/queue - очередь публикаций\n"
            "/cancel - отмена операции"
        )

    async def add_account_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало добавления аккаунта"""
        await update.message.reply_text("Введите имя пользователя Instagram:")
        return State.INPUT_USERNAME

    async def input_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода username"""
        username = update.message.text.strip()
        context.user_data['username'] = username
        await update.message.reply_text("Введите пароль:")
        return State.INPUT_PASSWORD

    async def input_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода пароля"""
        password = update.message.text
        username = context.user_data['username']
        context.user_data['password'] = password

        try:
            methods = await InstagramManager.get_2fa_methods(username, password)
            if not methods:
                client = await InstagramManager.login(username, password)
                self.data_manager.accounts[username] = AccountData(username, client)
                self.data_manager.save_accounts()
                await update.message.reply_text(f"✅ Аккаунт {username} добавлен!")
                return ConversationHandler.END

            buttons = []
            if 'app' in methods:
                buttons.append(InlineKeyboardButton("📱 Приложение", callback_data='2fa_app'))
            if 'sms' in methods:
                buttons.append(InlineKeyboardButton("📨 SMS", callback_data='2fa_sms'))
            if 'whatsapp' in methods:
                buttons.append(InlineKeyboardButton("💬 WhatsApp", callback_data='2fa_whatsapp'))
            if 'call' in methods:
                buttons.append(InlineKeyboardButton("📞 Звонок", callback_data='2fa_call'))
            if 'email' in methods:
                buttons.append(InlineKeyboardButton("📧 Email", callback_data='2fa_email'))

            keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
            await update.message.reply_text(
                "🔐 Выберите метод 2FA:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return State.SELECT_2FA_METHOD

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    async def select_2fa_method(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора метода 2FA"""
        query = update.callback_query
        await query.answer()

        method_map = {
            '2fa_app': ('app', "Введите код из приложения:"),
            '2fa_sms': ('sms', "Введите код из SMS:"),
            '2fa_whatsapp': ('whatsapp', "Введите код из WhatsApp:"),
            '2fa_call': ('call', "Введите код из звонка:"),
            '2fa_email': ('email', "Введите код из email:")
        }

        method, message = method_map.get(query.data, (None, None))
        if not method:
            await query.edit_message_text("❌ Неверный метод")
            return State.SELECT_2FA_METHOD

        context.user_data['2fa_method'] = method
        await query.edit_message_text(message)
        return State.INPUT_2FA_CODE

    async def input_2fa_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода кода 2FA"""
        code = update.message.text.strip()
        if not re.match(r'^\d{6}$', code):
            await update.message.reply_text("❌ Код должен быть 6 цифр")
            return State.INPUT_2FA_CODE

        username = context.user_data['username']
        password = context.user_data['password']
        method = context.user_data['2fa_method']

        try:
            client = await InstagramManager.login(username, password, code, method)
            self.data_manager.accounts[username] = AccountData(username, client, method)
            self.data_manager.save_accounts()
            await update.message.reply_text(f"✅ Аккаунт {username} добавлен!")
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    def setup_handlers(self):
        """Настройка обработчиков команд"""
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('add_account', self.add_account_start)],
            states={
                State.INPUT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.input_username)],
                State.INPUT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.input_password)],
                State.SELECT_2FA_METHOD: [CallbackQueryHandler(self.select_2fa_method)],
                State.INPUT_2FA_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.input_2fa_code)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
        )

        self.application.add_handler(CommandHandler('start', self.start))
        self.application.add_handler(conv_handler)
        self.application.add_handler(CommandHandler('accounts', self.list_accounts))
        self.application.add_error_handler(self.error_handler)

    async def run(self):
        """Запуск бота"""
        self.application = Application.builder().token(Config.TELEGRAM_TOKEN).build()
        self.setup_handlers()
        
        if not os.path.exists(Config.TEMP_DIR):
            os.makedirs(Config.TEMP_DIR)

        await self.application.run_polling()

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена текущей операции"""
        await update.message.reply_text("Операция отменена")
        return ConversationHandler.END

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)
        if update and update.message:
            await update.message.reply_text("❌ Произошла ошибка")

# ==============================================
# ЗАПУСК БОТА
# ==============================================

if __name__ == '__main__':
    bot = InstagramBot()
    bot.run()
