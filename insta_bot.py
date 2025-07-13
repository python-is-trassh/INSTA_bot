import os
import logging
import pickle
import re
from datetime import datetime, timedelta
from threading import Thread, Lock
from time import sleep
import pytz

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ConversationHandler,
    CallbackQueryHandler
)
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, TwoFactorRequired,
    ChallengeRequired, ClientError,
    BadPassword, ReloginAttemptExceeded
)

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

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
STORAGE_FILE = 'instagram_accounts.dat'
TEMP_DIR = 'tmp'

# Состояния ConversationHandler
(
    SELECT_ACCOUNT, INPUT_USERNAME, INPUT_PASSWORD,
    SELECT_2FA_METHOD, INPUT_2FA_CODE,
    INPUT_TARGET_ACCOUNT, INPUT_POST_CAPTION,
    INPUT_POST_TIME, INPUT_STORY_TIME
) = range(9)

class InstagramBot:
    def __init__(self):
        self.accounts = {}
        self.post_queue = []
        self.stories_queue = []
        self.account_lock = Lock()
        self.scheduler_running = False
        self.load_accounts()
        
        if not os.path.exists(TEMP_DIR):
            os.makedirs(TEMP_DIR)

    def load_accounts(self):
        try:
            with open(STORAGE_FILE, 'rb') as f:
                self.accounts = pickle.load(f)
                # Инициализация клиентов для загруженных аккаунтов
                for username, data in self.accounts.items():
                    if 'client' not in data:
                        self.accounts[username]['client'] = Client()
            logger.info(f"Loaded {len(self.accounts)} accounts")
        except (FileNotFoundError, EOFError):
            self.accounts = {}

    def save_accounts(self):
        with self.account_lock:
            accounts_to_save = {}
            for username, data in self.accounts.items():
                accounts_to_save[username] = {
                    k: v for k, v in data.items() if k != 'client'
                }
            with open(STORAGE_FILE, 'wb') as f:
                pickle.dump(accounts_to_save, f)

    def init_instagram_client(self, username, password, verification_code=None, verification_method=None):
        """Инициализация клиента с поддержкой всех методов 2FA"""
        cl = Client()
        try:
            if verification_method == 'email':
                # Специальная обработка для email 2FA
                cl.login(username, password, verification_code=verification_code)
            elif verification_code and verification_method:
                cl.login(username, password)
                cl.get_totp_two_factor_code = lambda: verification_code
                cl.handle_two_factor_login(verification_code)
            else:
                cl.login(username, password)
            return cl
        except Exception as e:
            logger.error(f"Login error: {e}")
            raise

    def get_2fa_methods(self, username, password):
        """Получение доступных методов 2FA"""
        cl = Client()
        try:
            cl.login(username, password)
            return []  # Если вход без 2FA
        except TwoFactorRequired as e:
            return getattr(e, 'allowed_methods', ['app', 'sms', 'whatsapp', 'call', 'email'])
        except Exception as e:
            logger.error(f"2FA check error: {e}")
            return []

    def add_account(self, username, password, verification_code=None, verification_method=None):
        """Добавление аккаунта с обработкой 2FA"""
        try:
            cl = self.init_instagram_client(username, password, verification_code, verification_method)
            self.accounts[username] = {
                'client': cl,
                'user_id': cl.user_id,
                'last_used': datetime.now(),
                'verification_method': verification_method
            }
            self.save_accounts()
            return True
        except Exception as e:
            logger.error(f"Add account error: {e}")
            return False

    def add_to_queue(self, queue_type, content, caption=None, publish_time=None, target_account=None):
        """Добавление в очередь постов или сторис"""
        item = {
            'content': content,
            'caption': caption,
            'publish_time': publish_time or datetime.now(pytz.utc),
            'status': 'queued',
            'target_account': target_account,
            'added_at': datetime.now(pytz.utc)
        }
        
        if queue_type == 'post':
            self.post_queue.append(item)
        elif queue_type == 'story':
            self.stories_queue.append(item)
        
        return item

    def publish_post(self, post):
        """Публикация поста"""
        try:
            account = self.accounts.get(post['target_account'])
            if not account:
                post['status'] = 'failed'
                return False

            cl = account['client']
            
            if isinstance(post['content'], list):  # Альбом
                media = []
                for photo in post['content']:
                    media.append(cl.photo_upload(photo, post['caption']))
            else:  # Одно фото
                media = cl.photo_upload(post['content'], post['caption'])
            
            post['status'] = 'published'
            post['published_at'] = datetime.now(pytz.utc)
            account['last_used'] = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Post publish error: {e}")
            post['status'] = 'failed'
            return False

    def publish_story(self, story):
        """Публикация истории"""
        try:
            account = self.accounts.get(story['target_account'])
            if not account:
                story['status'] = 'failed'
                return False

            cl = account['client']
            
            if isinstance(story['content'], list):  # Несколько фото
                for photo in story['content']:
                    cl.photo_upload_to_story(photo)
            else:  # Одно фото
                cl.photo_upload_to_story(story['content'])
            
            story['status'] = 'published'
            story['published_at'] = datetime.now(pytz.utc)
            account['last_used'] = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Story publish error: {e}")
            story['status'] = 'failed'
            return False

    def scheduler(self):
        """Планировщик публикаций"""
        self.scheduler_running = True
        
        while self.scheduler_running:
            try:
                # Проверяем очередь постов
                for post in self.post_queue:
                    if post['status'] == 'queued' and post['publish_time'] <= datetime.now(pytz.utc):
                        self.publish_post(post)
                
                # Проверяем очередь сторис
                for story in self.stories_queue:
                    if story['status'] == 'queued' and story['publish_time'] <= datetime.now(pytz.utc):
                        self.publish_story(story)
                
                sleep(10)  # Проверяем каждые 10 секунд
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                sleep(30)

    def start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start"""
        update.message.reply_text(
            "👋 Привет! Я бот для отложенного постинга в Instagram.\n\n"
            "📌 Доступные команды:\n"
            "/add_account - добавить аккаунт\n"
            "/accounts - список аккаунтов\n"
            "/add_post - добавить пост\n"
            "/add_story - добавить сторис\n"
            "/queue - очередь публикаций\n"
            "/cancel - отмена операции"
        )

    def add_account_command(self, update: Update, context: CallbackContext):
        """Начало процесса добавления аккаунта"""
        update.message.reply_text(
            "Введите имя пользователя Instagram:",
            reply_markup=ReplyKeyboardRemove()
        )
        return INPUT_USERNAME

    def input_username(self, update: Update, context: CallbackContext):
        """Обработка ввода username"""
        username = update.message.text.strip()
        context.user_data['username'] = username
        update.message.reply_text("Введите пароль:")
        return INPUT_PASSWORD

    def input_password(self, update: Update, context: CallbackContext):
        """Обработка ввода пароля"""
        password = update.message.text
        username = context.user_data['username']
        context.user_data['password'] = password

        try:
            methods = self.get_2fa_methods(username, password)
            if not methods:  # Если 2FA не требуется
                if self.add_account(username, password):
                    update.message.reply_text(f"Аккаунт {username} успешно добавлен!")
                else:
                    update.message.reply_text("Не удалось добавить аккаунт")
                return ConversationHandler.END

            # Создаем кнопки для всех доступных методов
            buttons = []
            if 'app' in methods:
                buttons.append(InlineKeyboardButton("Приложение", callback_data='2fa_app'))
            if 'sms' in methods:
                buttons.append(InlineKeyboardButton("SMS", callback_data='2fa_sms'))
            if 'whatsapp' in methods:
                buttons.append(InlineKeyboardButton("WhatsApp", callback_data='2fa_whatsapp'))
            if 'call' in methods:
                buttons.append(InlineKeyboardButton("Звонок", callback_data='2fa_call'))
            if 'email' in methods:
                buttons.append(InlineKeyboardButton("Email", callback_data='2fa_email'))

            keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
            update.message.reply_text(
                "Выберите метод двухфакторной аутентификации:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return SELECT_2FA_METHOD

        except Exception as e:
            update.message.reply_text(f"Ошибка: {str(e)}")
            return ConversationHandler.END

    def select_2fa_method(self, update: Update, context: CallbackContext):
        """Обработка выбора метода 2FA"""
        query = update.callback_query
        query.answer()

        method_map = {
            '2fa_app': ('app', "Введите код из приложения:"),
            '2fa_sms': ('sms', "Введите код из SMS:"),
            '2fa_whatsapp': ('whatsapp', "Введите код из WhatsApp:"),
            '2fa_call': ('call', "Введите код из звонка:"),
            '2fa_email': ('email', "Введите код из email:")
        }

        method, message = method_map.get(query.data, (None, None))
        if not method:
            query.edit_message_text("Неизвестный метод")
            return SELECT_2FA_METHOD

        context.user_data['2fa_method'] = method
        query.edit_message_text(message)
        return INPUT_2FA_CODE

    def input_2fa_code(self, update: Update, context: CallbackContext):
        """Обработка ввода кода 2FA"""
        code = update.message.text.strip()
        if not re.match(r'^\d{6}$', code):
            update.message.reply_text("Код должен быть 6 цифр")
            return INPUT_2FA_CODE

        username = context.user_data['username']
        password = context.user_data['password']
        method = context.user_data['2fa_method']

        try:
            if self.add_account(username, password, code, method):
                update.message.reply_text(f"Аккаунт {username} успешно добавлен!")
            else:
                update.message.reply_text("Не удалось добавить аккаунт")
                return INPUT_2FA_CODE
        except Exception as e:
            update.message.reply_text(f"Ошибка: {str(e)}")

        return ConversationHandler.END

    def add_post(self, update: Update, context: CallbackContext):
        """Добавление поста в очередь"""
        if not self.accounts:
            update.message.reply_text("Сначала добавьте аккаунт командой /add_account")
            return
        
        keyboard = [[KeyboardButton(username)] for username in self.accounts.keys()]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        
        update.message.reply_text(
            "Выберите аккаунт для публикации:",
            reply_markup=reply_markup
        )
        return SELECT_ACCOUNT

    def select_account(self, update: Update, context: CallbackContext):
        """Выбор аккаунта для публикации"""
        selected_account = update.message.text
        if selected_account not in self.accounts:
            update.message.reply_text("Неверный аккаунт")
            return SELECT_ACCOUNT
        
        context.user_data['target_account'] = selected_account
        update.message.reply_text(
            "Отправьте фото для поста (можно несколько):",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data['post_media'] = []
        return INPUT_TARGET_ACCOUNT

    def handle_media(self, update: Update, context: CallbackContext):
        """Обработка медиафайлов"""
        if 'post_media' in context.user_data:
            photo = update.message.photo[-1].get_file()
            file_path = f"{TEMP_DIR}/post_{update.update_id}.jpg"
            context.user_data['post_media'].append(photo.download(custom_path=file_path))
            update.message.reply_text("Фото добавлено. Отправьте еще или /done для продолжения")
        elif 'story_media' in context.user_data:
            photo = update.message.photo[-1].get_file()
            file_path = f"{TEMP_DIR}/story_{update.update_id}.jpg"
            context.user_data['story_media'].append(photo.download(custom_path=file_path))
            update.message.reply_text("Фото для сторис добавлено. Отправьте еще или /done для продолжения")

    def done(self, update: Update, context: CallbackContext):
        """Завершение загрузки медиа"""
        if 'post_media' in context.user_data:
            if not context.user_data['post_media']:
                update.message.reply_text("Вы не отправили ни одного фото")
                return
            
            update.message.reply_text("Введите подпись к посту:")
            return INPUT_POST_CAPTION
        elif 'story_media' in context.user_data:
            if not context.user_data['story_media']:
                update.message.reply_text("Вы не отправили ни одного фото")
                return
            
            update.message.reply_text(
                "Введите время публикации (ДД.ММ.ГГГГ ЧЧ:ММ или 'now'):"
            )
            return INPUT_STORY_TIME

    def input_post_caption(self, update: Update, context: CallbackContext):
        """Обработка подписи к посту"""
        context.user_data['post_caption'] = update.message.text
        update.message.reply_text(
            "Введите время публикации (ДД.ММ.ГГГГ ЧЧ:ММ или 'now'):"
        )
        return INPUT_POST_TIME

    def handle_time(self, update: Update, context: CallbackContext):
        """Обработка времени публикации"""
        text = update.message.text.lower()
        
        try:
            if text == 'now':
                publish_time = datetime.now(pytz.utc)
            else:
                publish_time = datetime.strptime(text, "%d.%m.%Y %H:%M")
                publish_time = pytz.timezone('Europe/Moscow').localize(publish_time).astimezone(pytz.utc)
            
            if 'post_caption' in context.user_data:  # Это пост
                media = context.user_data['post_media']
                caption = context.user_data['post_caption']
                target_account = context.user_data['target_account']
                
                content = media[0] if len(media) == 1 else media
                post = self.add_to_queue('post', content, caption, publish_time, target_account)
                
                update.message.reply_text(
                    f"Пост добавлен в очередь на {publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}\n"
                    f"Аккаунт: {target_account}"
                )
            else:  # Это сторис
                media = context.user_data['story_media']
                target_account = context.user_data['target_account']
                
                content = media[0] if len(media) == 1 else media
                story = self.add_to_queue('story', content, None, publish_time, target_account)
                
                update.message.reply_text(
                    f"Сторис добавлен в очередь на {publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}\n"
                    f"Аккаунт: {target_account}"
                )
            
            context.user_data.clear()
            return ConversationHandler.END
        
        except ValueError:
            update.message.reply_text("Неверный формат времени")
            return INPUT_POST_TIME if 'post_caption' in context.user_data else INPUT_STORY_TIME

    def list_accounts(self, update: Update, context: CallbackContext):
        """Список добавленных аккаунтов"""
        if not self.accounts:
            update.message.reply_text("Нет добавленных аккаунтов")
            return
        
        message = "Добавленные аккаунты:\n\n"
        for i, (username, data) in enumerate(self.accounts.items(), 1):
            method = data.get('verification_method', 'нет')
            last_used = data['last_used'].strftime('%d.%m.%Y %H:%M') if 'last_used' in data else 'никогда'
            message += f"{i}. {username} (2FA: {method}, использован: {last_used})\n"
        
        update.message.reply_text(message)

    def show_queue(self, update: Update, context: CallbackContext):
        """Показать очередь публикаций"""
        if not self.post_queue and not self.stories_queue:
            update.message.reply_text("Очередь публикаций пуста")
            return
        
        message = "Очередь публикаций:\n\n"
        
        if self.post_queue:
            message += "Посты:\n"
            for i, post in enumerate(self.post_queue, 1):
                status = 'Ожидает' if post['status'] == 'queued' else 'Опубликован' if post['status'] == 'published' else 'Ошибка'
                time = post['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
                message += f"{i}. {status} ({time}) - {post['target_account']} - {post['caption'][:20]}...\n"
        
        if self.stories_queue:
            message += "\nСторис:\n"
            for i, story in enumerate(self.stories_queue, 1):
                status = 'Ожидает' if story['status'] == 'queued' else 'Опубликована' if story['status'] == 'published' else 'Ошибка'
                time = story['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
                message += f"{i}. {status} ({time}) - {story['target_account']}\n"
        
        update.message.reply_text(message)

    def cancel(self, update: Update, context: CallbackContext):
        """Отмена текущей операции"""
        update.message.reply_text("Операция отменена")
        context.user_data.clear()
        return ConversationHandler.END

    def error_handler(self, update: Update, context: CallbackContext):
        """Обработчик ошибок"""
        logger.error("Ошибка:", exc_info=context.error)
        if update and update.message:
            update.message.reply_text("Произошла ошибка")

    def start_bot(self):
        """Запуск бота"""
        updater = Updater(TELEGRAM_TOKEN)
        dp = updater.dispatcher

        # Обработчики команд
        dp.add_handler(CommandHandler("start", self.start))
        dp.add_handler(CommandHandler("accounts", self.list_accounts))
        dp.add_handler(CommandHandler("queue", self.show_queue))
        dp.add_handler(CommandHandler("cancel", self.cancel))

        # ConversationHandler для добавления аккаунта
        add_account_conv = ConversationHandler(
            entry_points=[CommandHandler('add_account', self.add_account_command)],
            states={
                INPUT_USERNAME: [MessageHandler(Filters.text & ~Filters.command, self.input_username)],
                INPUT_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, self.input_password)],
                SELECT_2FA_METHOD: [CallbackQueryHandler(self.select_2fa_method)],
                INPUT_2FA_CODE: [MessageHandler(Filters.text & ~Filters.command, self.input_2fa_code)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
        )
        dp.add_handler(add_account_conv)

        # ConversationHandler для добавления поста
        add_post_conv = ConversationHandler(
            entry_points=[CommandHandler('add_post', self.add_post)],
            states={
                SELECT_ACCOUNT: [MessageHandler(Filters.text & ~Filters.command, self.select_account)],
                INPUT_TARGET_ACCOUNT: [
                    MessageHandler(Filters.photo, self.handle_media),
                    CommandHandler('done', self.done)
                ],
                INPUT_POST_CAPTION: [MessageHandler(Filters.text & ~Filters.command, self.input_post_caption)],
                INPUT_POST_TIME: [MessageHandler(Filters.text & ~Filters.command, self.handle_time)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
        )
        dp.add_handler(add_post_conv)

        # Обработчик ошибок
        dp.add_error_handler(self.error_handler)

        # Запуск планировщика
        scheduler_thread = Thread(target=self.scheduler, daemon=True)
        scheduler_thread.start()

        updater.start_polling()
        updater.idle()

        self.scheduler_running = False
        scheduler_thread.join()

if __name__ == '__main__':
    bot = InstagramBot()
    bot.start_bot()
