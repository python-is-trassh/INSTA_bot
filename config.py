import os
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

@dataclass
class DatabaseConfig:
    """Конфигурация базы данных"""
    url: str = os.getenv('DATABASE_URL', 'sqlite:///enhanced_instagram_bot.db')
    echo: bool = os.getenv('DB_ECHO', 'false').lower() == 'true'
    pool_size: int = int(os.getenv('DB_POOL_SIZE', '5'))
    max_overflow: int = int(os.getenv('DB_MAX_OVERFLOW', '10'))
    pool_timeout: int = int(os.getenv('DB_POOL_TIMEOUT', '30'))

@dataclass
class SecurityConfig:
    """Конфигурация безопасности"""
    encryption_password: str = os.getenv('ENCRYPTION_PASSWORD', 'default_password_change_me')
    allowed_users: Optional[List[int]] = field(default_factory=lambda: [
        int(x) for x in os.getenv('ALLOWED_USERS', '').split(',') if x.strip()
    ])
    webhook_secret: Optional[str] = os.getenv('WEBHOOK_SECRET')
    rate_limit_enabled: bool = os.getenv('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
    max_requests_per_hour: int = int(os.getenv('MAX_REQUESTS_PER_HOUR', '200'))

@dataclass
class TelegramConfig:
    """Конфигурация Telegram бота"""
    token: str = os.getenv('TELEGRAM_TOKEN', '')
    webhook_url: Optional[str] = os.getenv('WEBHOOK_URL')
    use_webhook: bool = os.getenv('USE_WEBHOOK', 'false').lower() == 'true'
    webhook_port: int = int(os.getenv('WEBHOOK_PORT', '8443'))
    webhook_listen: str = os.getenv('WEBHOOK_LISTEN', '0.0.0.0')

@dataclass
class InstagramConfig:
    """Конфигурация Instagram API"""
    requests_per_hour: int = int(os.getenv('INSTAGRAM_REQUESTS_PER_HOUR', '200'))
    posts_per_day: int = int(os.getenv('INSTAGRAM_POSTS_PER_DAY', '50'))
    stories_per_day: int = int(os.getenv('INSTAGRAM_STORIES_PER_DAY', '100'))
    reels_per_day: int = int(os.getenv('INSTAGRAM_REELS_PER_DAY', '20'))
    session_timeout: int = int(os.getenv('INSTAGRAM_SESSION_TIMEOUT', '3600'))
    max_login_attempts: int = int(os.getenv('INSTAGRAM_MAX_LOGIN_ATTEMPTS', '3'))

@dataclass
class MediaConfig:
    """Конфигурация медиафайлов"""
    max_file_size: int = int(os.getenv('MAX_FILE_SIZE', '52428800'))  # 50MB
    max_video_duration: int = int(os.getenv('MAX_VIDEO_DURATION', '60'))  # seconds
    max_reel_duration: int = int(os.getenv('MAX_REEL_DURATION', '90'))  # seconds
    allowed_photo_formats: List[str] = field(default_factory=lambda: ['jpg', 'jpeg', 'png', 'webp'])
    allowed_video_formats: List[str] = field(default_factory=lambda: ['mp4', 'mov', 'avi', 'mkv'])
    temp_dir: str = os.getenv('TEMP_DIR', 'tmp')
    
    def __post_init__(self):
        """Создаем временную директорию если её нет"""
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir, exist_ok=True)

@dataclass
class SchedulerConfig:
    """Конфигурация планировщика"""
    interval: int = int(os.getenv('SCHEDULER_INTERVAL', '10'))  # seconds
    max_concurrent_jobs: int = int(os.getenv('MAX_CONCURRENT_JOBS', '5'))
    retry_delay: int = int(os.getenv('RETRY_DELAY', '60'))  # seconds
    max_retries: int = int(os.getenv('MAX_RETRIES', '3'))
    timezone: str = os.getenv('DEFAULT_TIMEZONE', 'UTC')

