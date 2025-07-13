import os
import logging
from datetime import datetime, timedelta
from threading import Thread
from time import sleep
import pytz

from telegram import Update, InputMediaPhoto, Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from instagrapi import Client
from instagrapi.exceptions import LoginRequired

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
INSTAGRAM_USERNAME = os.getenv('INSTAGRAM_USERNAME')
INSTAGRAM_PASSWORD = os.getenv('INSTAGRAM_PASSWORD')

# Глобальные переменные
instagram_client = None
post_queue = []
stories_queue = []
scheduler_running = False

# Инициализация Instagram клиента
def init_instagram():
    global instagram_client
    instagram_client = Client()
    try:
        instagram_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        logger.info("Успешный вход в Instagram")
    except Exception as e:
        logger.error(f"Ошибка входа в Instagram: {e}")
        raise

# Функции для работы с очередями
def add_to_queue(queue_type, content, caption=None, publish_time=None):
    """Добавление в очередь постов или сторис"""
    item = {
        'content': content,
        'caption': caption,
        'publish_time': publish_time or datetime.now(pytz.utc),
        'status': 'queued'
    }
    
    if queue_type == 'post':
        post_queue.append(item)
    elif queue_type == 'story':
        stories_queue.append(item)
    
    return item

def get_next_item(queue_type):
    """Получение следующего элемента для публикации"""
    queue = post_queue if queue_type == 'post' else stories_queue
    now = datetime.now(pytz.utc)
    
    for item in queue:
        if item['status'] == 'queued' and item['publish_time'] <= now:
            return item
    return None

# Функции публикации
def publish_post(post):
    """Публикация поста в Instagram"""
    try:
        if isinstance(post['content'], list):  # Несколько фото
            media = []
            for photo in post['content']:
                media.append(instagram_client.photo_upload(photo, post['caption']))
        else:  # Одно фото
            media = instagram_client.photo_upload(post['content'], post['caption'])
        
        post['status'] = 'published'
        post['published_at'] = datetime.now(pytz.utc)
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации поста: {e}")
        post['status'] = 'failed'
        return False

def publish_story(story):
    """Публикация истории в Instagram"""
    try:
        if isinstance(story['content'], list):  # Несколько фото для сторис
            for photo in story['content']:
                instagram_client.photo_upload_to_story(photo)
        else:  # Одно фото для сторис
            instagram_client.photo_upload_to_story(story['content'])
        
        story['status'] = 'published'
        story['published_at'] = datetime.now(pytz.utc)
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации сторис: {e}")
        story['status'] = 'failed'
        return False

# Планировщик
def scheduler():
    """Проверка очереди и публикация по расписанию"""
    global scheduler_running
    scheduler_running = True
    
    while scheduler_running:
        try:
            # Проверяем очередь постов
            next_post = get_next_item('post')
            if next_post:
                if publish_post(next_post):
                    logger.info(f"Опубликован пост: {next_post['caption'][:20]}...")
            
            # Проверяем очередь сторис
            next_story = get_next_item('story')
            if next_story:
                if publish_story(next_story):
                    logger.info("Опубликована сторис")
            
            sleep(10)  # Проверяем каждые 10 секунд
        except Exception as e:
            logger.error(f"Ошибка в планировщике: {e}")
            sleep(30)

# Обработчики команд Telegram
def start(update: Update, context: CallbackContext):
    """Обработчик команды /start"""
    user = update.effective_user
    update.message.reply_text(
        f"Привет, {user.first_name}!\n\n"
        "Я бот для отложенной публикации в Instagram.\n\n"
        "Доступные команды:\n"
        "/add_post - добавить пост в очередь\n"
        "/add_story - добавить сторис в очередь\n"
        "/queue - просмотреть очередь публикаций\n"
        "/cancel - отменить последнюю добавленную публикацию"
    )

def add_post(update: Update, context: CallbackContext):
    """Обработчик команды /add_post"""
    update.message.reply_text(
        "Отправьте мне фото или несколько фото для поста (как альбом), "
        "а затем подпись к посту. После этого я запрошу время публикации."
    )
    context.user_data['awaiting_post'] = True
    context.user_data['post_media'] = []

