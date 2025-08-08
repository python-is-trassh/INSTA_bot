import os
import logging
import asyncio
import json
import re
import hashlib
import base64
from datetime import datetime, timedelta
from threading import Thread, Lock
from time import sleep
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from functools import wraps
import pytz

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import sqlite3
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ConversationHandler, CallbackQueryHandler
)
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, TwoFactorRequired, ChallengeRequired, 
    ClientError, BadPassword, ReloginAttemptExceeded
)

# ==================== КОНФИГУРАЦИЯ ====================

@dataclass
class BotConfig:
    telegram_token: str
    database_url: str = 'sqlite:///instagram_bot.db'
    temp_dir: str = 'tmp'
    scheduler_interval: int = 10
    max_retries: int = 3
    log_level: str = 'INFO'
    allowed_users: Optional[List[int]] = None
    encryption_password: str = 'default_password'
    max_file_size: int = 50 * 1024 * 1024  # 50MB
    max_video_duration: int = 60  # seconds for reels
    weekly_reports: bool = True

# ==================== БАЗА ДАННЫХ ====================

Base = declarative_base()

class InstagramAccount(Base):
    __tablename__ = 'accounts'
    
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    encrypted_password = Column(Text, nullable=False)
    user_id = Column(String)
    verification_method = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime)
    is_active = Column(Boolean, default=True)
    posts_count = Column(Integer, default=0)
    stories_count = Column(Integer, default=0)
    reels_count = Column(Integer, default=0)

class Publication(Base):
    __tablename__ = 'publications'
    
    id = Column(Integer, primary_key=True)
    account_username = Column(String, nullable=False)
    content_type = Column(String, nullable=False)  # post, story, reel
    media_type = Column(String, nullable=False)    # photo, video
    media_paths = Column(Text)  # JSON list of file paths
    caption = Column(Text)
    publish_time = Column(DateTime, nullable=False)
    status = Column(String, default='queued')  # queued, published, failed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    published_at = Column(DateTime)
    error_message = Column(Text)
    likes_count = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    views_count = Column(Integer, default=0)

class BotMetrics(Base):
    __tablename__ = 'metrics'
    
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, default=datetime.utcnow)
    posts_published = Column(Integer, default=0)
    stories_published = Column(Integer, default=0)
    reels_published = Column(Integer, default=0)
    failed_publications = Column(Integer, default=0)
    active_accounts = Column(Integer, default=0)

class UserSettings(Base):
    __tablename__ = 'user_settings'
    
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(Integer, unique=True, nullable=False)
    notifications_enabled = Column(Boolean, default=True)
    weekly_reports = Column(Boolean, default=True)
    timezone = Column(String, default='UTC')
    language = Column(String, default='ru')

# ==================== БЕЗОПАСНОСТЬ ====================

class SecurityManager:
    def __init__(self, password: str):
        self.password = password.encode()
        self._key = None
    
    def _get_key(self) -> bytes:
        if self._key is None:
            salt = b'instagram_bot_salt'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            self._key = base64.urlsafe_b64encode(kdf.derive(self.password))
        return self._key
    
    def encrypt(self, data: str) -> str:
        f = Fernet(self._get_key())
        return f.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted_data: str) -> str:
        f = Fernet(self._get_key())
        return f.decrypt(encrypted_data.encode()).decode()
    
    def hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

def check_user_access(allowed_users: Optional[List[int]] = None):
    def decorator(func):
        @wraps(func)
        def wrapper(self, update: Update, context: CallbackContext, *args, **kwargs):
            if allowed_users and update.effective_user.id not in allowed_users:
                update.message.reply_text("❌ У вас нет доступа к этому боту")
                return
            return func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator

