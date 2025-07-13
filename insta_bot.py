import os
import logging
import pickle
from datetime import datetime, timedelta
from threading import Thread, Lock
from time import sleep
import pytz
from enum import Enum, auto

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
    BadPassword, ReloginAttemptExceeded,
    PleaseWaitFewMinutes
)

# ==============================================
# МОДУЛЬ НАСТРОЕК И КОНФИГУРАЦИИ
# ==============================================

class Config:
    """Класс для хранения конфигурации бота"""
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')  # Токен бота из переменных окружения
    STORAGE_FILE = 'instagram_accounts.dat'       # Файл для хранения данных аккаунтов
    TEMP_DIR = 'tmp'                             # Временная папка для медиафайлов
    SCHEDULER_INTERVAL = 10                      # Интервал проверки очереди (секунды)
    MAX_LOGIN_ATTEMPTS = 3                       # Максимальное количество попыток входа

class State(Enum):
    """Перечисление состояний ConversationHandler"""
    SELECT_ACCOUNT = auto()
    INPUT_USERNAME = auto()
    INPUT_PASSWORD = auto() 
    SELECT_2FA_METHOD = auto()
    INPUT_2FA_CODE = auto()
    INPUT_2FA_PHONE_NUMBER = auto()
    INPUT_TARGET_ACCOUNT = auto()
    INPUT_POST_CAPTION = auto()
    INPUT_POST_TIME = auto()
    INPUT_STORY_TIME = auto()

# ==============================================
# МОДУЛЬ ЛОГГИРОВАНИЯ
# ==============================================