def add_story(update: Update, context: CallbackContext):
    """Обработчик команды /add_story"""
    update.message.reply_text(
        "Отправьте мне фото для сторис. Вы можете отправить несколько фото. "
        "После этого я запрошу время публикации."
    )
    context.user_data['awaiting_story'] = True
    context.user_data['story_media'] = []

def handle_media(update: Update, context: CallbackContext):
    """Обработчик медиафайлов (фото)"""
    if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
        if update.message.photo:
            photo = update.message.photo[-1].get_file()
            context.user_data['post_media'].append(photo.download(custom_path=f"tmp/post_{update.update_id}.jpg"))
            update.message.reply_text("Фото добавлено. Отправьте еще фото или /done чтобы продолжить.")
    
    elif 'awaiting_story' in context.user_data and context.user_data['awaiting_story']:
        if update.message.photo:
            photo = update.message.photo[-1].get_file()
            context.user_data['story_media'].append(photo.download(custom_path=f"tmp/story_{update.update_id}.jpg"))
            update.message.reply_text("Фото для сторис добавлено. Отправьте еще фото или /done чтобы продолжить.")

def handle_text(update: Update, context: CallbackContext):
    """Обработчик текстовых сообщений"""
    if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
        if 'post_media' in context.user_data and context.user_data['post_media']:
            context.user_data['post_caption'] = update.message.text
            update.message.reply_text(
                "Введите время публикации в формате:\n"
                "DD.MM.YYYY HH:MM (например: 25.12.2023 15:30)\n"
                "Или 'now' для немедленной публикации."
            )
            context.user_data['awaiting_post_time'] = True
        else:
            update.message.reply_text("Сначала отправьте фото для поста.")

def handle_time(update: Update, context: CallbackContext):
    """Обработчик времени публикации"""
    text = update.message.text.lower()
    
    try:
        if text == 'now':
            publish_time = datetime.now(pytz.utc)
        else:
            publish_time = datetime.strptime(text, "%d.%m.%Y %H:%M")
            publish_time = pytz.timezone('Europe/Moscow').localize(publish_time).astimezone(pytz.utc)
        
        if 'awaiting_post_time' in context.user_data and context.user_data['awaiting_post_time']:
            media = context.user_data['post_media']
            caption = context.user_data.get('post_caption', '')
            
            if len(media) == 1:
                content = media[0]
            else:
                content = media
            
            post = add_to_queue('post', content, caption, publish_time)
            update.message.reply_text(
                f"Пост добавлен в очередь на {publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}\n"
                f"Всего в очереди постов: {len(post_queue)}"
            )
            
            # Очищаем контекст
            for key in ['awaiting_post', 'post_media', 'post_caption', 'awaiting_post_time']:
                context.user_data.pop(key, None)
        
        elif 'awaiting_story_time' in context.user_data and context.user_data['awaiting_story_time']:
            media = context.user_data['story_media']
            
            if len(media) == 1:
                content = media[0]
            else:
                content = media
            
            story = add_to_queue('story', content, None, publish_time)
            update.message.reply_text(
                f"Сторис добавлен в очередь на {publish_time.astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}\n"
                f"Всего в очереди сторис: {len(stories_queue)}"
            )
            
            # Очищаем контекст
            for key in ['awaiting_story', 'story_media', 'awaiting_story_time']:
                context.user_data.pop(key, None)
    
    except ValueError:
        update.message.reply_text("Неверный формат времени. Попробуйте снова.")

