#!/usr/bin/env python3
"""
Утилиты для работы с базой данных Enhanced Instagram Bot
"""

import os
import sys
import logging
import pickle
from datetime import datetime
from typing import List, Dict, Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

# Добавляем текущую директорию в путь для импорта модулей
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import get_config, BotConfig
from enhanced_insta_bot import Base, InstagramAccount, Publication, BotMetrics, UserSettings

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Менеджер для работы с базой данных"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.engine = create_engine(config.database.url, echo=config.debug)
        self.SessionLocal = sessionmaker(bind=self.engine)
    
    def create_tables(self):
        """Создание всех таблиц"""
        try:
            Base.metadata.create_all(self.engine)
            logger.info("Database tables created successfully")
            return True
        except SQLAlchemyError as e:
            logger.error(f"Failed to create tables: {e}")
            return False
    
    def drop_tables(self):
        """Удаление всех таблиц (осторожно!)"""
        try:
            Base.metadata.drop_all(self.engine)
            logger.info("Database tables dropped successfully")
            return True
        except SQLAlchemyError as e:
            logger.error(f"Failed to drop tables: {e}")
            return False
    
    def backup_database(self, backup_path: str) -> bool:
        """Создание резервной копии базы данных"""
        try:
            if 'sqlite' in self.config.database.url:
                # Для SQLite просто копируем файл
                import shutil
                db_path = self.config.database.url.replace('sqlite:///', '')
                shutil.copy2(db_path, backup_path)
            else:
                # Для других БД используем pg_dump/mysqldump
                self._backup_non_sqlite(backup_path)
            
            logger.info(f"Database backup created: {backup_path}")
            return True
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False
    
    def _backup_non_sqlite(self, backup_path: str):
        """Резервное копирование для PostgreSQL/MySQL"""
        import subprocess
        
        if 'postgresql' in self.config.database.url:
            cmd = f"pg_dump {self.config.database.url} > {backup_path}"
        elif 'mysql' in self.config.database.url:
            cmd = f"mysqldump {self.config.database.url} > {backup_path}"
        else:
            raise ValueError("Unsupported database type for backup")
        
        subprocess.run(cmd, shell=True, check=True)
    
    def migrate_from_pickle(self, pickle_file: str = 'instagram_accounts.dat') -> bool:
        """Миграция данных из старого pickle файла"""
        if not os.path.exists(pickle_file):
            logger.warning(f"Pickle file {pickle_file} not found")
            return False
        
        try:
            with open(pickle_file, 'rb') as f:
                old_accounts = pickle.load(f)
            
            session = self.SessionLocal()
            migrated_count = 0
            
            for username, data in old_accounts.items():
                # Проверяем, нет ли уже такого аккаунта
                existing = session.query(InstagramAccount).filter_by(username=username).first()
                if existing:
                    logger.info(f"Account {username} already exists, skipping")
                    continue
                
                # Создаем новую запись
                account = InstagramAccount(
                    username=username,
                    encrypted_password=data.get('encrypted_password', ''),
                    user_id=str(data.get('user_id', '')),
                    verification_method=data.get('verification_method'),
                    last_used=data.get('last_used', datetime.utcnow()),
                    created_at=data.get('created_at', datetime.utcnow())
                )
                
                session.add(account)
                migrated_count += 1
            
            session.commit()
            session.close()
            
            logger.info(f"Migrated {migrated_count} accounts from pickle file")
            return True
            
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            return False
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Получение статистики базы данных"""
        session = self.SessionLocal()
        
        try:
            stats = {
                'accounts': {
                    'total': session.query(InstagramAccount).count(),
                    'active': session.query(InstagramAccount).filter_by(is_active=True).count(),
                },
                'publications': {
                    'total': session.query(Publication).count(),
                    'queued': session.query(Publication).filter_by(status='queued').count(),
                    'published': session.query(Publication).filter_by(status='published').count(),
                    'failed': session.query(Publication).filter_by(status='failed').count(),
                },
                'content_types': {},
                'users': session.query(UserSettings).count()
            }
            
            # Статистика по типам контента
            for content_type in ['post', 'story', 'reel']:
                stats['content_types'][content_type] = session.query(Publication).filter_by(
                    content_type=content_type
                ).count()
            
            return stats
            
        except Exception as e:
            logger.error(f"Failed to get database stats: {e}")
            return {}
        finally:
            session.close()
    
    def cleanup_old_data(self, days: int = 30) -> int:
        """Очистка старых данных"""
        session = self.SessionLocal()
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        try:
            # Удаляем старые опубликованные/неудачные публикации
            deleted_count = session.query(Publication).filter(
                Publication.status.in_(['published', 'failed']),
                Publication.created_at < cutoff_date
            ).delete()
            
            session.commit()
            logger.info(f"Cleaned up {deleted_count} old records")
            return deleted_count
            
        except Exception as e:
            session.rollback()
            logger.error(f"Cleanup failed: {e}")
            return 0
        finally:
            session.close()
    
    def verify_database_integrity(self) -> bool:
        """Проверка целостности базы данных"""
        session = self.SessionLocal()
        
        try:
            # Проверяем наличие всех таблиц
            tables = Base.metadata.tables.keys()
            for table in tables:
                result = session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                count = result.scalar()
                logger.info(f"Table {table}: {count} records")
            
            # Проверяем ссылочную целостность
            orphaned_publications = session.query(Publication).filter(
                ~Publication.account_username.in_(
                    session.query(InstagramAccount.username)
                )
            ).count()
            
            if orphaned_publications > 0:
                logger.warning(f"Found {orphaned_publications} orphaned publications")
                return False
            
            logger.info("Database integrity check passed")
            return True
            
        except Exception as e:
            logger.error(f"Integrity check failed: {e}")
            return False
        finally:
            session.close()

def main():
    """Главная функция для запуска утилит через командную строку"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Database utilities for Enhanced Instagram Bot')
    parser.add_argument('command', choices=[
        'create', 'drop', 'backup', 'migrate', 'stats', 'cleanup', 'verify'
    ], help='Command to execute')
    parser.add_argument('--file', help='File path for backup/migrate operations')
    parser.add_argument('--days', type=int, default=30, help='Days for cleanup operation')
    parser.add_argument('--force', action='store_true', help='Force operation without confirmation')
    
    args = parser.parse_args()
    
    # Настройка логирования
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    try:
        config = get_config()
        db_manager = DatabaseManager(config)
        
        if args.command == 'create':
            if db_manager.create_tables():
                print("✅ Database tables created successfully")
            else:
                print("❌ Failed to create database tables")
                sys.exit(1)
        
        elif args.command == 'drop':
            if not args.force:
                confirm = input("⚠️  This will delete ALL data. Are you sure? (yes/no): ")
                if confirm.lower() != 'yes':
                    print("Operation cancelled")
                    sys.exit(0)
            
            if db_manager.drop_tables():
                print("✅ Database tables dropped successfully")
            else:
                print("❌ Failed to drop database tables")
                sys.exit(1)
        
        elif args.command == 'backup':
            backup_file = args.file or f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
            if db_manager.backup_database(backup_file):
                print(f"✅ Backup created: {backup_file}")
            else:
                print("❌ Backup failed")
                sys.exit(1)
        
        elif args.command == 'migrate':
            pickle_file = args.file or 'instagram_accounts.dat'
            if db_manager.migrate_from_pickle(pickle_file):
                print("✅ Migration completed successfully")
            else:
                print("❌ Migration failed")
                sys.exit(1)
        
        elif args.command == 'stats':
            stats = db_manager.get_database_stats()
            print("\n📊 Database Statistics:")
            print(f"👤 Accounts: {stats['accounts']['total']} total, {stats['accounts']['active']} active")
            print(f"📝 Publications: {stats['publications']['total']} total")
            print(f"   - Queued: {stats['publications']['queued']}")
            print(f"   - Published: {stats['publications']['published']}")
            print(f"   - Failed: {stats['publications']['failed']}")
            print(f"📱 Content types:")
            for content_type, count in stats['content_types'].items():
                print(f"   - {content_type.title()}: {count}")
            print(f"👥 Users: {stats['users']}")
        
        elif args.command == 'cleanup':
            if not args.force:
                confirm = input(f"⚠️  Delete records older than {args.days} days? (yes/no): ")
                if confirm.lower() != 'yes':
                    print("Operation cancelled")
                    sys.exit(0)
            
            deleted = db_manager.cleanup_old_data(args.days)
            print(f"✅ Cleaned up {deleted} old records")
        
        elif args.command == 'verify':
            if db_manager.verify_database_integrity():
                print("✅ Database integrity check passed")
            else:
                print("❌ Database integrity issues found")
                sys.exit(1)
    
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