def retry(max_attempts: int = 3, delay: int = 1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    sleep(delay * (2 ** attempt))
            return None
        return wrapper
    return decorator

def validate_input(input_type: str):
    def decorator(func):
        @wraps(func)
        def wrapper(self, update: Update, context: CallbackContext, *args, **kwargs):
            text = update.message.text if update.message else ""
            
            if input_type == 'username':
                if not re.match(r'^[a-zA-Z0-9._]{1,30}$', text):
                    update.message.reply_text("❌ Некорректный username")
                    return
            elif input_type == '2fa_code':
                if not re.match(r'^\d{6}$', text):
                    update.message.reply_text("❌ Код должен содержать 6 цифр")
                    return
            elif input_type == 'time':
                if text.lower() != 'now' and not re.match(r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$', text):
                    update.message.reply_text("❌ Неверный формат времени (ДД.ММ.ГГГГ ЧЧ:ММ)")
                    return
            
            return func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator

# ==================== ИСКЛЮЧЕНИЯ ====================

class InstagramBotError(Exception):
    pass

class AccountNotFoundError(InstagramBotError):
    pass

class PublishError(InstagramBotError):
    pass

class SecurityError(InstagramBotError):
    pass

class ValidationError(InstagramBotError):
    pass

# ==================== СОСТОЯНИЯ ====================

(
    SELECT_ACCOUNT, INPUT_USERNAME, INPUT_PASSWORD,
    SELECT_2FA_METHOD, INPUT_2FA_CODE, INPUT_TARGET_ACCOUNT,
    INPUT_POST_CAPTION, INPUT_POST_TIME, INPUT_STORY_TIME,
    SELECT_CONTENT_TYPE, SELECT_MEDIA_TYPE, UPLOAD_MEDIA,
    INPUT_REEL_CAPTION, INPUT_REEL_TIME, SETTINGS_MENU,
    EDIT_NOTIFICATIONS, EDIT_TIMEZONE
) = range(16)

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class EnhancedInstagramBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.security = SecurityManager(config.encryption_password)
        self.account_lock = Lock()
        self.scheduler_running = False
        
        # Настройка логирования
        self._setup_logging()
        
        # Инициализация базы данных
        self.engine = create_engine(config.database_url)
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.db_session = Session()
        
        # Создание временной директории
        if not os.path.exists(config.temp_dir):
            os.makedirs(config.temp_dir)
        
        self.logger.info("Bot initialized successfully")

    def _setup_logging(self):
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=getattr(logging, self.config.log_level),
            handlers=[
                logging.FileHandler('enhanced_bot.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    # ==================== УПРАВЛЕНИЕ АККАУНТАМИ ====================

    @retry(max_attempts=3)
    def init_instagram_client(self, username: str, password: str, 
                             verification_code: str = None, 
                             verification_method: str = None) -> Client:
        """Инициализация Instagram клиента с обработкой 2FA"""
        cl = Client()
        try:
            if verification_code and verification_method:
                if verification_method == 'email':
                    cl.login(username, password, verification_code=verification_code)
                else:
                    cl.login(username, password)
                    if hasattr(cl, 'challenge_code_handler'):
                        cl.challenge_code_handler(username, password)
            else:
                cl.login(username, password)
            return cl
        except Exception as e:
            self.logger.error(f"Login error for {username}: {e}")
            raise

    def get_2fa_methods(self, username: str, password: str) -> List[str]:
        """Получение доступных методов 2FA"""
        cl = Client()
        try:
            cl.login(username, password)
            return []
        except TwoFactorRequired as e:
            return getattr(e, 'allowed_methods', ['app', 'sms', 'whatsapp', 'call', 'email'])
        except Exception as e:
            self.logger.error(f"2FA check error: {e}")
            return []

    def add_account(self, username: str, password: str, 
                   verification_code: str = None, 
                   verification_method: str = None) -> bool:
        """Добавление Instagram аккаунта"""
        try:
            cl = self.init_instagram_client(username, password, 
                                          verification_code, verification_method)
            
            encrypted_password = self.security.encrypt(password)
            
            account = InstagramAccount(
                username=username,
                encrypted_password=encrypted_password,
                user_id=str(cl.user_id),
                verification_method=verification_method,
                last_used=datetime.utcnow()
            )
            
            # Проверка на дубликаты
            existing = self.db_session.query(InstagramAccount).filter_by(
                username=username
            ).first()
            
            if existing:
                existing.encrypted_password = encrypted_password
                existing.verification_method = verification_method
                existing.last_used = datetime.utcnow()
                existing.is_active = True
            else:
                self.db_session.add(account)
            
            self.db_session.commit()
            self.logger.info(f"Account {username} added successfully")
            return True
            
        except Exception as e:
            self.db_session.rollback()
            self.logger.error(f"Add account error: {e}")
            return False

    def get_account_client(self, username: str) -> Optional[Client]:
        """Получение клиента для аккаунта"""
        account = self.db_session.query(InstagramAccount).filter_by(
            username=username, is_active=True
        ).first()
        
        if not account:
            raise AccountNotFoundError(f"Account {username} not found")
        
        try:
            password = self.security.decrypt(account.encrypted_password)
            cl = self.init_instagram_client(username, password)
            
            # Обновляем время последнего использования
            account.last_used = datetime.utcnow()
            self.db_session.commit()
            
            return cl
        except Exception as e:
            self.logger.error(f"Failed to get client for {username}: {e}")
            return None

    # ==================== МЕДИА УТИЛИТЫ ====================

    def validate_media_file(self, file_path: str, media_type: str, content_type: str) -> bool:
        """Валидация медиафайлов"""
        if not os.path.exists(file_path):
            return False
        
        file_size = os.path.getsize(file_path)
        if file_size > self.config.max_file_size:
            return False
        
        if media_type == 'video':
            # Здесь можно добавить проверку длительности видео
            # Для простоты пропускаем
            pass
        
        return True

    def get_video_duration(self, file_path: str) -> float:
        """Получение длительности видео (заглушка)"""
        # В реальной реализации используйте ffprobe или moviepy
        return 30.0  # Заглушка

    # ==================== ПУБЛИКАЦИЯ КОНТЕНТА ====================

    @retry(max_attempts=3)
    def publish_post(self, publication: Publication) -> bool:
        """Публикация поста"""
        try:
            cl = self.get_account_client(publication.account_username)
            if not cl:
                publication.status = 'failed'
                publication.error_message = 'Failed to get Instagram client'
                return False

            media_paths = json.loads(publication.media_paths)
            
            if publication.media_type == 'photo':
                if len(media_paths) == 1:
                    # Одиночное фото
                    media = cl.photo_upload(media_paths[0], publication.caption)
                else:
                    # Альбом фото
                    media = cl.album_upload(media_paths, publication.caption)
            elif publication.media_type == 'video':
                if len(media_paths) == 1:
                    # Одиночное видео
                    media = cl.video_upload(media_paths[0], publication.caption)
                else:
                    # Альбом с видео
                    media = cl.album_upload(media_paths, publication.caption)
            
            publication.status = 'published'
            publication.published_at = datetime.utcnow()
            
            # Обновляем счетчики
            account = self.db_session.query(InstagramAccount).filter_by(
                username=publication.account_username
            ).first()
            if account:
                account.posts_count += 1
            
            self.db_session.commit()
            self.logger.info(f"Post published successfully for {publication.account_username}")
            return True
            
        except Exception as e:
            publication.status = 'failed'
            publication.error_message = str(e)
            self.db_session.commit()
            self.logger.error(f"Post publish error: {e}")
            return False

    @retry(max_attempts=3)
    def publish_story(self, publication: Publication) -> bool:
        """Публикация истории"""
        try:
            cl = self.get_account_client(publication.account_username)
            if not cl:
                publication.status = 'failed'
                publication.error_message = 'Failed to get Instagram client'
                return False

            media_paths = json.loads(publication.media_paths)
            
            for media_path in media_paths:
                if publication.media_type == 'photo':
                    cl.photo_upload_to_story(media_path)
                elif publication.media_type == 'video':
                    cl.video_upload_to_story(media_path)
            
            publication.status = 'published'
            publication.published_at = datetime.utcnow()
            
            # Обновляем счетчики
            account = self.db_session.query(InstagramAccount).filter_by(
                username=publication.account_username
            ).first()
            if account:
                account.stories_count += 1
            
            self.db_session.commit()
            self.logger.info(f"Story published successfully for {publication.account_username}")
            return True
            
        except Exception as e:
            publication.status = 'failed'
            publication.error_message = str(e)
            self.db_session.commit()
            self.logger.error(f"Story publish error: {e}")
            return False

    @retry(max_attempts=3)
    def publish_reel(self, publication: Publication) -> bool:
        """Публикация рилса"""
        try:
            cl = self.get_account_client(publication.account_username)
            if not cl:
                publication.status = 'failed'
                publication.error_message = 'Failed to get Instagram client'
                return False

            media_paths = json.loads(publication.media_paths)
            video_path = media_paths[0]  # Рилс - это всегда одно видео
            
            # Проверяем длительность видео для рилса
            duration = self.get_video_duration(video_path)
            if duration > self.config.max_video_duration:
                raise ValidationError(f"Video too long: {duration}s (max: {self.config.max_video_duration}s)")
            
            media = cl.clip_upload(video_path, publication.caption)
            
            publication.status = 'published'
            publication.published_at = datetime.utcnow()
            
            # Обновляем счетчики
            account = self.db_session.query(InstagramAccount).filter_by(
                username=publication.account_username
            ).first()
            if account:
                account.reels_count += 1
            
            self.db_session.commit()
            self.logger.info(f"Reel published successfully for {publication.account_username}")
            return True
            
        except Exception as e:
            publication.status = 'failed'
            publication.error_message = str(e)
            self.db_session.commit()
            self.logger.error(f"Reel publish error: {e}")
            return False

    def add_to_queue(self, content_type: str, media_type: str, media_paths: List[str],
                    caption: str = None, publish_time: datetime = None, 
                    account_username: str = None) -> Publication:
        """Добавление контента в очередь публикации"""
        
        publication = Publication(
            account_username=account_username,
            content_type=content_type,
            media_type=media_type,
            media_paths=json.dumps(media_paths),
            caption=caption,
            publish_time=publish_time or datetime.utcnow(),
            status='queued'
        )
        
        self.db_session.add(publication)
        self.db_session.commit()
        
        return publication

    # ==================== ПЛАНИРОВЩИК ====================

    def scheduler(self):
        """Планировщик публикаций"""
        self.scheduler_running = True
        
        while self.scheduler_running:
            try:
                current_time = datetime.utcnow()
                
                # Получаем все публикации, готовые к отправке
                publications = self.db_session.query(Publication).filter(
                    Publication.status == 'queued',
                    Publication.publish_time <= current_time
                ).all()
                
                for pub in publications:
                    try:
                        if pub.content_type == 'post':
                            self.publish_post(pub)
                        elif pub.content_type == 'story':
                            self.publish_story(pub)
                        elif pub.content_type == 'reel':
                            self.publish_reel(pub)
                        
                        # Отправляем уведомление о публикации
                        self.send_publish_notification(pub)
                        
                    except Exception as e:
                        self.logger.error(f"Publication error: {e}")
                        pub.status = 'failed'
                        pub.error_message = str(e)
                        self.db_session.commit()
                
                sleep(self.config.scheduler_interval)
                
            except Exception as e:
                self.logger.error(f"Scheduler error: {e}")
                sleep(30)

    # ==================== УВЕДОМЛЕНИЯ ====================

    def send_publish_notification(self, publication: Publication):
        """Отправка уведомления о публикации"""
        # Здесь должна быть логика отправки уведомлений пользователям
        pass

    def send_weekly_report(self, user_id: int):
        """Отправка еженедельного отчета"""
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=7)
        
        # Получаем статистику за неделю
        publications = self.db_session.query(Publication).filter(
            Publication.created_at >= start_date,
            Publication.created_at <= end_date
        ).all()
        
        posts_published = len([p for p in publications if p.content_type == 'post' and p.status == 'published'])
        stories_published = len([p for p in publications if p.content_type == 'story' and p.status == 'published'])
        reels_published = len([p for p in publications if p.content_type == 'reel' and p.status == 'published'])
        failed_publications = len([p for p in publications if p.status == 'failed'])
        
        active_accounts = self.db_session.query(InstagramAccount).filter(
            InstagramAccount.is_active == True
        ).count()
        
        report = f"""
📊 <b>Отчет за неделю</b>

✅ Опубликовано постов: {posts_published}
📸 Опубликовано Stories: {stories_published}
🎬 Опубликовано Reels: {reels_published}
❌ Ошибок публикации: {failed_publications}
👤 Активных аккаунтов: {active_accounts}

📅 Период: {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}
        """
        
        # Здесь должна быть отправка отчета пользователю
        return report

    # ==================== TELEGRAM ОБРАБОТЧИКИ ====================

    @check_user_access()
    def start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start"""
        keyboard = [
            [
                InlineKeyboardButton("📱 Аккаунты", callback_data="menu_accounts"),
                InlineKeyboardButton("📝 Добавить пост", callback_data="menu_add_post")
            ],
            [
                InlineKeyboardButton("📸 Добавить Story", callback_data="menu_add_story"),
                InlineKeyboardButton("🎬 Добавить Reel", callback_data="menu_add_reel")
            ],
            [
                InlineKeyboardButton("📋 Очередь", callback_data="menu_queue"),
                InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
                InlineKeyboardButton("❓ Помощь", callback_data="menu_help")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = """
🤖 <b>Enhanced Instagram Bot</b>

Добро пожаловать! Этот бот поможет вам:

📱 Управлять Instagram аккаунтами
📝 Планировать посты с фото/видео
📸 Публиковать Stories
🎬 Создавать Reels
📊 Отслеживать статистику
⏰ Автоматически публиковать по расписанию

Выберите действие:
        """
        
        update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )

    def callback_query_handler(self, update: Update, context: CallbackContext):
        """Обработчик inline кнопок"""
        query = update.callback_query
        query.answer()
        
        if query.data == "menu_accounts":
            self.show_accounts_menu(update, context)
        elif query.data == "menu_add_post":
            self.start_add_content(update, context, 'post')
        elif query.data == "menu_add_story":
            self.start_add_content(update, context, 'story')
        elif query.data == "menu_add_reel":
            self.start_add_content(update, context, 'reel')
        elif query.data == "menu_queue":
            self.show_queue(update, context)
        elif query.data == "menu_stats":
            self.show_statistics(update, context)
        elif query.data == "menu_settings":
            self.show_settings_menu(update, context)
        elif query.data == "menu_help":
            self.show_help(update, context)

    def show_accounts_menu(self, update: Update, context: CallbackContext):
        """Показать меню аккаунтов"""
        accounts = self.db_session.query(InstagramAccount).filter_by(is_active=True).all()
        
        if not accounts:
            text = "❌ Нет добавленных аккаунтов"
            keyboard = [[InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")]]
        else:
            text = "<b>📱 Ваши аккаунты:</b>\n\n"
            keyboard = []
            
            for account in accounts:
                last_used = account.last_used.strftime('%d.%m.%Y %H:%M') if account.last_used else 'Никогда'
                text += f"👤 <b>{account.username}</b>\n"
                text += f"📊 Посты: {account.posts_count} | Stories: {account.stories_count} | Reels: {account.reels_count}\n"
                text += f"🕒 Последнее использование: {last_used}\n\n"
                
                keyboard.append([
                    InlineKeyboardButton(f"📊 {account.username}", callback_data=f"account_stats_{account.username}"),
                    InlineKeyboardButton("❌", callback_data=f"account_delete_{account.username}")
                ])
            
            keyboard.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")])
        
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        else:
            update.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )

    def show_queue(self, update: Update, context: CallbackContext):
        """Показать очередь публикаций"""
        publications = self.db_session.query(Publication).filter(
            Publication.status.in_(['queued', 'failed'])
        ).order_by(Publication.publish_time).limit(20).all()
        
        if not publications:
            text = "📋 Очередь публикаций пуста"
        else:
            text = "<b>📋 Очередь публикаций:</b>\n\n"
            
            for pub in publications:
                status_emoji = "⏳" if pub.status == 'queued' else "❌"
                content_emoji = {"post": "📝", "story": "📸", "reel": "🎬"}.get(pub.content_type, "📄")
                media_emoji = {"photo": "🖼️", "video": "🎥"}.get(pub.media_type, "📄")
                
                time_str = pub.publish_time.strftime('%d.%m.%Y %H:%M')
                text += f"{status_emoji} {content_emoji} {media_emoji} <b>{pub.account_username}</b>\n"
                text += f"📅 {time_str}\n"
                
                if pub.caption:
                    caption_preview = pub.caption[:50] + "..." if len(pub.caption) > 50 else pub.caption
                    text += f"💬 {caption_preview}\n"
                
                if pub.status == 'failed' and pub.error_message:
                    text += f"❌ {pub.error_message[:50]}...\n"
                
                text += "\n"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_queue")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )

    def show_statistics(self, update: Update, context: CallbackContext):
        """Показать статистику"""
        # Статистика за последние 30 дней
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=30)
        
        publications = self.db_session.query(Publication).filter(
            Publication.created_at >= start_date
        ).all()
        
        total_posts = len([p for p in publications if p.content_type == 'post'])
        total_stories = len([p for p in publications if p.content_type == 'story'])
        total_reels = len([p for p in publications if p.content_type == 'reel'])
        
        published_posts = len([p for p in publications if p.content_type == 'post' and p.status == 'published'])
        published_stories = len([p for p in publications if p.content_type == 'story' and p.status == 'published'])
        published_reels = len([p for p in publications if p.content_type == 'reel' and p.status == 'published'])
        
        failed_total = len([p for p in publications if p.status == 'failed'])
        
        active_accounts = self.db_session.query(InstagramAccount).filter_by(is_active=True).count()
        
        text = f"""
<b>📊 Статистика за 30 дней</b>

👤 <b>Аккаунты:</b> {active_accounts} активных

📝 <b>Посты:</b> {published_posts}/{total_posts} опубликовано
📸 <b>Stories:</b> {published_stories}/{total_stories} опубликовано  
🎬 <b>Reels:</b> {published_reels}/{total_reels} опубликовано

❌ <b>Ошибок:</b> {failed_total}

📈 <b>Успешность:</b> {((published_posts + published_stories + published_reels) / max(total_posts + total_stories + total_reels, 1) * 100):.1f}%
        """
        
        keyboard = [
            [InlineKeyboardButton("📄 Подробный отчет", callback_data="detailed_report")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )

    def show_settings_menu(self, update: Update, context: CallbackContext):
        """Показать меню настроек"""
        user_id = update.effective_user.id
        settings = self.db_session.query(UserSettings).filter_by(
            telegram_user_id=user_id
        ).first()
        
        if not settings:
            settings = UserSettings(telegram_user_id=user_id)
            self.db_session.add(settings)
            self.db_session.commit()
        
        notifications_status = "✅" if settings.notifications_enabled else "❌"
        reports_status = "✅" if settings.weekly_reports else "❌"
        
        text = f"""
<b>⚙️ Настройки</b>

🔔 Уведомления: {notifications_status}
📊 Еженедельные отчеты: {reports_status}
🌍 Часовой пояс: {settings.timezone}
🗣️ Язык: {settings.language}
        """
        
        keyboard = [
            [InlineKeyboardButton("🔔 Переключить уведомления", callback_data="toggle_notifications")],
            [InlineKeyboardButton("📊 Переключить отчеты", callback_data="toggle_reports")],
            [InlineKeyboardButton("🌍 Изменить часовой пояс", callback_data="change_timezone")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )

    def show_help(self, update: Update, context: CallbackContext):
        """Показать справку"""
        text = """
<b>❓ Справка</b>

<b>🤖 Как пользоваться ботом:</b>

1️⃣ <b>Добавьте аккаунты</b>
   Перейдите в раздел "Аккаунты" и добавьте ваши Instagram аккаунты

2️⃣ <b>Создавайте контент</b>
   • 📝 Посты - обычные публикации с фото/видео
   • 📸 Stories - истории (исчезают через 24 часа)
   • 🎬 Reels - короткие видео до 60 секунд

3️⃣ <b>Планируйте публикации</b>
   Укажите время публикации или выберите "Сейчас"

4️⃣ <b>Отслеживайте статистику</b>
   Следите за успешностью публикаций в разделе "Статистика"

<b>📋 Поддерживаемые форматы:</b>
• Фото: JPG, PNG
• Видео: MP4, MOV
• Максимальный размер файла: 50MB
• Максимальная длительность Reels: 60 секунд

<b>🔒 Безопасность:</b>
Все данные аккаунтов зашифрованы и хранятся локально
        """
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )

    # ==================== ДОБАВЛЕНИЕ КОНТЕНТА ====================

    def start_add_content(self, update: Update, context: CallbackContext, content_type: str):
        """Начало процесса добавления контента"""
        accounts = self.db_session.query(InstagramAccount).filter_by(is_active=True).all()
        
        if not accounts:
            update.callback_query.edit_message_text(
                "❌ Нет добавленных аккаунтов. Сначала добавьте аккаунт.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")
                ]])
            )
            return

        keyboard = []
        for account in accounts:
            keyboard.append([InlineKeyboardButton(
                f"👤 {account.username}", 
                callback_data=f"select_account_{content_type}_{account.username}"
            )])
        
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])
        
        content_names = {"post": "поста", "story": "Story", "reel": "Reel"}
        
        update.callback_query.edit_message_text(
            f"📱 Выберите аккаунт для {content_names[content_type]}:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        context.user_data['content_type'] = content_type

    def handle_account_selection(self, update: Update, context: CallbackContext):
        """Обработка выбора аккаунта"""
        query = update.callback_query
        query.answer()
        
        # Парсим callback_data: select_account_{content_type}_{username}
        parts = query.data.split('_', 3)
        if len(parts) != 4:
            return
        
        content_type = parts[2]
        username = parts[3]
        
        context.user_data['target_account'] = username
        context.user_data['content_type'] = content_type
        
        # Определяем тип медиа
        if content_type == 'reel':
            # Для рилсов только видео
            context.user_data['media_type'] = 'video'
            self.request_media_upload(update, context)
        else:
            # Для постов и сторис даем выбор
            keyboard = [
                [
                    InlineKeyboardButton("🖼️ Фото", callback_data=f"media_type_photo_{content_type}"),
                    InlineKeyboardButton("🎥 Видео", callback_data=f"media_type_video_{content_type}")
                ],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
            ]
            
            query.edit_message_text(
                f"📎 Выберите тип медиа для {content_type}:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    def handle_media_type_selection(self, update: Update, context: CallbackContext):
        """Обработка выбора типа медиа"""
        query = update.callback_query
        query.answer()
        
        # Парсим callback_data: media_type_{photo/video}_{content_type}
        parts = query.data.split('_', 3)
        if len(parts) != 4:
            return
        
        media_type = parts[2]  # photo или video
        content_type = parts[3]  # post, story, reel
        
        context.user_data['media_type'] = media_type
        context.user_data['content_type'] = content_type
        
        self.request_media_upload(update, context)

    def request_media_upload(self, update: Update, context: CallbackContext):
        """Запрос загрузки медиафайлов"""
        content_type = context.user_data.get('content_type')
        media_type = context.user_data.get('media_type')
        
        context.user_data['uploaded_media'] = []
        
        media_emoji = "🖼️" if media_type == 'photo' else "🎥"
        content_emoji = {"post": "📝", "story": "📸", "reel": "🎬"}.get(content_type, "📄")
        
        if content_type == 'reel':
            text = f"{content_emoji} Отправьте видео для Reel\n\n⚠️ Требования:\n• Максимум {self.config.media.max_reel_duration} секунд\n• Форматы: MP4, MOV\n• Размер до {self.config.media.max_file_size // (1024*1024)}MB"
        else:
            text = f"{content_emoji} Отправьте {media_emoji} {media_type} для {content_type}\n\n💡 Можно отправить несколько файлов для альбома\nНажмите /done когда закончите"
        
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_upload")]]
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )

    def handle_media_upload(self, update: Update, context: CallbackContext):
        """Обработка загруженных медиафайлов"""
        if 'uploaded_media' not in context.user_data:
            return

        media_type = context.user_data.get('media_type')
        content_type = context.user_data.get('content_type')
        
        file_obj = None
        if update.message.photo and media_type == 'photo':
            file_obj = update.message.photo[-1].get_file()
            extension = 'jpg'
        elif update.message.video and media_type == 'video':
            file_obj = update.message.video.get_file()
            extension = 'mp4'
        elif update.message.document:
            file_obj = update.message.document.get_file()
            extension = file_obj.file_path.split('.')[-1].lower()
        
        if not file_obj:
            update.message.reply_text("❌ Неподдерживаемый тип файла")
            return
        
        # Проверяем размер файла
        if file_obj.file_size > self.config.media.max_file_size:
            size_mb = self.config.media.max_file_size // (1024 * 1024)
            update.message.reply_text(f"❌ Файл слишком большой. Максимум {size_mb}MB")
            return
        
        # Проверяем формат
        allowed_formats = (self.config.media.allowed_photo_formats if media_type == 'photo' 
                          else self.config.media.allowed_video_formats)
        
        if extension not in allowed_formats:
            update.message.reply_text(f"❌ Неподдерживаемый формат. Разрешены: {', '.join(allowed_formats)}")
            return
        
        # Скачиваем файл
        try:
            timestamp = int(datetime.utcnow().timestamp())
            filename = f"{content_type}_{timestamp}_{len(context.user_data['uploaded_media'])}.{extension}"
            file_path = os.path.join(self.config.media.temp_dir, filename)
            
            file_obj.download(custom_path=file_path)
            
            # Для видео проверяем длительность
            if media_type == 'video':
                duration = self.get_video_duration(file_path)
                max_duration = (self.config.media.max_reel_duration if content_type == 'reel' 
                               else self.config.media.max_video_duration)
                
                if duration > max_duration:
                    os.remove(file_path)
                    update.message.reply_text(f"❌ Видео слишком длинное. Максимум {max_duration} секунд")
                    return
            
            context.user_data['uploaded_media'].append(file_path)
            
            if content_type == 'reel':
                # Для рилса сразу переходим к описанию
                self.request_caption_input(update, context)
            else:
                count = len(context.user_data['uploaded_media'])
                update.message.reply_text(
                    f"✅ Файл #{count} загружен\n\n"
                    f"📁 Загружено файлов: {count}\n"
                    f"💡 Отправьте еще файлы или нажмите /done для продолжения"
                )
        
        except Exception as e:
            self.logger.error(f"Media upload error: {e}")
            update.message.reply_text("❌ Ошибка загрузки файла")

    def handle_done_upload(self, update: Update, context: CallbackContext):
        """Завершение загрузки медиафайлов"""
        if not context.user_data.get('uploaded_media'):
            update.message.reply_text("❌ Вы не загрузили ни одного файла")
            return
        
        content_type = context.user_data.get('content_type')
        
        if content_type in ['post', 'reel']:
            self.request_caption_input(update, context)
        else:  # story
            self.request_publish_time(update, context)

    def request_caption_input(self, update: Update, context: CallbackContext):
        """Запрос ввода подписи"""
        content_type = context.user_data.get('content_type')
        
        if content_type == 'reel':
            text = "✍️ Введите описание для Reel:"
        else:
            text = "✍️ Введите подпись к посту:"
        
        keyboard = [
            [InlineKeyboardButton("➡️ Пропустить", callback_data="skip_caption")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_upload")]
        ]
        
        update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    def handle_caption_input(self, update: Update, context: CallbackContext):
        """Обработка ввода подписи"""
        if update.message and update.message.text:
            context.user_data['caption'] = update.message.text
        elif update.callback_query and update.callback_query.data == "skip_caption":
            context.user_data['caption'] = ""
            update.callback_query.answer()
        
        self.request_publish_time(update, context)

    def request_publish_time(self, update: Update, context: CallbackContext):
        """Запрос времени публикации"""
        content_type = context.user_data.get('content_type')
        
        keyboard = [
            [InlineKeyboardButton("🚀 Опубликовать сейчас", callback_data="publish_now")],
            [InlineKeyboardButton("⏰ Запланировать", callback_data="schedule_later")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_upload")]
        ]
        
        text = f"⏰ Когда опубликовать {content_type}?"
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )

    def handle_publish_time_selection(self, update: Update, context: CallbackContext):
        """Обработка выбора времени публикации"""
        query = update.callback_query
        query.answer()
        
        if query.data == "publish_now":
            context.user_data['publish_time'] = datetime.utcnow()
            self.create_publication(update, context)
        elif query.data == "schedule_later":
            query.edit_message_text(
                "📅 Введите дату и время публикации\n\n"
                "Формат: ДД.ММ.ГГГГ ЧЧ:ММ\n"
                "Пример: 25.12.2024 15:30\n\n"
                "⚠️ Время указывается в вашем часовом поясе"
            )
            # Здесь должен быть обработчик ввода времени

    @validate_input('time')
    def handle_time_input(self, update: Update, context: CallbackContext):
        """Обработка ввода времени"""
        time_text = update.message.text.strip()
        
        try:
            if time_text.lower() == 'now':
                publish_time = datetime.utcnow()
            else:
                # Парсим время
                publish_time = datetime.strptime(time_text, "%d.%m.%Y %H:%M")
                
                # Конвертируем в UTC (предполагаем московское время)
                moscow_tz = pytz.timezone('Europe/Moscow')
                publish_time = moscow_tz.localize(publish_time).astimezone(pytz.utc)
                
                # Проверяем, что время в будущем
                if publish_time <= datetime.utcnow().replace(tzinfo=pytz.utc):
                    update.message.reply_text("❌ Время должно быть в будущем")
                    return
            
            context.user_data['publish_time'] = publish_time.replace(tzinfo=None)
            self.create_publication(update, context)
            
        except ValueError:
            update.message.reply_text("❌ Неверный формат времени. Используйте: ДД.ММ.ГГГГ ЧЧ:ММ")

    def create_publication(self, update: Update, context: CallbackContext):
        """Создание публикации в очереди"""
        try:
            publication = self.add_to_queue(
                content_type=context.user_data['content_type'],
                media_type=context.user_data['media_type'],
                media_paths=context.user_data['uploaded_media'],
                caption=context.user_data.get('caption', ''),
                publish_time=context.user_data['publish_time'],
                account_username=context.user_data['target_account']
            )
            
            # Форматируем время для отображения
            moscow_tz = pytz.timezone('Europe/Moscow')
            display_time = publication.publish_time.replace(tzinfo=pytz.utc).astimezone(moscow_tz)
            
            content_emoji = {"post": "📝", "story": "📸", "reel": "🎬"}.get(publication.content_type, "📄")
            
            success_text = f"""
{content_emoji} <b>Публикация добавлена в очередь!</b>

👤 <b>Аккаунт:</b> {publication.account_username}
📅 <b>Время:</b> {display_time.strftime('%d.%m.%Y %H:%M')}
📁 <b>Файлов:</b> {len(json.loads(publication.media_paths))}
            """
            
            if publication.caption:
                caption_preview = publication.caption[:100] + "..." if len(publication.caption) > 100 else publication.caption
                success_text += f"\n💬 <b>Подпись:</b> {caption_preview}"
            
            keyboard = [
                [InlineKeyboardButton("📋 Показать очередь", callback_data="menu_queue")],
                [InlineKeyboardButton("➕ Добавить еще", callback_data=f"menu_add_{publication.content_type}")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")]
            ]
            
            if update.callback_query:
                update.callback_query.edit_message_text(
                    success_text, 
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                update.message.reply_text(
                    success_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            # Очищаем данные пользователя
            context.user_data.clear()
            
        except Exception as e:
            self.logger.error(f"Create publication error: {e}")
            error_text = "❌ Ошибка при создании публикации"
            
            if update.callback_query:
                update.callback_query.edit_message_text(error_text)
            else:
                update.message.reply_text(error_text)

    # ==================== ДОБАВЛЕНИЕ АККАУНТОВ ====================

    def start_add_account(self, update: Update, context: CallbackContext):
        """Начало добавления аккаунта"""
        text = "👤 Добавление Instagram аккаунта\n\n✍️ Введите имя пользователя (username):"
        
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_account")]]
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )

    @validate_input('username')
    def handle_username_input(self, update: Update, context: CallbackContext):
        """Обработка ввода username"""
        username = update.message.text.strip().lower()
        
        # Проверяем, нет ли уже такого аккаунта
        existing = self.db_session.query(InstagramAccount).filter_by(username=username).first()
        if existing and existing.is_active:
            update.message.reply_text(
                f"❌ Аккаунт @{username} уже добавлен",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")
                ]])
            )
            return
        
        context.user_data['new_username'] = username
        
        update.message.reply_text(
            "🔐 Введите пароль от аккаунта:\n\n"
            "⚠️ Пароль будет зашифрован и сохранен безопасно",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_account")
            ]])
        )

    def handle_password_input(self, update: Update, context: CallbackContext):
        """Обработка ввода пароля"""
        password = update.message.text
        username = context.user_data.get('new_username')
        
        if not username:
            update.message.reply_text("❌ Ошибка: username не найден")
            return
        
        # Удаляем сообщение с паролем для безопасности
        try:
            update.message.delete()
        except:
            pass
        
        context.user_data['new_password'] = password
        
        # Проверяем необходимость 2FA
        try:
            methods = self.get_2fa_methods(username, password)
            
            if not methods:
                # 2FA не требуется, добавляем аккаунт
                if self.add_account(username, password):
                    success_text = f"✅ Аккаунт @{username} успешно добавлен!"
                    keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")]]
                else:
                    success_text = "❌ Не удалось добавить аккаунт. Проверьте данные."
                    keyboard = [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="add_account")]]
                
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=success_text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                context.user_data.clear()
                return
            
            # Показываем доступные методы 2FA
            self.show_2fa_methods(update, context, methods)
            
        except Exception as e:
            self.logger.error(f"Password check error: {e}")
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка входа: {str(e)}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Попробовать снова", callback_data="add_account")
                ]])
            )

    def show_2fa_methods(self, update: Update, context: CallbackContext, methods: List[str]):
        """Показ доступных методов 2FA"""
        method_names = {
            'app': '📱 Приложение',
            'sms': '💬 SMS',
            'whatsapp': '💚 WhatsApp',
            'call': '📞 Звонок',
            'email': '📧 Email'
        }
        
        keyboard = []
        for method in methods:
            if method in method_names:
                keyboard.append([InlineKeyboardButton(
                    method_names[method], 
                    callback_data=f"2fa_method_{method}"
                )])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_account")])
        
        text = "🔐 Требуется двухфакторная аутентификация\n\nВыберите способ получения кода:"
        
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    def handle_2fa_method_selection(self, update: Update, context: CallbackContext):
        """Обработка выбора метода 2FA"""
        query = update.callback_query
        query.answer()
        
        method = query.data.split('_')[-1]  # Извлекаем метод из callback_data
        context.user_data['2fa_method'] = method
        
        method_names = {
            'app': 'приложения',
            'sms': 'SMS',
            'whatsapp': 'WhatsApp',
            'call': 'звонка',
            'email': 'email'
        }
        
        text = f"🔐 Введите 6-значный код из {method_names.get(method, method)}:"
        
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_account")
            ]])
        )

    @validate_input('2fa_code')
    def handle_2fa_code_input(self, update: Update, context: CallbackContext):
        """Обработка ввода кода 2FA"""
        code = update.message.text.strip()
        username = context.user_data.get('new_username')
        password = context.user_data.get('new_password')
        method = context.user_data.get('2fa_method')
        
        # Удаляем сообщение с кодом
        try:
            update.message.delete()
        except:
            pass
        
        if not all([username, password, method]):
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Ошибка: данные не найдены"
            )
            return
        
        # Добавляем аккаунт с 2FA
        if self.add_account(username, password, code, method):
            success_text = f"✅ Аккаунт @{username} успешно добавлен с 2FA!"
            keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")]]
        else:
            success_text = "❌ Не удалось добавить аккаунт. Проверьте код."
            keyboard = [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="add_account")]]
        
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=success_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        context.user_data.clear()

    # ==================== ОБРАБОТЧИКИ НАСТРОЕК ====================

    def handle_settings_callbacks(self, update: Update, context: CallbackContext):
        """Обработка callback'ов настроек"""
        query = update.callback_query
        query.answer()
        
        user_id = update.effective_user.id
        settings = self.db_session.query(UserSettings).filter_by(
            telegram_user_id=user_id
        ).first()
        
        if not settings:
            settings = UserSettings(telegram_user_id=user_id)
            self.db_session.add(settings)
        
        if query.data == "toggle_notifications":
            settings.notifications_enabled = not settings.notifications_enabled
            status = "включены" if settings.notifications_enabled else "отключены"
            query.edit_message_text(f"🔔 Уведомления {status}")
            
        elif query.data == "toggle_reports":
            settings.weekly_reports = not settings.weekly_reports
            status = "включены" if settings.weekly_reports else "отключены"
            query.edit_message_text(f"📊 Еженедельные отчеты {status}")
        
        self.db_session.commit()
        
        # Возвращаемся к меню настроек через 2 секунды
        import time
        time.sleep(2)
        self.show_settings_menu(update, context)

    # ==================== УВЕДОМЛЕНИЯ ====================

    def send_notification(self, user_id: int, message: str, parse_mode=None):
        """Отправка уведомления пользователю"""
        try:
            settings = self.db_session.query(UserSettings).filter_by(
                telegram_user_id=user_id
            ).first()
            
            if settings and not settings.notifications_enabled:
                return  # Уведомления отключены
            
            # Здесь должна быть отправка сообщения через Telegram API
            # context.bot.send_message(user_id, message, parse_mode=parse_mode)
            
        except Exception as e:
            self.logger.error(f"Failed to send notification: {e}")

    def send_publish_notification(self, publication: Publication):
        """Отправка уведомления о публикации"""
        if publication.status == 'published':
            message = f"✅ Успешно опубликовано!\n\n👤 Аккаунт: {publication.account_username}\n📝 Тип: {publication.content_type}"
        else:
            message = f"❌ Ошибка публикации\n\n👤 Аккаунт: {publication.account_username}\n📝 Тип: {publication.content_type}\n🔴 Ошибка: {publication.error_message}"
        
        # Отправляем всем пользователям с включенными уведомлениями
        users = self.db_session.query(UserSettings).filter_by(
            notifications_enabled=True
        ).all()
        
        for user in users:
            self.send_notification(user.telegram_user_id, message)

    def error_handler(self, update: Update, context: CallbackContext):
        """Обработчик ошибок"""
        self.logger.error("Update '%s' caused error '%s'", update, context.error)
        
        if update and update.effective_message:
            error_text = "❌ Произошла ошибка. Попробуйте еще раз или обратитесь к администратору."
            
            if isinstance(context.error, SecurityError):
                error_text = "🔒 Ошибка безопасности. Проверьте права доступа."
            elif isinstance(context.error, ValidationError):
                error_text = f"⚠️ Ошибка валидации: {context.error}"
            elif isinstance(context.error, AccountNotFoundError):
                error_text = "👤 Аккаунт не найден. Проверьте настройки."
            
            update.effective_message.reply_text(error_text)

    def start_bot(self):
        """Запуск бота"""
        updater = Updater(self.config.telegram.token)
        dp = updater.dispatcher

        # Основные обработчики команд
        dp.add_handler(CommandHandler("start", self.start))
        dp.add_handler(CommandHandler("help", self.show_help))
        dp.add_handler(CommandHandler("accounts", self.show_accounts_menu))
        dp.add_handler(CommandHandler("queue", self.show_queue))
        dp.add_handler(CommandHandler("stats", self.show_statistics))
        dp.add_handler(CommandHandler("settings", self.show_settings_menu))

        # ConversationHandler для добавления аккаунта
        add_account_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.start_add_account, pattern="^add_account$")
            ],
            states={
                INPUT_USERNAME: [
                    MessageHandler(Filters.text & ~Filters.command, self.handle_username_input),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_add_account$")
                ],
                INPUT_PASSWORD: [
                    MessageHandler(Filters.text & ~Filters.command, self.handle_password_input),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_add_account$")
                ],
                SELECT_2FA_METHOD: [
                    CallbackQueryHandler(self.handle_2fa_method_selection, pattern="^2fa_method_"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_add_account$")
                ],
                INPUT_2FA_CODE: [
                    MessageHandler(Filters.text & ~Filters.command, self.handle_2fa_code_input),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_add_account$")
                ],
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_operation),
                CallbackQueryHandler(self.cancel_operation, pattern="^cancel_")
            ],
            per_user=True
        )
        dp.add_handler(add_account_conv)

        # ConversationHandler для добавления контента
        add_content_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.handle_account_selection, pattern="^select_account_"),
                CallbackQueryHandler(self.handle_media_type_selection, pattern="^media_type_")
            ],
            states={
                UPLOAD_MEDIA: [
                    MessageHandler(
                        Filters.photo | Filters.video | Filters.document, 
                        self.handle_media_upload
                    ),
                    CommandHandler('done', self.handle_done_upload),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_upload$")
                ],
                INPUT_POST_CAPTION: [
                    MessageHandler(Filters.text & ~Filters.command, self.handle_caption_input),
                    CallbackQueryHandler(self.handle_caption_input, pattern="^skip_caption$"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_upload$")
                ],
                INPUT_POST_TIME: [
                    MessageHandler(Filters.text & ~Filters.command, self.handle_time_input),
                    CallbackQueryHandler(self.handle_publish_time_selection, pattern="^publish_now$"),
                    CallbackQueryHandler(self.handle_publish_time_selection, pattern="^schedule_later$"),
                    CallbackQueryHandler(self.cancel_operation, pattern="^cancel_upload$")
                ],
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_operation),
                CallbackQueryHandler(self.cancel_operation, pattern="^cancel_")
            ],
            per_user=True
        )
        dp.add_handler(add_content_conv)

        # Основной обработчик callback'ов
        callback_handlers = [
            # Главное меню
            CallbackQueryHandler(self.callback_query_handler, pattern="^menu_"),
            CallbackQueryHandler(self.show_main_menu, pattern="^back_to_main$"),
            
            # Аккаунты
            CallbackQueryHandler(self.handle_account_callbacks, pattern="^account_"),
            
            # Настройки
            CallbackQueryHandler(self.handle_settings_callbacks, pattern="^toggle_"),
            CallbackQueryHandler(self.handle_settings_callbacks, pattern="^change_"),
            
            # Общие callback'ы
            CallbackQueryHandler(self.cancel_operation, pattern="^cancel_"),
        ]
        
        for handler in callback_handlers:
            dp.add_handler(handler)

        # Обработчик ошибок
        dp.add_error_handler(self.error_handler)

        # Запуск планировщика
        scheduler_thread = Thread(target=self.scheduler, daemon=True)
        scheduler_thread.start()

        # Запуск отправки еженедельных отчетов
        if self.config.notifications.weekly_reports:
            reports_thread = Thread(target=self.weekly_reports_scheduler, daemon=True)
            reports_thread.start()

        self.logger.info("Enhanced Instagram Bot started successfully")
        
        try:
            if self.config.telegram.use_webhook:
                self.setup_webhook(updater)
            else:
                updater.start_polling(timeout=30, read_latency=10)
            
            updater.idle()
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        except Exception as e:
            self.logger.error(f"Bot error: {e}")
        finally:
            self.scheduler_running = False
            self.logger.info("Bot shutdown complete")

    def setup_webhook(self, updater):
        """Настройка webhook для продакшена"""
        updater.start_webhook(
            listen=self.config.telegram.webhook_listen,
            port=self.config.telegram.webhook_port,
            url_path=self.config.telegram.token,
            webhook_url=f"{self.config.telegram.webhook_url}/{self.config.telegram.token}"
        )
        self.logger.info(f"Webhook setup completed on {self.config.telegram.webhook_url}")

    def show_main_menu(self, update: Update, context: CallbackContext):
        """Показать главное меню"""
        query = update.callback_query
        if query:
            query.answer()
        
        keyboard = [
            [
                InlineKeyboardButton("📱 Аккаунты", callback_data="menu_accounts"),
                InlineKeyboardButton("📝 Добавить пост", callback_data="menu_add_post")
            ],
            [
                InlineKeyboardButton("📸 Добавить Story", callback_data="menu_add_story"),
                InlineKeyboardButton("🎬 Добавить Reel", callback_data="menu_add_reel")
            ],
            [
                InlineKeyboardButton("📋 Очередь", callback_data="menu_queue"),
                InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
                InlineKeyboardButton("❓ Помощь", callback_data="menu_help")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = """
🤖 <b>Enhanced Instagram Bot</b>

Главное меню - выберите действие:
        """
        
        if query:
            query.edit_message_text(
                welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        else:
            update.message.reply_text(
                welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )

    def handle_account_callbacks(self, update: Update, context: CallbackContext):
        """Обработка callback'ов аккаунтов"""
        query = update.callback_query
        query.answer()
        
        if query.data.startswith("account_stats_"):
            username = query.data.replace("account_stats_", "")
            self.show_account_statistics(update, context, username)
        elif query.data.startswith("account_delete_"):
            username = query.data.replace("account_delete_", "")
            self.confirm_account_deletion(update, context, username)

    def show_account_statistics(self, update: Update, context: CallbackContext, username: str):
        """Показать статистику аккаунта"""
        account = self.db_session.query(InstagramAccount).filter_by(username=username).first()
        if not account:
            update.callback_query.edit_message_text("❌ Аккаунт не найден")
            return
        
        # Статистика публикаций за последние 30 дней
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=30)
        
        publications = self.db_session.query(Publication).filter(
            Publication.account_username == username,
            Publication.created_at >= start_date
        ).all()
        
        posts_count = len([p for p in publications if p.content_type == 'post'])
        stories_count = len([p for p in publications if p.content_type == 'story'])
        reels_count = len([p for p in publications if p.content_type == 'reel'])
        
        published_count = len([p for p in publications if p.status == 'published'])
        failed_count = len([p for p in publications if p.status == 'failed'])
        
        success_rate = (published_count / max(len(publications), 1)) * 100
        
        text = f"""
📊 <b>Статистика @{username}</b>

📅 <b>За последние 30 дней:</b>
📝 Посты: {posts_count}
📸 Stories: {stories_count}
🎬 Reels: {reels_count}

✅ Опубликовано: {published_count}
❌ Ошибок: {failed_count}
📈 Успешность: {success_rate:.1f}%

🕒 <b>Последнее использование:</b>
{account.last_used.strftime('%d.%m.%Y %H:%M') if account.last_used else 'Никогда'}
        """
        
        keyboard = [
            [InlineKeyboardButton("◀️ К аккаунтам", callback_data="menu_accounts")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")]
        ]
        
        update.callback_query.edit_message_text(
            text, 
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    def confirm_account_deletion(self, update: Update, context: CallbackContext, username: str):
        """Подтверждение удаления аккаунта"""
        text = f"⚠️ Вы уверены, что хотите удалить аккаунт @{username}?\n\nЭто действие нельзя отменить."
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{username}"),
                InlineKeyboardButton("❌ Отмена", callback_data="menu_accounts")
            ]
        ]
        
        update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    def cancel_operation(self, update: Update, context: CallbackContext):
        """Отмена текущей операции"""
        context.user_data.clear()
        
        if update.callback_query:
            update.callback_query.edit_message_text(
                "❌ Операция отменена",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")
                ]])
            )
        else:
            update.message.reply_text(
                "❌ Операция отменена",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main")
                ]])
            )
        
        return ConversationHandler.END

    def weekly_reports_scheduler(self):
        """Планировщик еженедельных отчетов"""
        while self.scheduler_running:
            try:
                now = datetime.utcnow()
                
                # Проверяем, нужно ли отправить отчеты (каждый понедельник в 9:00)
                if (now.weekday() == 0 and  # Понедельник
                    now.hour == 9 and 
                    now.minute < 10):  # В течение первых 10 минут часа
                    
                    users = self.db_session.query(UserSettings).filter_by(
                        weekly_reports=True
                    ).all()
                    
                    for user in users:
                        try:
                            report = self.send_weekly_report(user.telegram_user_id)
                            self.send_notification(user.telegram_user_id, report, ParseMode.HTML)
                        except Exception as e:
                            self.logger.error(f"Failed to send weekly report to {user.telegram_user_id}: {e}")
                
                # Проверяем раз в час
                sleep(3600)
                
            except Exception as e:
                self.logger.error(f"Weekly reports scheduler error: {e}")
                sleep(3600)

# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    # Конфигурация из переменных окружения
    config = BotConfig(
        telegram_token=os.getenv('TELEGRAM_TOKEN', 'YOUR_TOKEN_HERE'),
        encryption_password=os.getenv('ENCRYPTION_PASSWORD', 'your_secure_password'),
        allowed_users=[int(x) for x in os.getenv('ALLOWED_USERS', '').split(',') if x],
        database_url=os.getenv('DATABASE_URL', 'sqlite:///enhanced_instagram_bot.db')
    )
    
    bot = EnhancedInstagramBot(config)
    bot.start_bot()