def show_queue(update: Update, context: CallbackContext):
    """Обработчик команды /queue - показывает очередь публикаций"""
    if not post_queue and not stories_queue:
        update.message.reply_text("Очередь публикаций пуста.")
        return
    
    message = "📌 Очередь публикаций:\n\n"
    
    if post_queue:
        message += "📷 Посты:\n"
        for i, post in enumerate(post_queue, 1):
            status = {
                'queued': '⏳ Ожидает',
                'published': '✅ Опубликован',
                'failed': '❌ Ошибка'
            }.get(post['status'], post['status'])
            
            time = post['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
            message += f"{i}. {status} ({time}) - {post['caption'][:20]}...\n"
    
    if stories_queue:
        message += "\n📱 Сторис:\n"
        for i, story in enumerate(stories_queue, 1):
            status = {
                'queued': '⏳ Ожидает',
                'published': '✅ Опубликована',
                'failed': '❌ Ошибка'
            }.get(story['status'], story['status'])
            
            time = story['publish_time'].astimezone(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
            message += f"{i}. {status} ({time})\n"
    
    update.message.reply_text(message)

def cancel_last(update: Update, context: CallbackContext):
    """Обработчик команды /cancel - отменяет последнюю добавленную публикацию"""
    if post_queue:
        last_item = post_queue[-1]
        if last_item['status'] == 'queued':
            post_queue.pop()
            update.message.reply_text("Последний пост удален из очереди.")
            return
    
    if stories_queue:
        last_item = stories_queue[-1]
        if last_item['status'] == 'queued':
            stories_queue.pop()
            update.message.reply_text("Последняя сторис удалена из очереди.")
            return
    
    update.message.reply_text("Нет публикаций в очереди для отмены.")

def done(update: Update, context: CallbackContext):
    """Обработчик команды /done - завершает ввод медиа"""
    if 'awaiting_post' in context.user_data and context.user_data['awaiting_post']:
        if 'post_media' in context.user_data and context.user_data['post_media']:
            update.message.reply_text("Теперь введите подпись к посту.")
        else:
            update.message.reply_text("Вы не отправили ни одного фото. Отправьте фото или /cancel чтобы отменить.")
    
    elif 'awaiting_story' in context.user_data and context.user_data['awaiting_story']:
        if 'story_media' in context.user_data and context.user_data['story_media']:
            update.message.reply_text(
                "Введите время публикации в формате:\n"
                "DD.MM.YYYY HH:MM (например: 25.12.2023 15:30)\n"
                "Или 'now' для немедленной публикации."
            )
            context.user_data['awaiting_story_time'] = True
        else:
            update.message.reply_text("Вы не отправили ни одного фото. Отправьте фото или /cancel чтобы отменить.")

def error_handler(update: Update, context: CallbackContext):
    """Обработчик ошибок"""
    logger.error(msg="Ошибка в обработчике Telegram:", exc_info=context.error)
    if update and update.message:
        update.message.reply_text('Произошла ошибка. Пожалуйста, попробуйте еще раз.')

def main():
    """Основная функция"""
    # Создаем временную папку, если ее нет
    if not os.path.exists('tmp'):
        os.makedirs('tmp')
    
    # Инициализируем Instagram клиент
    init_instagram()
    
    # Запускаем планировщик в отдельном потоке
    scheduler_thread = Thread(target=scheduler, daemon=True)
    scheduler_thread.start()
    
    # Настраиваем Telegram бота
    updater = Updater(TELEGRAM_TOKEN)
    dp = updater.dispatcher
    
    # Регистрируем обработчики команд
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add_post", add_post))
    dp.add_handler(CommandHandler("add_story", add_story))
    dp.add_handler(CommandHandler("queue", show_queue))
    dp.add_handler(CommandHandler("cancel", cancel_last))
    dp.add_handler(CommandHandler("done", done))
    
    # Регистрируем обработчики сообщений
    dp.add_handler(MessageHandler(Filters.photo, handle_media))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    
    # Обработчик времени публикации
    dp.add_handler(MessageHandler(
        Filters.regex(r'^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}|now)$'), 
        handle_time
    ))
    
    # Обработчик ошибок
    dp.add_error_handler(error_handler)
    
    # Запускаем бота
    updater.start_polling()
    updater.idle()
    
    # Останавливаем планировщик при завершении работы
    global scheduler_running
    scheduler_running = False
    scheduler_thread.join()

if __name__ == '__main__':
    main(
