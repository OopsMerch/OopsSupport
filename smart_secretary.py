import asyncio
import time
import json
import os
import logging
import shutil
from typing import Dict, Optional, Union
from datetime import datetime, timezone, timedelta

# --- ДОПОЛНИТЕЛЬНЫЕ ИМПОРТЫ ---
from dotenv import load_dotenv

# --- TELETHON IMPORTS ---
from telethon import TelegramClient, events
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.messages import SetTypingRequest
# Импортируем типы явно для корректной обработки UserStatus
from telethon.tl.types import SendMessageTypingAction, User, UserStatusOffline, UserStatusRecently, UserStatusOnline 
from telethon.errors import FloodWaitError
from telethon.tl import types
from telethon.tl.functions.users import GetUsersRequest


# =========================================================
#             ⚙️ КОНФИГУРАЦИЯ СИСТЕМЫ ⚙️
# =========================================================

load_dotenv()

# --- 1. ЗАГРУЗКА НАСТРОЕК ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 1.1. Доступы (из .env)
    API_ID_ENV = os.getenv('API_ID')
    API_HASH = os.getenv('API_HASH')
    SESSION_NAME = os.getenv('SESSION_NAME')

    if not API_ID_ENV or not API_HASH:
        raise ValueError("API_ID or API_HASH missing in .env file")
        
    API_ID = int(API_ID_ENV)

    # 1.2. Настройки (из config.json)
    SETTINGS = config['settings']
    CONVERSATION_THRESHOLD_SEC = SETTINGS['conversation_threshold_sec']
    TYPING_DELAY_SEC = SETTINGS['typing_delay_sec']
    RESPONSES_FILE = SETTINGS['responses_file']
    
    # НАСТРОЙКИ ДЛЯ СТАТУСА
    ADMIN_ID_TO_CHECK = SETTINGS['admin_status_check_id']
    ONLINE_THRESHOLD_SEC = SETTINGS['online_threshold_sec']
    
    # Кэширование статуса
    STATUS_CACHE_TTL_SEC = 30 

    # 1.3. Тексты (из config.json)
    TEXTS = config['texts']
    BRAND_LINK = TEXTS['brand_link']
    HEADER_FORMATTED = TEXTS['header'].format(brand_link=BRAND_LINK)
    ACTION_TEXT_BASE = TEXTS['action_text_base']
    
    # Динамические части ответа
    RESPONSE_ONLINE_DYNAMIC = TEXTS['dynamic_online']
    RESPONSE_OFFLINE_DYNAMIC = TEXTS['dynamic_offline']
    
except FileNotFoundError:
    print("FATAL ERROR: 'config.json' or '.env' not found. Please check your project structure.")
    exit(1)
except json.JSONDecodeError as e:
    print(f"FATAL ERROR: 'config.json' contains invalid JSON.\nError at line {e.lineno}, column {e.colno}: {e.msg}")
    exit(1)
except Exception as e:
    print(f"FATAL ERROR: Failed to load configuration: {e}")
    exit(1)


# =========================================================
#                 💎 ЯДРО СИСТЕМЫ 💎
# =========================================================

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("Secretary")


class AdminStatusCache:
    """Класс для управления кэшем статуса администратора."""
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.cache = {
            'is_online': False,
            'timestamp': 0.0
        }
    
    def get(self) -> Optional[bool]:
        """Возвращает кэшированный статус, если он не устарел."""
        now = time.time()
        if (now - self.cache['timestamp']) < self.ttl:
            logger.debug("Admin status: using cache.")
            return self.cache['is_online']
        logger.debug("Admin status cache expired. Performing live check.")
        return None
        
    def set(self, is_online: bool):
        """Обновляет кэш."""
        self.cache['is_online'] = is_online
        self.cache['timestamp'] = time.time()
        