def setup_logging():
    """Настройка системы логирования"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
        level=logging.INFO,
        handlers=[
            logging.FileHandler('bot.log'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ==============================================
# МОДУЛЬ РАБОТЫ С ДАННЫМИ
# ==============================================

class DataManager:
    """Класс для управления данными аккаунтов и очередей"""
    
    def __init__(self):
        self.accounts = {}
        self.post_queue = []
        self.stories_queue = []
        self.lock = Lock()
        self.load_accounts()

    def load_accounts(self):
        """Загрузка аккаунтов из файла"""
        try:
            with open(Config.STORAGE_FILE, 'rb') as f:
                data = pickle.load(f)
                with self.lock:
                    self.accounts = data
                    # Инициализируем клиентов для загруженных аккаунтов
                    for username, acc_data in self.accounts.items():
                        if 'client' not in acc_data:
                            self.accounts[username]['client'] = Client()
                logger.info(f"Загружено {len(data)} аккаунтов")
        except (FileNotFoundError, EOFError):
            logger.info("Файл с аккаунтами не найден, будет создан новый")

    def save_accounts(self):
        """Сохранение аккаунтов в файл"""
        with self.lock:
            # Не сохраняем клиенты в файл
            accounts_to_save = {
                username: {k: v for k, v in data.items() if k != 'client'}
                for username, data in self.accounts.items()
            }
            
            with open(Config.STORAGE_FILE, 'wb') as f:
                pickle.dump(accounts_to_save, f)
                logger.info("Аккаунты сохранены")

    def add_account(self, username, password, verification_code=None, verification_method=None):
        """Добавление нового аккаунта"""
        try:
            cl = InstagramManager.init_client(username, password, verification_code, verification_method)
            user_id = cl.user_id
            
            with self.lock:
                self.accounts[username] = {
                    'client': cl,
                    'user_id': user_id,
                    'last_used': datetime.now(),
                    'verification_method': verification_method
                }
            
            self.save_accounts()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления аккаунта {username}: {e}")
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
        
        with self.lock:
            if queue_type == 'post':
                self.post_queue.append(item)
            elif queue_type == 'story':
                self.stories_queue.append(item)
        
        return item

# ==============================================
# МОДУЛЬ РАБОТЫ С INSTAGRAM API
# ==============================================

class InstagramManager:
    """Класс для работы с Instagram API"""
    
    @staticmethod
    def init_client(username, password, verification_code=None, verification_method=None):
        """
        Инициализация клиента Instagram с обработкой 2FA
        
        Args:
            username (str): Логин Instagram
            password (str): Пароль Instagram
            verification_code (str, optional): Код подтверждения
            verification_method (str, optional): Метод подтверждения (app/sms/whatsapp/call)
        
        Returns:
            Client: Аутентифицированный клиент Instagram
        """
        cl = Client()
        cl.request_timeout = 30  # Увеличиваем таймаут запросов
        
        try:
            if verification_code and verification_method:
                if verification_method == 'app':
                    cl.login(username, password, verification_code=verification_code)
                elif verification_method in ['sms', 'whatsapp', 'call']:
                    cl.login(username, password)
                    # Переопределяем метод получения кода 2FA
                    cl.get_totp_two_factor_code = lambda: verification_code
                    cl.handle_two_factor_login(verification_code)
                else:
                    raise ValueError("Неизвестный метод двухфакторной аутентификации")
            else:
                cl.login(username, password)
            
            logger.info(f"Успешный вход для аккаунта {username}")
            return cl
        
        except TwoFactorRequired as e:
            logger.info(f"Требуется 2FA для {username}")
            raise TwoFactorRequired(f"Требуется двухфакторная аутентификация: {e}")
        except ChallengeRequired as e:
            logger.error(f"Требуется подтверждение входа: {e}")
            raise ChallengeRequired(f"Требуется подтверждение в Instagram: {e}")
        except BadPassword as e:
            logger.error(f"Неверный пароль для {username}")
            raise BadPassword("Неверный пароль")
        except ReloginAttemptExceeded as e:
            logger.error(f"Превышено количество попыток входа: {e}")
            raise ReloginAttemptExceeded("Превышено количество попыток входа")
        except PleaseWaitFewMinutes as e:
            logger.error(f"Необходимо подождать: {e}")
            raise PleaseWaitFewMinutes("Слишком много попыток входа. Подождите несколько минут.")
        except Exception as e:
            logger.error(f"Неизвестная ошибка при входе: {e}")
            raise

    @staticmethod
    def get_2fa_methods(username, password):
        """Получение доступных методов двухфакторной аутентификации"""
        cl = Client()
        try:
            cl.login(username, password)
            return []  # Если вход успешен без 2FA
        except TwoFactorRequired as e:
            # Получаем доступные методы 2FA
            methods = []
            if hasattr(e, 'allowed_methods'):
                methods = e.allowed_methods
            else:
                # Стандартные методы, если Instagram не вернул список
                methods = ['app', 'sms', 'whatsapp', 'call']
            return methods
        except Exception as e:
            logger.error(f"Ошибка при проверке методов 2FA: {e}")
            return []

    @staticmethod
    def publish_post(client, content, caption):
        """Публикация поста"""
        try:
            if isinstance(content, list):  # Альбом
                media = []
                for photo in content:
                    media.append(client.photo_upload(photo, caption))
                return media
            else:  # Одиночное фото
                return client.photo_upload(content, caption)
        except Exception as e:
            logger.error(f"Ошибка публикации поста: {e}")
            raise

    @staticmethod
    def publish_story(client, content):
        """Публикация истории"""
        try:
            if isinstance(content, list):  # Несколько фото
                for photo in content:
                    client.photo_upload_to_story(photo)
            else:  # Одно фото
                client.photo_upload_to_story(content)
        except Exception as e:
            logger.error(f"Ошибка публикации сторис: {e}")
            raise

# ==============================================
# МОДУЛЬ ПЛАНИРОВЩИКА
# ==============================================

class Scheduler:
    """Класс для планирования и выполнения отложенных публикаций"""
    
    def __init__(self, data_manager):
        self.data_manager = data_manager
        self.running = False
        self.thread = None

    def start(self):
        """Запуск планировщика в отдельном потоке"""
        if not self.running:
            self.running = True
            self.thread = Thread(target=self.run, daemon=True)
            self.thread.start()
            logger.info("Планировщик запущен")

    def stop(self):
        """Остановка планировщика"""
        if self.running:
            self.running = False
            if self.thread:
                self.thread.join()
            logger.info("Планировщик остановлен")

    def run(self):
        """Основной цикл планировщика"""
        while self.running:
            try:
                self.check_post_queue()
                self.check_stories_queue()
                sleep(Config.SCHEDULER_INTERVAL)
            except Exception as e:
                logger.error(f"Ошибка в планировщике: {e}")
                sleep(30)

    def check_post_queue(self):
        """Проверка очереди постов"""
        now = datetime.now(pytz.utc)
        
        with self.data_manager.lock:
            for post in self.data_manager.post_queue:
                if post['status'] == 'queued' and post['publish_time'] <= now:
                    try:
                        account = self.data_manager.accounts.get(post['target_account'])
                        if account:
                            InstagramManager.publish_post(account['client'], post['content'], post['caption'])
                            post['status'] = 'published'
                            post['published_at'] = now
                            account['last_used'] = now
                            logger.info(f"Опубликован пост в аккаунте {post['target_account']}")
                    except Exception as e:
                        post['status'] = 'failed'
                        logger.error(f"Ошибка публикации поста: {e}")

    def check_stories_queue(self):
        """Проверка очереди сторис"""
        now = datetime.now(pytz.utc)
        
        with self.data_manager.lock:
            for story in self.data_manager.stories_queue:
                if story['status'] == 'queued' and story['publish_time'] <= now:
                    try:
                        account = self.data_manager.accounts.get(story['target_account'])
                        if account:
                            InstagramManager.publish_story(account['client'], story['content'])
                            story['status'] = 'published'
                            story['published_at'] = now
                            account['last_used'] = now
                            logger.info(f"Опубликована сторис в аккаунте {story['target_account']}")
                    except Exception as e:
                        story['status'] = 'failed'
                        logger.error(f"Ошибка публикации сторис: {e}")

# ==============================================
# МОДУЛЬ TELEGRAM БОТА
# ==============================================

class InstagramBot:
    """Основной класс Telegram бота"""
    
    def __init__(self):
        self.data_manager = DataManager()
        self.scheduler = Scheduler(self.data_manager)
        self.updater = None

    def start(self):
        """Запуск бота"""
        # Создаем временную папку
        if not os.path.exists(Config.TEMP_DIR):
            os.makedirs(Config.TEMP_DIR)
        
        # Запускаем планировщик
        self.scheduler.start()
        
        # Настраиваем Telegram бота
        self.updater = Updater(Config.TELEGRAM_TOKEN)
        dp = self.updater.dispatcher
        
        # Регистрируем обработчики команд
        dp.add_handler(CommandHandler("start", self.cmd_start))
        dp.add_handler(self.get_add_account_conversation())
        dp.add_handler(CommandHandler("accounts", self.cmd_list_accounts))
        dp.add_handler(self.get_add_post_conversation())
        dp.add_handler(CommandHandler("queue", self.cmd_show_queue))
        dp.add_handler(CommandHandler("cancel", self.cmd_cancel))
        
        # Регистрируем обработчики сообщений
        dp.add_handler(MessageHandler(Filters.photo, self.handle_media))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, self.handle_text))
        
        # Обработчик времени публикации
        dp.add_handler(MessageHandler(
            Filters.regex(r'^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}|now)$'), 
            self.handle_time
        ))
        
        # Обработчик ошибок
        dp.add_error_handler(self.error_handler)
        
        # Запускаем бота
        logger.info("Бот запускается...")
        self.updater.start_polling()
        self.updater.idle()
        
        # Останавливаем планировщик при завершении работы
        self.scheduler.stop()

    # ==============================================
    # ОБРАБОТЧИКИ КОМАНД
    # ==============================================

    def cmd_start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start - приветственное сообщение"""
        user = update.effective_user
        update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отложенной публикации в Instagram.\n\n"
            "📌 Доступные команды:\n"
            "/add_account - добавить аккаунт Instagram\n"
            "/accounts - список аккаунтов\n"
            "/add_post - добавить пост в очередь\n"
            "/add_story - добавить сторис в очередь\n"
            "/queue - просмотреть очередь публикаций\n"
            "/cancel - отменить текущую операцию"
        )

    def cmd_list_accounts(self, update: Update, context: CallbackContext):
        """Обработчик команды /accounts - список аккаунтов"""
        if not self.data_manager.accounts:
            update.message.reply_text("🔴 Нет добавленных аккаунтов.")
            return
        
        message = "📱 Добавленные аккаунты Instagram:\n\n"
        for i, (username, data) in enumerate(self.data_manager.accounts.items(), 1):
            last_used = data['last_used'].strftime('%d.%m.%Y %H:%M') if 'last_used' in data else 'никогда'
            method = data.get('verification_method', 'нет')
            method_display = {
                'app': '📱 Приложение',
                'sms': '✉️ SMS',
                'whatsapp': '💬 WhatsApp',
                'call': '📞 Звонок',
                'none': '❌ Нет'
            }.get(method, method)
            
            message += f"{i}. 👤 {username}\n   🔐 2FA: {method_display}\n   ⏳ Последнее использование: {last_used}\n\n"
        
        update.message.reply_text(message)

    def cmd_show_queue(self, update: Update, context: CallbackContext):
        """Обработчик команды /queue - показывает очередь публикаций"""
        if not self.data_manager.post_queue and not self.data_manager.stories_queue:
            update.message.reply_text("🔄 Очередь публикаций пуста.")
            return
        
        message = "📌 Очередь публикаций:\n\n"
        
        if self.data_manager.post_queue:
            message += "📷 Посты:\n"
            for i, post in enumerate(self.data_manager.post_queue, 1):
                status = {
                    'queued': '⏳ Ожидает',
                    'published': '✅ Опубликован',
                    'failed': '❌ Ошибка'
                }.get(post['status'], post['status'])
                
                time = post['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
                account = post.get('target_account', 'не указан')
                added = post.get('added_at', datetime.now(pytz.utc)).strftime('%d.%m.%Y %H:%M')
                message += (
                    f"{i}. {status}\n"
                    f"   🕒 {time}\n"
                    f"   👤 {account}\n"
                    f"   📝 {post['caption'][:30]}...\n"
                    f"   📅 Добавлен: {added}\n\n"
                )
        
        if self.data_manager.stories_queue:
            message += "\n📱 Сторис:\n"
            for i, story in enumerate(self.data_manager.stories_queue, 1):
                status = {
                    'queued': '⏳ Ожидает',
                    'published': '✅ Опубликована',
                    'failed': '❌ Ошибка'
                }.get(story['status'], story['status'])
                
                time = story['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
                account = story.get('target_account', 'не указан')
                added = story.get('added_at', datetime.now(pytz.utc)).strftime('%d.%m.%Y %H:%M')
                message += (
                    f"{i}. {status}\n"
                    f"   🕒 {time}\n"
                    f"   👤 {account}\n"
                    f"   📅 Добавлен: {added}\n\n"
                )
        
        update.message.reply_text(message)

    def cmd_cancel(self, update: Update, context: CallbackContext):
        """Обработчик команды /cancel - отмена текущей операции"""
        if 'user_data' in context:
            context.user_data.clear()
        update.message.reply_text("✅ Текущая операция отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # ==============================================
    # CONVERSATION HANDLERS
    # ==============================================

    def get_add_account_conversation(self):
        """Создает ConversationHandler для добавления аккаунта"""
        return ConversationHandler(
            entry_points=[CommandHandler('add_account', self.add_account_start)],
            states={
                State.INPUT_USERNAME: [MessageHandler(Filters.text & ~Filters.command, self.input_username)],
                State.INPUT_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, self.input_password)],
                State.SELECT_2FA_METHOD: [CallbackQueryHandler(self.select_2fa_method)],
                State.INPUT_2FA_CODE: [MessageHandler(Filters.text & ~Filters.command, self.input_2fa_code)],
            },
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )

    def get_add_post_conversation(self):
        """Создает ConversationHandler для добавления поста"""
        return ConversationHandler(
            entry_points=[CommandHandler('add_post', self.add_post_start)],
            states={
                State.SELECT_ACCOUNT: [MessageHandler(Filters.text & ~Filters.command, self.select_account)],
                State.INPUT_TARGET_ACCOUNT: [
                    MessageHandler(Filters.photo, self.handle_media),
                    MessageHandler(Filters.text & ~Filters.command, self.handle_text),
                    CommandHandler('done', self.done)
                ],
                State.INPUT_POST_CAPTION: [MessageHandler(Filters.text & ~Filters.command, self.input_post_caption)],
                State.INPUT_POST_TIME: [MessageHandler(Filters.text & ~Filters.command, self.input_post_time)],
            },
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )

    # ==============================================
    # ОБРАБОТЧИКИ ДОБАВЛЕНИЯ АККАУНТА
    # ==============================================

    def add_account_start(self, update: Update, context: CallbackContext):
        """Начало процесса добавления аккаунта"""
        update.message.reply_text(
            "🔑 Введите имя пользователя Instagram:",
            reply_markup=ReplyKeyboardRemove()
        )
        return State.INPUT_USERNAME

    def input_username(self, update: Update, context: CallbackContext):
        """Обработчик ввода имени пользователя"""
        username = update.message.text.strip()
        
        if username in self.data_manager.accounts:
            update.message.reply_text(
                f"⚠️ Аккаунт {username} уже добавлен. Хотите обновить данные? (да/нет)"
            )
            context.user_data['username'] = username
            context.user_data['update_existing'] = True
        else:
            context.user_data['username'] = username
            context.user_data['update_existing'] = False
        
        update.message.reply_text(
            "🔒 Введите пароль:",
            reply_markup=ReplyKeyboardRemove()
        )
        return State.INPUT_PASSWORD

    def input_password(self, update: Update, context: CallbackContext):
        """Обработчик ввода пароля"""
        password = update.message.text
        username = context.user_data['username']
        context.user_data['password'] = password
        
        # Проверяем, требуется ли 2FA
        try:
            methods = InstagramManager.get_2fa_methods(username, password)
            if not methods:  # 2FA не требуется
                if self.data_manager.add_account(username, password):
                    update.message.reply_text(f"✅ Аккаунт {username} успешно добавлен!")
                else:
                    update.message.reply_text("❌ Не удалось добавить аккаунт. Проверьте данные и попробуйте снова.")
                context.user_data.clear()
                return ConversationHandler.END
            
            # Если требуется 2FA, предлагаем выбрать метод
            buttons = []
            if 'app' in methods:
                buttons.append(InlineKeyboardButton("📱 Приложение аутентификации", callback_data='2fa_app'))
            if 'sms' in methods:
                buttons.append(InlineKeyboardButton("✉️ SMS", callback_data='2fa_sms'))
            if 'whatsapp' in methods:
                buttons.append(InlineKeyboardButton("💬 WhatsApp", callback_data='2fa_whatsapp'))
            if 'call' in methods:
                buttons.append(InlineKeyboardButton("📞 Звонок", callback_data='2fa_call'))
            
            keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            update.message.reply_text(
                "🔐 Выберите метод двухфакторной аутентификации:",
                reply_markup=reply_markup
            )
            return State.SELECT_2FA_METHOD
        
        except BadPassword:
            update.message.reply_text("❌ Неверный пароль. Попробуйте снова.")
            return State.INPUT_USERNAME
        except PleaseWaitFewMinutes:
            update.message.reply_text("⏳ Слишком много попыток входа. Подождите несколько минут.")
            return ConversationHandler.END
        except Exception as e:
            update.message.reply_text(f"❌ Ошибка: {str(e)}. Попробуйте снова.")
            return State.INPUT_USERNAME

    def select_2fa_method(self, update: Update, context: CallbackContext):
        """Обработчик выбора метода 2FA"""
        query = update.callback_query
        query.answer()
        
        method_map = {
            '2fa_app': 'app',
            '2fa_sms': 'sms',
            '2fa_whatsapp': 'whatsapp',
            '2fa_call': 'call'
        }
        
        method = method_map.get(query.data)
        if not method:
            query.edit_message_text("❌ Неизвестный метод аутентификации. Попробуйте снова.")
            return State.SELECT_2FA_METHOD
        
        context.user_data['2fa_method'] = method
        
        instructions = {
            'app': "Введите 6-значный код из приложения аутентификации:",
            'sms': "Введите код из SMS, отправленного на ваш телефон:",
            'whatsapp': "Введите код из сообщения WhatsApp:",
            'call': "Введите код из голосового звонка:"
        }.get(method, "Введите код подтверждения:")
        
        query.edit_message_text(instructions)
        return State.INPUT_2FA_CODE

    def input_2fa_code(self, update: Update, context: CallbackContext):
        """Обработчик ввода кода 2FA"""
        if update.callback_query:
            update.callback_query.answer()
            update.callback_query.edit_message_text("Введите код:")
            return State.INPUT_2FA_CODE
        
        code = update.message.text.strip()
        if not code.isdigit() or len(code) != 6:
            update.message.reply_text("❌ Код должен быть 6-значным числом. Попробуйте снова.")
            return State.INPUT_2FA_CODE
        
        username = context.user_data['username']
        password = context.user_data['password']
        method = context.user_data['2fa_method']
        
        try:
            if self.data_manager.add_account(username, password, code, method):
                update.message.reply_text(f"✅ Аккаунт {username} успешно добавлен!")
            else:
                update.message.reply_text("❌ Не удалось добавить аккаунт. Проверьте код и попробуйте снова.")
                return State.INPUT_2FA_CODE
        except TwoFactorRequired:
            update.message.reply_text("❌ Неверный код двухфакторной аутентификации. Попробуйте снова.")
            return State.INPUT_2FA_CODE
        except Exception as e:
            update.message.reply_text(f"❌ Ошибка: {str(e)}")
        
        # Очищаем user_data
        context.user_data.clear()
        return ConversationHandler.END

    # ==============================================
    # ОБРАБОТЧИКИ ДОБАВЛЕНИЯ ПОСТА
    # ==============================================

    def add_post_start(self, update: Update, context: CallbackContext):
        """Начало процесса добавления поста"""
        if not self.data_manager.accounts:
            update.message.reply_text("❌ Сначала добавьте хотя бы один аккаунт Instagram с помощью /add_account")
            return ConversationHandler.END
        
        keyboard = [[KeyboardButton(username)] for username in self.data_manager.accounts.keys()]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        
        update.message.reply_text(
            "👤 Выберите аккаунт для публикации:",
            reply_markup=reply_markup
        )
        return State.SELECT_ACCOUNT

    def select_account(self, update: Update, context: CallbackContext):
        """Обработчик выбора аккаунта"""
        selected_account = update.message.text
        if selected_account not in self.data_manager.accounts:
            update.message.reply_text("❌ Неверный аккаунт. Попробуйте снова.")
            return State.SELECT_ACCOUNT
        
        context.user_data['target_account'] = selected_account
        update.message.reply_text(
            "📸 Отправьте мне фото или несколько фото для поста (как альбом), "
            "а затем подпись к посту. После этого я запрошу время публикации.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data['awaiting_post'] = True
        context.user_data['post_media'] = []
        
        return State.INPUT_TARGET_ACCOUNT

    def handle_media(self, update: Update, context: CallbackContext):
        """Обработчик медиафайлов (фото)"""
        if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
            if update.message.photo:
                photo = update.message.photo[-1].get_file()
                file_path = f"{Config.TEMP_DIR}/post_{update.update_id}.jpg"
                context.user_data['post_media'].append(photo.download(custom_path=file_path))
                update.message.reply_text("✅ Фото добавлено. Отправьте еще фото или /done чтобы продолжить.")
        
        elif 'awaiting_story' in context.user_data and context.user_data['awaiting_story']:
            if update.message.photo:
                photo = update.message.photo[-1].get_file()
                file_path = f"{Config.TEMP_DIR}/story_{update.update_id}.jpg"
                context.user_data['story_media'].append(photo.download(custom_path=file_path))
                update.message.reply_text("✅ Фото для сторис добавлено. Отправьте еще фото или /done чтобы продолжить.")

    def handle_text(self, update: Update, context: CallbackContext):
        """Обработчик текстовых сообщений"""
        if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
            if 'post_media' in context.user_data and context.user_data['post_media']:
                context.user_data['post_caption'] = update.message.text
                update.message.reply_text(
                    "⏰ Введите время публикации в формате:\n"
                    "DD.MM.YYYY HH:MM (например: 25.12.2023 15:30)\n"
                    "Или 'now' для немедленной публикации."
                )
                return State.INPUT_POST_TIME
            else:
                update.message.reply_text("❌ Сначала отправьте фото для поста.")

    def input_post_caption(self, update: Update, context: CallbackContext):
        """Обработчик ввода подписи к посту"""
        context.user_data['post_caption'] = update.message.text
        update.message.reply_text(
            "⏰ Введите время публикации в формате:\n"
            "DD.MM.YYYY HH:MM (например: 25.12.2023 15:30)\n"
            "Или 'now' для немедленной публикации."
        )
        return State.INPUT_POST_TIME

    def input_post_time(self, update: Update, context: CallbackContext):
        """Обработчик ввода времени публикации поста"""
        text = update.message.text.lower()
        
        try:
            if text == 'now':
                publish_time = datetime.now(pytz.utc)
            else:
                publish_time = datetime.strptime(text, "%d.%m.%Y %H:%M")
                publish_time = pytz.timezone('Europe/Moscow').localize(publish_time).astimezone(pytz.utc)
            
            media = context.user_data['post_media']
            caption = context.user_data.get('post_caption', '')
            target_account = context.user_data.get('target_account')
            
            if len(media) == 1:
                content = media[0]
            else:
                content = media
            
            post = self.data_manager.add_to_queue('post', content, caption, publish_time, target_account)
            local_time = publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
            
            update.message.reply_text(
                f"✅ Пост добавлен в очередь:\n"
                f"⏰ Время: {local_time}\n"
                f"👤 Аккаунт: {target_account}\n"
                f"📌 Всего в очереди постов: {len(self.data_manager.post_queue)}"
            )
            
            # Очищаем контекст
            context.user_data.clear()
            return ConversationHandler.END
        
        except ValueError:
            update.message.reply_text("❌ Неверный формат времени. Попробуйте снова.")
            return State.INPUT_POST_TIME

    def done(self, update: Update, context: CallbackContext):
        """Обработчик команды /done - завершение ввода медиа"""
        if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
            if 'post_media' in context.user_data and context.user_data['post_media']:
                update.message.reply_text("📝 Теперь введите подпись к посту.")
                return State.INPUT_POST_CAPTION
            else:
                update.message.reply_text("❌ Вы не отправили ни одного фото. Отправьте фото или /cancel чтобы отменить.")
        
        elif 'awaiting_story' in context.user_data and context.user_data['awaiting_story']:
            if 'story_media' in context.user_data and context.user_data['story_media']:
                update.message.reply_text(
                    "⏰ Введите время публикации в формате:\n"
                    "DD.MM.YYYY HH:MM (например: 25.12.2023 15:30)\n"
                    "Или 'now' для немедленной публикации."
                )
                return State.INPUT_STORY_TIME
            else:
                update.message.reply_text("❌ Вы не отправили ни одного фото. Отправьте фото или /cancel чтобы отменить.")

    def handle_time(self, update: Update, context: CallbackContext):
        """Обработчик времени публикации"""
        text = update.message.text.lower()
        
        try:
            if text == 'now':
                publish_time = datetime.now(pytz.utc)
            else:
                publish_time = datetime.strptime(text, "%d.%m.%Y %H:%M")
                publish_time = pytz.timezone('Europe/Moscow').localize(publish_time).astimezone(pytz.utc)
            
            if 'awaiting_story_time' in context.user_data and context.user_data['awaiting_story_time']:
                media = context.user_data['story_media']
                target_account = context.user_data.get('target_account')
                
                if len(media) == 1:
                    content = media[0]
                else:
                    content = media
                
                story = self.data_manager.add_to_queue('story', content, None, publish_time, target_account)
                local_time = publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
                
                update.message.reply_text(
                    f"✅ Сторис добавлен в очередь:\n"
                    f"⏰ Время: {local_time}\n"
                    f"👤 Аккаунт: {target_account}\n"
                    f"📌 Всего в очереди сторис: {len(self.data_manager.stories_queue)}"
                )
                
                # Очищаем контекст
                context.user_data.clear()
                return ConversationHandler.END
        
        except ValueError:
            update.message.reply_text("❌ Неверный формат времени. Попробуйте снова.")
            return State.INPUT_STORY_TIME

    def error_handler(self, update: Update, context: CallbackContext):
        """Обработчик ошибок"""
        logger.error(msg="Ошибка в обработчике Telegram:", exc_info=context.error)
        if update and update.message:
            update.message.reply_text('❌ Произошла ошибка. Пожалуйста, попробуйте еще раз.')

# ==============================================
# ЗАПУСК БОТА
# ==============================================

if __name__ == '__main__':
    bot = InstagramBot()
    bot.start()