@dataclass
class LoggingConfig:
    """Конфигурация логирования"""
    level: str = os.getenv('LOG_LEVEL', 'INFO')
    format: str = os.getenv('LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    log_dir: str = os.getenv('LOG_DIR', 'logs')
    max_file_size: int = int(os.getenv('LOG_MAX_FILE_SIZE', '10485760'))  # 10MB
    backup_count: int = int(os.getenv('LOG_BACKUP_COUNT', '5'))
    
    def __post_init__(self):
        """Создаем директорию логов если её нет"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)

@dataclass
class NotificationConfig:
    """Конфигурация уведомлений"""
    enabled: bool = os.getenv('NOTIFICATIONS_ENABLED', 'true').lower() == 'true'
    weekly_reports: bool = os.getenv('WEEKLY_REPORTS', 'true').lower() == 'true'
    success_notifications: bool = os.getenv('SUCCESS_NOTIFICATIONS', 'true').lower() == 'true'
    error_notifications: bool = os.getenv('ERROR_NOTIFICATIONS', 'true').lower() == 'true'
    report_day: int = int(os.getenv('REPORT_DAY', '1'))  # Monday = 1
    report_hour: int = int(os.getenv('REPORT_HOUR', '9'))  # 9 AM

@dataclass
class RedisConfig:
    """Конфигурация Redis (опционально)"""
    url: Optional[str] = os.getenv('REDIS_URL')
    enabled: bool = os.getenv('REDIS_ENABLED', 'false').lower() == 'true'
    db: int = int(os.getenv('REDIS_DB', '0'))
    max_connections: int = int(os.getenv('REDIS_MAX_CONNECTIONS', '20'))
    socket_timeout: int = int(os.getenv('REDIS_SOCKET_TIMEOUT', '30'))

@dataclass
class MonitoringConfig:
    """Конфигурация мониторинга"""
    sentry_dsn: Optional[str] = os.getenv('SENTRY_DSN')
    enable_metrics: bool = os.getenv('ENABLE_METRICS', 'false').lower() == 'true'
    metrics_port: int = int(os.getenv('METRICS_PORT', '8000'))
    health_check_port: int = int(os.getenv('HEALTH_CHECK_PORT', '8001'))

@dataclass
class BotConfig:
    """Основная конфигурация бота"""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    instagram: InstagramConfig = field(default_factory=InstagramConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    
    # Дополнительные настройки
    debug: bool = os.getenv('DEBUG', 'false').lower() == 'true'
    environment: str = os.getenv('ENVIRONMENT', 'development')
    version: str = os.getenv('VERSION', '2.0.0')
    
    def __post_init__(self):
        """Валидация конфигурации"""
        self.validate()
    
    def validate(self):
        """Проверка корректности конфигурации"""
        errors = []
        
        # Проверяем обязательные поля
        if not self.telegram.token:
            errors.append("TELEGRAM_TOKEN is required")
        
        if not self.security.encryption_password or self.security.encryption_password == 'default_password_change_me':
            errors.append("ENCRYPTION_PASSWORD must be set to a secure value")
        
        # Проверяем размеры файлов
        if self.media.max_file_size <= 0:
            errors.append("MAX_FILE_SIZE must be positive")
        
        if self.media.max_video_duration <= 0:
            errors.append("MAX_VIDEO_DURATION must be positive")
        
        # Проверяем интервалы планировщика
        if self.scheduler.interval <= 0:
            errors.append("SCHEDULER_INTERVAL must be positive")
        
        if self.scheduler.max_retries < 0:
            errors.append("MAX_RETRIES must be non-negative")
        
        # Проверяем уровень логирования
        valid_log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if self.logging.level.upper() not in valid_log_levels:
            errors.append(f"LOG_LEVEL must be one of: {', '.join(valid_log_levels)}")
        
        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"- {error}" for error in errors))
    
    def get_logging_config(self) -> Dict[str, Any]:
        """Получить конфигурацию для logging.dictConfig"""
        return {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'default': {
                    'format': self.logging.format,
                },
                'detailed': {
                    'format': '%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
                },
            },
            'handlers': {
                'console': {
                    'class': 'logging.StreamHandler',
                    'formatter': 'default',
                    'level': self.logging.level,
                },
                'file': {
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': os.path.join(self.logging.log_dir, 'bot.log'),
                    'maxBytes': self.logging.max_file_size,
                    'backupCount': self.logging.backup_count,
                    'formatter': 'detailed',
                    'level': self.logging.level,
                },
                'error_file': {
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': os.path.join(self.logging.log_dir, 'errors.log'),
                    'maxBytes': self.logging.max_file_size,
                    'backupCount': self.logging.backup_count,
                    'formatter': 'detailed',
                    'level': 'ERROR',
                },
            },
            'loggers': {
                '': {  # root logger
                    'handlers': ['console', 'file'],
                    'level': self.logging.level,
                    'propagate': False,
                },
                'errors': {
                    'handlers': ['console', 'error_file'],
                    'level': 'ERROR',
                    'propagate': False,
                },
            },
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь (для сериализации)"""
        return {
            'telegram': self.telegram.__dict__,
            'database': self.database.__dict__,
            'security': {k: v for k, v in self.security.__dict__.items() if k != 'encryption_password'},
            'instagram': self.instagram.__dict__,
            'media': self.media.__dict__,
            'scheduler': self.scheduler.__dict__,
            'logging': self.logging.__dict__,
            'notifications': self.notifications.__dict__,
            'redis': self.redis.__dict__,
            'monitoring': self.monitoring.__dict__,
            'debug': self.debug,
            'environment': self.environment,
            'version': self.version,
        }

def load_config() -> BotConfig:
    """Загрузка конфигурации"""
    try:
        config = BotConfig()
        logging.info("Configuration loaded successfully")
        return config
    except Exception as e:
        logging.error(f"Failed to load configuration: {e}")
        raise

# Глобальная конфигурация (ленивая загрузка)
_config = None

def get_config() -> BotConfig:
    """Получить глобальную конфигурацию"""
    global _config
    if _config is None:
        _config = load_config()
    return _config

# Настройка логирования при импорте модуля
if __name__ != '__main__':
    try:
        config = get_config()
        logging.config.dictConfig(config.get_logging_config())
    except Exception as e:
        # Fallback к базовому логированию если конфигурация не загрузилась
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        logging.error(f"Failed to configure logging: {e}")