class ResponseManager:
    """
    Управляет логами ответов, используя асинхронный ввод-вывод
    для предотвращения блокировки цикла событий.
    """
    
    @staticmethod
    def _convert_to_timestamp(value: Union[float, str]) -> float:
        """
        Конвертирует значение (timestamp или ISO-строку) в timestamp.
        """
        if isinstance(value, (int, float)):
            return float(value)
        try:
            # Парсинг ISO-формата с учетом возможного TZ-смещения
            dt = datetime.fromisoformat(value)
            # Переводим в UTC и затем в timestamp
            if dt.tzinfo is None:
                # Если информация о TZ отсутствует, предполагаем UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).timestamp()
        except ValueError:
            logger.error(f"Failed to parse time string: {value}. Assuming 0.")
            return 0.0

    @staticmethod
    async def load_log() -> Dict[str, Union[float, str]]:
        """
        Загружает лог ответов, используя asyncio.to_thread.
        """
        def sync_load():
            if os.path.exists(RESPONSES_FILE):
                try:
                    with open(RESPONSES_FILE, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except json.JSONDecodeError:
                    # Бэкап поврежденного файла
                    timestamp = int(time.time())
                    backup_name = f"{RESPONSES_FILE}.corrupted_{timestamp}.bak"
                    try:
                        shutil.copy(RESPONSES_FILE, backup_name)
                        logger.critical(f"⚠️ Log file '{RESPONSES_FILE}' is corrupted (invalid JSON).")
                        logger.critical(f"💾 BACKUP CREATED: {backup_name}")
                        logger.warning("Starting with a fresh log to keep the bot running.")
                    except Exception as backup_error:
                        logger.error(f"Failed to create backup of corrupted log: {backup_error}")
                    return {}
                except IOError as e:
                    # ФАТАЛЬНЫЙ СБОЙ ВВОДА/ВЫВОДА -> ОСТАНОВКА
                    logger.critical(f"❌ FATAL I/O ERROR reading log file '{RESPONSES_FILE}': {e}")
                    raise RuntimeError("Cannot safely proceed without log file access.") from e
            return {}
            
        return await asyncio.to_thread(sync_load)

    @staticmethod
    async def save_log(user_id: str):
        """
        Сохраняет лог ответов, используя asyncio.to_thread.
        """
        log = await ResponseManager.load_log()
        # Сохраняем в ISO-формате (UTC)
        now_iso = datetime.now(timezone.utc).isoformat()
        log[user_id] = now_iso
        
        def sync_save():
            try:
                with open(RESPONSES_FILE, 'w', encoding='utf-8') as f:
                    json.dump(log, f, indent=4, ensure_ascii=False)
            except IOError as e:
                logger.error(f"Failed to save log file: {e}")

        await asyncio.to_thread(sync_save)


    @staticmethod
    async def should_reply(user_id: str) -> bool:
        """
        Проверяет, прошло ли достаточно времени с момента последнего ответа.
        """
        log = await ResponseManager.load_log()
        last_response_value = log.get(user_id)
        
        if last_response_value is None:
            return True 
            
        last_response_timestamp = ResponseManager._convert_to_timestamp(last_response_value)

        return (time.time() - last_response_timestamp) >= CONVERSATION_THRESHOLD_SEC


async def check_admin_online_status(client, status_cache: AdminStatusCache) -> bool:
    """
    Проверяет, был ли основной админ-аккаунт в сети в течение ONLINE_THRESHOLD_SEC,
    используя инкапсулированное кэширование.
    """
    
    # 1. Проверка кэша
    cached_status = status_cache.get()
    if cached_status is not None:
        return cached_status
    
    # 2. Если кэш устарел, делаем сетевой запрос
    is_online = False
    
    try:
        user_list = await client(GetUsersRequest([ADMIN_ID_TO_CHECK]))
        admin_user = user_list[0]
        status = admin_user.status
        
        # NOTE: Telethon.tl.types.UserStatusOnline/UserStatusRecently/UserStatusOffline
        
        if isinstance(status, (UserStatusOnline, UserStatusRecently)):
            logger.info("Admin status: Online/Recently (Live Check).")
            is_online = True
        
        elif isinstance(status, UserStatusOffline):
            # Проверяем, когда последний раз был онлайн (timestamp в UTC)
            if status.was_online:
                was_online_utc = status.was_online.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                last_seen_delta = now_utc - was_online_utc
                
                if last_seen_delta.total_seconds() <= ONLINE_THRESHOLD_SEC:
                    logger.info(f"Admin status: Offline, seen {int(last_seen_delta.total_seconds())}s ago (within limit). (Live Check)")
                    is_online = True
                else:
                    logger.info(f"Admin status: Offline, seen {int(last_seen_delta.total_seconds())}s ago (over limit). (Live Check)")
                    is_online = False
            else:
                 # Если was_online отсутствует (очень старый статус), считаем офлайн
                 logger.info("Admin status: Offline, last seen timestamp missing. (Live Check)")
                 is_online = False
        
        else:
            logger.info(f"Admin status: Unknown/Other ({type(status).__name__}). Assuming offline. (Live Check)")
            is_online = False
        
    except Exception as e:
        # Улучшено: логируем ошибку, но возвращаем предыдущий кэшированный статус
        logger.error(f"Error checking admin status: {e}. Falling back to default/cached status (False).")
        is_online = False # Безопасный отказ
        
    # 3. Обновление кэша
    status_cache.set(is_online)
    
    return is_online


async def process_message(event, status_cache: AdminStatusCache):
    
    # 1. ФИЛЬТРАЦИЯ (Игнорировать исходящие, ботов и не-личные чаты)
    if event.out:
        return
    
    chat = await event.get_chat()
    
    if not isinstance(chat, types.User) or chat.bot:
        return 

    sender_id = str(event.sender_id)
    client = event.client

    # 2. ПРОВЕРКА АНТИ-СПАМА (Теперь асинхронная)
    if not await ResponseManager.should_reply(sender_id):
        return

    logger.info(f"📨 Входящее/Редактирование от {sender_id}. Обработка...")

    try:
        chat_input = await event.get_input_chat()
        
        # 3. ПРОВЕРКА СТАТУСА (Использует кэш, передаем status_cache)
        is_admin_online = await check_admin_online_status(client, status_cache)
        
        if is_admin_online:
            dynamic_message_part = RESPONSE_ONLINE_DYNAMIC
            logger.info("-> Статус: ОНЛАЙН.")
        else:
            dynamic_message_part = RESPONSE_OFFLINE_DYNAMIC
            logger.info("-> Статус: ОФФЛАЙН.")
        
        final_response_text = (
            HEADER_FORMATTED + 
            dynamic_message_part + 
            ACTION_TEXT_BASE
        )

        # 4. ИМИТАЦИЯ НАБОРА
        await client(SetTypingRequest(
            peer=chat_input,
            action=SendMessageTypingAction()
        ))
        await asyncio.sleep(TYPING_DELAY_SEC)

        # 5. ОТПРАВКА ОТВЕТА
        await event.reply(final_response_text, link_preview=False)

        # 6. ЛОГИРОВАНИЕ (Теперь асинхронное)
        await ResponseManager.save_log(sender_id)
        logger.info(f"✅ [ОТВЕТ] Клиенту {sender_id} отправлен.")

    except FloodWaitError as e:
        logger.warning(f"⚠️ FloodWait: {e.seconds} сек. Ожидание...")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"❌ Ошибка при обработке сообщения: {e}")


async def main():
    print(f"\n🛡️ SMART SECRETARY v7.2 (FINAL) 🛡️")
    print(f"-------------------------------------------")
    
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    # Создаем инстанс кэша
    status_cache = AdminStatusCache(STATUS_CACHE_TTL_SEC)
    
    await client.start()
    # Убедитесь, что бот выходит в оффлайн, чтобы не отображаться онлайн 24/7
    await client(UpdateStatusRequest(offline=True))
    
    me = await client.get_me()
    print(f"👤 Секретарь: @{me.username}")
    print(f"🔍 Админ ID: {ADMIN_ID_TO_CHECK}")
    print(f"⏱️ Порог 'Онлайн': {ONLINE_THRESHOLD_SEC/60:.0f} мин")
    print(f"⏱️ Кэш статуса: {STATUS_CACHE_TTL_SEC} сек")
    print(f"💾 Лог файл: {RESPONSES_FILE}")
    print(f"-------------------------------------------\n")
    
    # Обернем process_message в lambda, чтобы передать status_cache
    client.add_event_handler(lambda e: process_message(e, status_cache), events.NewMessage(incoming=True))
    client.add_event_handler(lambda e: process_message(e, status_cache), events.MessageEdited(incoming=True))
    
    logger.info("Система запущена и ожидает событий...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🏁 Система остановлена пользователем.")
    except RuntimeError as e:
        logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА СИСТЕМЫ: {e}")
        print("\n🏁 Система аварийно остановлена из-за невозможности доступа к лог-файлу.")
    except Exception as e:
        logger.critical(f"CRITICAL SYSTEM FAILURE: {e}")