import asyncio
import html
import logging
import os
import pickle
import platform
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LabeledPrice, BotCommand, ForceReply
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, PicklePersistence, PreCheckoutQueryHandler,
    BaseUpdateProcessor, ApplicationHandlerStop
)

try:
    import stripe
except Exception:
    stripe = None

try:
    from aiohttp import web
    import aiohttp
except Exception:
    web = None
    aiohttp = None

# ============================================================
# НАСТРОЙКИ
#
# Railway/GitHub версия: токены не хранятся в коде.
# Все секреты задаются через Railway Variables.

#



def clean_env(name, default=""):
    return os.getenv(name, default).strip()


def int_env(name, default, minimum=None, maximum=None):
    """Читает целую Railway Variable и сообщает понятную ошибку конфигурации."""
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Railway Variable {name} должна быть целым числом, получено: {raw_value!r}") from error
    if minimum is not None and value < minimum:
        raise RuntimeError(f"Railway Variable {name} должна быть не меньше {minimum}, получено: {value}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"Railway Variable {name} должна быть не больше {maximum}, получено: {value}")
    return value

PARTNER_BOT_TOKEN = clean_env("PARTNER_BOT_TOKEN")
GEMINI_API_KEY = clean_env("GEMINI_API_KEY")

ADMIN_TELEGRAM_ID = int_env("ADMIN_TELEGRAM_ID", 894394087, minimum=1)
ADMIN_CHAT_ID = int_env("ADMIN_CHAT_ID", -1004484453420)
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@binio_praha")
PAYMENT_PROVIDER_TOKEN = clean_env("PAYMENT_PROVIDER_TOKEN")
STRIPE_SECRET_KEY = clean_env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = clean_env("STRIPE_WEBHOOK_SECRET")
PUBLIC_BASE_URL = clean_env("PUBLIC_BASE_URL").rstrip("/")
BOT_USERNAME = clean_env("BOT_USERNAME", "binio_partner_bot").lstrip("@")
PUBLIC_LISTING_PRICE_CZK = int_env("PUBLIC_LISTING_PRICE_CZK", 20, minimum=1, maximum=1_000_000)
PUBLIC_PAYMENT_CURRENCY = "CZK"
PUBLIC_PAYMENT_AMOUNT = PUBLIC_LISTING_PRICE_CZK * 100
PUBLIC_MONTHLY_LIMIT = int_env("PUBLIC_MONTHLY_LIMIT", 3, minimum=0, maximum=1000)
PUBLIC_INVOICE_TTL_HOURS = int_env("PUBLIC_INVOICE_TTL_HOURS", 2, minimum=1, maximum=24)
PUBLIC_PAYMENT_TEST_MODE = os.getenv("PUBLIC_PAYMENT_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "partner_bot_data.pickle")
BOT_DATA_PATH = os.path.join(BOT_DATA_DIR, BOT_DATA_FILE)
WEB_PORT = int_env("PORT", 8080, minimum=1, maximum=65535)
BOT_DRAFT_TTL_DAYS = int_env("BOT_DRAFT_TTL_DAYS", 14, minimum=1, maximum=3650)
BOT_SUBMITTED_TTL_DAYS = int_env("BOT_SUBMITTED_TTL_DAYS", 90, minimum=1, maximum=3650)
BOT_TRANSIENT_TTL_DAYS = int_env("BOT_TRANSIENT_TTL_DAYS", 7, minimum=1, maximum=3650)
PUBLIC_GEMINI_DAILY_LIMIT = int_env("PUBLIC_GEMINI_DAILY_LIMIT", 15, minimum=0, maximum=10000)
PARTNER_GEMINI_DAILY_LIMIT = int_env("PARTNER_GEMINI_DAILY_LIMIT", 100, minimum=0, maximum=100000)
REVOKED_PARTNER_IDS = {
    value.strip()
    for value in clean_env("REVOKED_PARTNER_IDS").split(",")
    if value.strip().isdigit()
}

EMPLOYEES = {
    "ivan": "https://t.me/malakhov_prague",
    "ivan2": "https://t.me/malakhov_prague",
    "irina": "https://t.me/binio_irina",
    "irina2": "https://t.me/binio_irina",
    "vera": "https://t.me/VeraGryshyna",
    "vera2": "https://t.me/VeraGryshyna",
    "ekaterina2": "https://t.me/ekaterina_rossel",
    "binio_dp2": "https://t.me/Binio_DP",
    "darya2": "https://t.me/Binio_Darya",
}
EMPLOYEE_NAMES = {
    "ivan": "Иван",
    "ivan2": "Иван",
    "irina": "Ирина",
    "irina2": "Ирина",
    "vera": "Вера",
    "vera2": "Вера",
    "ekaterina2": "Екатерина",
    "binio_dp2": "Диана",
    "darya2": "Дарья",
}
EMPLOYEE_CHOICE_KEYS = ("ivan2", "irina2", "vera2", "ekaterina2", "binio_dp2", "darya2")
EMPLOYEE_CODE_ALIASES = {
    "ivan": "ivan2",
    "irina": "irina2",
    "vera": "vera2",
}
DEFAULT_CONTACT = "https://t.me/malakhov_prague"
PUBLISHED_LISTINGS_PAGE_SIZE = 8
PUBLISHED_LISTING_FILTERS = {
    # Внутренний ключ остаётся all для совместимости со старыми callback-кнопками.
    # На экране это «Основные»: архивные записи в этот раздел не входят.
    "all": "Основные",
    "active": "Активные",
    "rented": "Сданные",
    "archive": "Архив",
}
# Проверка канала запускается автоматически после открытия списка, но не чаще
# заданного интервала. Пользователь сразу получает список без ожидания сети.
CHANNEL_AUTO_SYNC_INTERVAL_SECONDS = int_env(
    "CHANNEL_AUTO_SYNC_INTERVAL_SECONDS", 6 * 60 * 60, minimum=900, maximum=30 * 24 * 60 * 60
)
CHANNEL_AUTO_SYNC_MAX_ITEMS = int_env("CHANNEL_AUTO_SYNC_MAX_ITEMS", 8, minimum=1, maximum=20)
CHANNEL_CHECK_VERSION = "telegram-preview-v3-conservative"

# Владельцы ссылок задаются отдельно: по публичной deep-link ссылке нельзя
# надёжно отличить сотрудника от партнёра, которого этот сотрудник привёл.
# Формат Railway Variable: ivan2:123456789,irina2:234567890
EMPLOYEE_STATS_OWNER_BY_ID = {}
for _employee_pair in clean_env("EMPLOYEE_TELEGRAM_IDS").split(","):
    _employee_code, _separator, _employee_id = _employee_pair.partition(":")
    _employee_code = _employee_code.strip()
    _employee_code = EMPLOYEE_CODE_ALIASES.get(_employee_code, _employee_code)
    _employee_id = _employee_id.strip()
    if _separator and _employee_code in EMPLOYEES and _employee_id.isdigit():
        EMPLOYEE_STATS_OWNER_BY_ID[_employee_id] = _employee_code
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    handlers=[logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class AtomicPicklePersistence(PicklePersistence):
    """Совместимая с прежним .pickle память с атомарной записью и .bak-копией."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._atomic_write_lock = threading.RLock()

    @property
    def backup_path(self):
        return Path(str(self.filepath) + ".bak")

    def _load_singlefile(self):
        try:
            return super()._load_singlefile()
        except (EOFError, OSError, TypeError, ValueError, pickle.UnpicklingError, AttributeError, ImportError):
            if not self.backup_path.exists():
                raise
            logger.exception("Основной файл памяти повреждён; восстанавливаю резервную копию")
            recovery_path = Path(str(self.filepath) + ".recovering")
            shutil.copy2(self.backup_path, recovery_path)
            os.replace(recovery_path, self.filepath)
            return super()._load_singlefile()

    def _dump_singlefile(self):
        with self._atomic_write_lock:
            original_path = self.filepath
            backup_path = Path(str(original_path) + ".bak")
            temp_path = Path(str(original_path) + ".tmp")
            backup_temp_path = Path(str(original_path) + ".bak.tmp")
            try:
                self.filepath = temp_path
                super()._dump_singlefile()
                with temp_path.open("r+b") as temp_file:
                    os.fsync(temp_file.fileno())

                if original_path.exists():
                    shutil.copy2(original_path, backup_temp_path)
                    os.replace(backup_temp_path, backup_path)
                os.replace(temp_path, original_path)

                if not backup_path.exists():
                    shutil.copy2(original_path, backup_temp_path)
                    os.replace(backup_temp_path, backup_path)
            finally:
                self.filepath = original_path
                for leftover in (temp_path, backup_temp_path):
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        pass


class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Параллельно обслуживает разных людей, но действия одного человека идут по очереди."""

    def __init__(self, max_concurrent_updates=16):
        super().__init__(max_concurrent_updates=max_concurrent_updates)
        self._locks = {}

    async def initialize(self):
        return None

    async def shutdown(self):
        self._locks.clear()

    async def do_process_update(self, update, coroutine):
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if user is not None:
            key = ("user", user.id)
        elif chat is not None:
            key = ("chat", chat.id)
        else:
            key = ("service", 0)
        entry = self._locks.get(key)
        if entry is None:
            entry = {"lock": asyncio.Lock(), "users": 0}
            self._locks[key] = entry
        entry["users"] += 1
        try:
            async with entry["lock"]:
                await coroutine
        finally:
            entry["users"] -= 1
            if entry["users"] == 0 and self._locks.get(key) is entry:
                self._locks.pop(key, None)


def is_transient_network_error(error):
    """Распознаёт краткие сбои Telegram/httpx, включая вложенную причину."""
    current = error
    seen = set()
    transient_httpx_names = {
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
    }
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # В python-telegram-bot BadRequest технически наследуется от
        # NetworkError, хотя повтор запроса не исправит битую HTML-разметку,
        # неверный chat_id или другое отклонение Telegram.
        if isinstance(current, BadRequest):
            return False
        if isinstance(current, (NetworkError, TimedOut, RetryAfter)):
            return True
        if current.__class__.__module__.startswith("httpx") and current.__class__.__name__ in transient_httpx_names:
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


def is_html_parse_error(error):
    """HTML-fallback нужен только при ошибке разметки, а не при сбое сети.

    Иначе после httpx.ReadError бот мог повторно отправить уже принятое Telegram
    фото/сообщение в plain-виде и создать визуальный дубль.
    """
    if not isinstance(error, BadRequest):
        return False
    message = str(error).lower()
    return "parse entities" in message or "can't parse" in message or "cant parse" in message

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_GENERATION_CONFIG = genai.types.GenerateContentConfig(
    temperature=0.7,
    top_p=0.9,
    automatic_function_calling=genai.types.AutomaticFunctionCallingConfig(
        disable=True,
        maximum_remote_calls=None,
    )
)
TELEGRAM_CAPTION_LIMIT = 1024
LISTING_SOFT_LIMIT = 1000
HUGE_SOURCE_CHARACTER_THRESHOLD = 2200
HUGE_SOURCE_WORD_THRESHOLD = 350
GEMINI_CONCURRENT_LIMIT = int_env("GEMINI_CONCURRENT_LIMIT", 8, minimum=1, maximum=16)
GEMINI_SEMAPHORE = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
MAX_CONCURRENT_UPDATES = int_env("MAX_CONCURRENT_UPDATES", 16, minimum=4, maximum=32)
# Та же модель и тот же полный промпт, но весь цикл Gemini (очередь + возможный
# быстрый повтор) ограничен единым бюджетом. Повтор не начинает новый бюджет.
GEMINI_TIMEOUT_SECONDS = int_env("GEMINI_TIMEOUT_SECONDS", 25, minimum=20, maximum=25)
GEMINI_MAX_ATTEMPTS = int_env("GEMINI_MAX_ATTEMPTS", 3, minimum=1, maximum=3)
GEMINI_SLOW_NOTICE_SECONDS = int_env("GEMINI_SLOW_NOTICE_SECONDS", 8, minimum=5, maximum=10)
GEMINI_SHORTEN_TIMEOUT_SECONDS = int_env("GEMINI_SHORTEN_TIMEOUT_SECONDS", 12, minimum=5, maximum=12)
STRIPE_API_TIMEOUT_SECONDS = int_env("STRIPE_API_TIMEOUT_SECONDS", 15, minimum=5, maximum=30)
STRIPE_FULFILLMENT_TIMEOUT_SECONDS = max(
    3.0,
    min(9.0, float(os.getenv("STRIPE_FULFILLMENT_TIMEOUT_SECONDS", "8"))),
)
STRIPE_SECRET_KEY_VALID = STRIPE_SECRET_KEY.startswith(("sk_test_", "sk_live_"))
STRIPE_WEBHOOK_SECRET_VALID = STRIPE_WEBHOOK_SECRET.startswith("whsec_")
STRIPE_ENABLED = bool(
    stripe
    and web
    and STRIPE_SECRET_KEY_VALID
    and STRIPE_WEBHOOK_SECRET_VALID
    and PUBLIC_BASE_URL
)
# Runtime-объект aiohttp нельзя класть в app.bot_data: PicklePersistence
# пытается сохранять bot_data в partner_bot_data.pickle и не умеет
# сериализовать AppRunner. Храним runner только в памяти процесса.
STRIPE_WEB_RUNNER = None
STRIPE_DELIVERY_TASKS = {}
STRIPE_RECOVERY_TASK = None
CHANNEL_SYNC_TASKS = {}
CHANNEL_FULL_SYNC_TASK = None
STRIPE_RECONCILIATION_SEMAPHORE = asyncio.Semaphore(4)
if stripe and STRIPE_SECRET_KEY_VALID:
    stripe.api_key = STRIPE_SECRET_KEY
elif STRIPE_SECRET_KEY and not STRIPE_SECRET_KEY_VALID:
    logger.error("STRIPE_SECRET_KEY имеет неправильный формат: нужен sk_test_... или sk_live_...")
if STRIPE_WEBHOOK_SECRET and not STRIPE_WEBHOOK_SECRET_VALID:
    logger.error("STRIPE_WEBHOOK_SECRET имеет неправильный формат: нужен whsec_...")

PROPERTY_TYPES = {
    "apartment": {
        "label": "квартира",
        "button": "Квартира",
        "rules": (
            "Партнёр выбрал тип объекта: КВАРТИРА. В заголовке обязательно должно быть понятно, "
            "что сдаётся квартира целиком. Начинай заголовок строго со слова «Квартира» кириллицей. "
            "Запрещено писать «Kvartira», «Apartman», «Apartment» или другой транслит. "
            "Формат: <b>Квартира [планировка], [метраж] м², [район]</b>. "
            "Если планировка или метраж не указаны, не придумывай их и пропусти эту часть."
        ),
    },
    "room": {
        "label": "комната",
        "button": "Комната",
        "rules": (
            "Партнёр выбрал тип объекта: КОМНАТА. В заголовке обязательно должно быть понятно, "
            "что сдаётся именно комната, а не вся квартира. Нельзя писать заголовок только как «2+1» "
            "или «Квартира 2+1». Начинай с «Комната», «Непроходная комната» или «Проходная комната» кириллицей, "
            "не используй «Komnata», «Pokoj», «Room». "
            "если это указано в тексте. Формат: <b>Комната [X м²] в квартире [планировка], [район]</b>. "
            "Площадь всей квартиры не выдавай за площадь комнаты."
        ),
    },
    "house": {
        "label": "дом",
        "button": "Дом",
        "rules": (
            "Партнёр выбрал тип объекта: ДОМ. В заголовке обязательно используй слово «Дом». "
            "Не называй объект квартирой или комнатой. Формат: <b>Дом, [метраж] м², [район/город]</b>."
        ),
    },
    "land": {
        "label": "участок",
        "button": "Участок",
        "rules": (
            "Партнёр выбрал тип объекта: УЧАСТОК. В заголовке обязательно используй слово «Участок». "
            "Не называй объект квартирой, комнатой или домом. Формат: <b>Участок, [площадь], [район/город]</b>."
        ),
    },
    "commercial": {
        "label": "коммерция",
        "button": "Коммерция",
        "rules": (
            "Партнёр выбрал тип объекта: КОММЕРЦИЯ. В заголовке обязательно покажи коммерческий характер объекта: "
            "«Коммерческое помещение», «Офис», «Магазин», «Салон» или другой тип, если он указан в тексте. "
            "Не называй объект квартирой или комнатой."
        ),
    },
    "non_residential": {
        "label": "нежилое помещение",
        "button": "Нежилое помещение",
        "rules": (
            "Партнёр выбрал тип объекта: НЕЖИЛОЕ ПОМЕЩЕНИЕ. В заголовке обязательно используй "
            "«Нежилое помещение» или более точный тип из текста. Не называй объект квартирой, комнатой или домом."
        ),
    },
    "other": {
        "label": "другое",
        "button": "Другое",
        "rules": (
            "Партнёр выбрал тип объекта: ДРУГОЕ. Определи тип только по исходному тексту, не придумывай. "
            "В заголовке обязательно назови объект человеческим словом: «Объект», «Помещение», "
            "«Место» или точный тип из текста."
        ),
    },
}


def listing_description_length_rules(raw_text):
    """Регулирует только вводное описание, не затрагивая факты в других разделах."""
    value = str(raw_text or "")
    word_count = len(re.findall(r'\S+', value))
    if (
        len(value) >= HUGE_SOURCE_CHARACTER_THRESHOLD
        or word_count >= HUGE_SOURCE_WORD_THRESHOLD
    ):
        return (
            "- Исходник очень большой или повторяющийся: вводное описание объекта до раздела «Локация» "
            "сделай компактным, примерно 30-37 слов. Это ограничение относится только к вводному описанию, "
            "а не ко всему объявлению. Удали повторы и рекламную воду, но сохрани все уникальные важные факты "
            "в подходящих разделах и финальных условиях."
        )
    return (
        "- Исходник короткий или обычный по объёму: если полезных фактов достаточно, целевой размер вводного "
        "описания — 30-37 слов. Если исходник короче, не растягивай описание до 30 слов и не добавляй фразы "
        "ради объёма. Плохой, обрывочный или иностранный текст можно полностью переформулировать, но количество "
        "деталей и общий масштаб описания должны соответствовать исходнику: улучшай язык, а не увеличивай содержание. "
        "Это правило относится только к вводному описанию, а не к разделам "
        "«Локация», «Финансовые условия», условиям заселения, контакту и хештегам."
    )


LISTING_TEMPLATE = """
Ты профессиональный редактор объявлений Binio для русскоязычных клиентов в Праге. Преврати исходный текст в красивое, естественное и полностью готовое к публикации объявление об аренде.

Тип объекта выбран партнёром:
{property_type_rules}
Это важнее исходного текста: комната не должна выглядеть как квартира, дом — как квартира, коммерция — как жильё.

Ориентир по стилю и структуре (данные из примера не копируй):
<b>Квартира 1+кк, 27 м², Прага 5, Motol</b>

Предлагается светлая квартира площадью 27 м² в спокойном районе Праги 5, Motol. Объект расположен на 2-м этаже дома без лифта, недавно отремонтирован, частично меблирован и готов к заселению.

Удобное расположение позволяет быстро добраться до общественного транспорта:

<b>Локация:</b>
— автобусная остановка Kudrnova — около 2 минут пешком
— станция метро B Nemocnice Motol — около 10 минут
— Рядом: парк, магазины и повседневная инфраструктура

<b>Финансовые условия:</b>
— Арендная плата: 18 000 Kč в месяц
— Коммунальные платежи: 3 000 Kč в месяц
— Залог: 25 000 Kč
— Комиссия агентства: 25 000 Kč

Доступна для заселения сразу.

Правила:
- Сначала молча определи язык и качество исходника, затем выбери глубину редактирования.
- Исходник может быть на русском, чешском, украинском, английском или другом языке. Всё смысловое содержание изложи на грамотном, естественном русском языке, которым говорят люди. Не делай буквальный построчный перевод и не сохраняй иностранный порядок слов или машинные кальки. Адреса, собственные названия и географические обозначения сохраняй по правилам ниже.
- Если исходник слабый — обрывочный, неграмотный, машинный, повторяющийся, плохо переведённый или хаотичный — можешь перестроить и переписать его почти полностью. Сделай связный красивый русский текст без тавтологии, но не меняй, не дополняй и не смягчай факты.
- Если исходник уже написан хорошим, естественным и связным русским языком, редактируй бережно: сохрани удачные формулировки, смысловой порядок и авторскую подачу. Исправляй ошибки, повторы, тяжёлые места и структуру, но не переписывай хороший текст целиком без необходимости.
- Если качество смешанное, сохраняй сильные фрагменты и глубоко переписывай только слабые. Степень изменения формулировок никогда не должна менять факты, суммы, ограничения или смысл.
- Пиши на русском; адреса, улицы, районы, станции и остановки всегда оставляй символ в символ как в исходнике.
- Никогда не переводи и не транслитерируй географические названия: Nusle должно остаться Nusle, а не «Нусле»; Palouček, Pražského povstání и Pankrác тоже не изменяй.
- Не придумывай конкретные факты: цену, район, улицу, метраж, этаж, планировку, залог, комиссию, коммунальные платежи, транспорт, сроки заезда.
- Пиши так, как написал бы опытный русскоязычный редактор недвижимости, а не переводчик. Не переводи исходник предложение за предложением: сначала пойми факты, затем изложи их естественно.
- Формулировки делай лучше исходных: плавно, понятно, уважительно, без буквального перевода, канцелярита и рекламных преувеличений.
- В описании не повторяй подряд «квартира», «комната», «дом», «объект» или другое название недвижимости. После первого упоминания перестрой следующую фразу без повторного подлежащего; не заменяй каждый повтор словом «жильё».
- Не начинай два соседних предложения одинаково и не повторяй одну мысль разными словами.
- Предпочитай естественные слова «есть», «можно», «подходит», «сдаётся». Не используй канцелярские обороты «имеется», «данный объект», «осуществляется», «предоставляется возможность», «оборудована для проживания», «возможно оформление». Пиши, например, «Можно оформить прописку».
- Не называй помещение «уютным», «современным», «просторным», «идеальным» или «полностью оборудованным», если это прямо не подтверждается исходником. Не скрывай недостатки: отсутствие кухни, лифта или мебели сообщай спокойно и прямо.
- Сроки и расписание передавай точно, не переосмысливай: «в течение недели», «по будням», «на следующей неделе» и конкретная дата — разные условия.
- Не делай сухую сводку. Короткий исходник улучшай без заметного увеличения объёма: не добавляй новые предложения только ради длины. Подробный исходник упорядочивай и сокращай без потери уникальных фактов.
- Объём только вводного описания выбирай по количеству фактов. Обычно это 1-4 связных предложения, не больше 37 слов; если исходник короткий, описание должно быть короче этого ориентира. Разделы с локацией, финансами и условиями в эти слова не входят.
{description_length_rules}
- Сохрани все важные факты: дату заезда, вместимость, мебель, технику, удобства, состояние, этаж, лифт, животных и другие условия, если они есть в исходнике.
- Обязательно сохрани статус объекта и юридические ограничения: например, что это ательер и что оформление trvalý/přechodný pobyt невозможно.
- Каждую отдельно указанную сумму или финансовое условие сохрани отдельной понятной строкой. Не объединяй коммунальные платежи, электричество и интернет в одни скобки и не теряй ни одну из этих сумм.
- Если точное время в пути не указано, пиши «несколько минут пешком». Не пиши неестественное «около нескольких минут». Форму «около X минут» используй только при наличии числа в исходнике.
- Раздел «Локация» добавляй только при наличии в исходнике адреса, транспорта или инфраструктуры. Не выдумывай пункты ради заполнения шаблона.
- В «Финансовых условиях» показывай только те платежи, которые реально указаны. Не оставляй пустые строки и шаблонные X.
- Не используй фразы: «квартира встречает», «локация предлагает», «пространство порадует», «данное помещение», «внутри найдёте».
- Ориентир естественной редактуры: вместо «Квартира оборудована для проживания, имеется мебель, возможно оформление прописки» пиши «Подходит для одного человека. Основная мебель уже есть. Можно оформить прописку». Вместо «Просмотры возможны по предварительной договорённости» пиши «Просмотр — по предварительной договорённости».
- Общий текст с контактом и хештегами должен помещаться в 1000 символов. Не сокращай хорошее подробное объявление до 700-800 символов только ради краткости; используй доступный объём для ясности и красивой структуры.
- Перед ответом молча перечитай готовый текст: убери повторы существительных и одинаковые начала предложений, замени машинные и канцелярские обороты на живой русский, затем ещё раз проверь все суммы и условия. Не описывай эту проверку в ответе.

Формат:
<b>[Заголовок]</b>

[Описание объекта]

<b>Локация:</b>
— [транспорт]: [X минут пешком / несколько минут пешком]
— Рядом: [важная инфраструктура, если есть]

<b>Финансовые условия:</b>
— Арендная плата: X Kč
— Коммунальные платежи: X Kč
— Электричество: X Kč
— Интернет: X Kč
— Залог: X Kč
— Комиссия агентства: X Kč

[Дата заселения и важные условия, если они есть]

[[CONTACT]]

#[тип] #[район] #pronajem

Заголовок: жирный через <b>, начинается с типа объекта согласно выбранным правилам. Части заголовка разделяй запятыми: «Прага 4, Braník», а не «Прага 4 - Braník». Разделы тоже жирные. Без эмодзи и звёздочек.
Хештеги: максимум 3. Второй хештег всегда район Праги в формате #Praha1...#Praha10, если он указан в тексте; не используй микрорайоны вроде #holesovice, #smichov, #vinohrady. 2kk/2кк/2+kk/2+кк = #2kk; 3kk/3кк/3+kk/3+кк = #3kk; 4kk/4кк/4+kk/4+кк = #4kk. #2plus1/#3plus1/#4plus1 только для явных 2+1/3+1/4+1. Комната #pokoj, дом #dum, участок #pozemek, коммерция/нежилое #komerce.
Контакт: вставь ровно [[CONTACT]] отдельной строкой. Не добавляй Markdown-ссылку, имя, username или URL рядом с этим маркером.

ТЕКСТ ОТ ПАРТНЁРА:
{text}

Верни только готовое объявление, без пояснений.
"""

SHORTEN_TEMPLATE = """
Минимально сократи объявление до {limit} символов только для технического лимита Telegram.
Не переписывай его заново: сохрани структуру, порядок разделов, тон и удачные формулировки исходного варианта.
Сначала убирай повторы и лишние прилагательные. Не превращай текст в один сухой абзац или краткую сводку.
Обязательно сохрани HTML <b>, тип объекта, все суммы и условия, дату заезда, мебель, животных, контакт и хештеги.
Не придумывай данные и не меняй смысл.
Адреса, улицы, районы, станции и остановки оставляй как в исходном объявлении.
Верни только готовое объявление. Не добавляй вступление, пояснение, количество символов, кавычки, разделитель --- или Markdown-блок кода.

ОБЪЯВЛЕНИЕ:
{text}
"""


def validate_config():
    missing = []

    def is_empty_or_placeholder(value, placeholder_prefix):
        return (
            not value
            or value.strip().startswith(placeholder_prefix)
        )

    if is_empty_or_placeholder(PARTNER_BOT_TOKEN, "PASTE_NEW_TELEGRAM_BOT_TOKEN"):
        missing.append("PARTNER_BOT_TOKEN")
    elif not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{20,}", PARTNER_BOT_TOKEN):
        missing.append("PARTNER_BOT_TOKEN (неверный формат)")
    if is_empty_or_placeholder(GEMINI_API_KEY, "PASTE_NEW_GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", CHANNEL_USERNAME):
        missing.append("CHANNEL_USERNAME (нужен публичный @username канала)")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", BOT_USERNAME):
        missing.append("BOT_USERNAME (без @, 5–32 символа)")

    # Частично заполненный Stripe нельзя считать отключённым: в таком случае
    # бот запустится, но платная публикация будет незаметно недоступна.
    # Если Stripe-переменные не заданы вообще, сохраняем совместимость со
    # старым Telegram provider token.
    # Один только домен может использоваться и без Stripe (например, для
    # Telegram provider token), поэтому признаком начатой Stripe-настройки
    # считаем именно наличие Stripe-ключа или webhook secret.
    stripe_values_present = any((STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET))
    if stripe_values_present:
        if not STRIPE_SECRET_KEY_VALID:
            missing.append("STRIPE_SECRET_KEY (нужен sk_test_ или sk_live_)")
        if not STRIPE_WEBHOOK_SECRET_VALID:
            missing.append("STRIPE_WEBHOOK_SECRET (нужен whsec_)")
        if not PUBLIC_BASE_URL:
            missing.append("PUBLIC_BASE_URL")
        else:
            parsed_base_url = urlparse(PUBLIC_BASE_URL)
            if parsed_base_url.scheme != "https" or not parsed_base_url.netloc:
                missing.append("PUBLIC_BASE_URL (нужен полный HTTPS-адрес)")

    if missing:
        interesting_env = sorted(
            key for key in os.environ
            if any(word in key.upper() for word in ("BOT", "TOKEN", "GEMINI", "API", "PARTNER", "RAILWAY"))
        )
        raise RuntimeError(
            "Не заполнены настройки Railway: " + ", ".join(missing) +
            ". Railway передал в запуск такие похожие переменные: " +
            (", ".join(interesting_env) if interesting_env else "нет похожих переменных") +
            ". Значения токенов в лог не выводятся."
        )


async def keep_chat_action(bot, chat_id, action=ChatAction.TYPING, interval=4.0):
    """Показывает пользователю, что бот жив и всё ещё обрабатывает запрос."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Не удалось отправить chat action: {e}")
        await asyncio.sleep(interval)


async def setup_bot_commands(app):
    removed = cleanup_bot_memory(app.bot_data)
    if sum(removed.values()):
        logger.info(f"Очистка памяти при запуске: {cleanup_summary_text(removed)}")

    interrupted = recover_interrupted_states(app.bot_data)
    if interrupted["review"] or interrupted["publish"]:
        # После падения процесса нельзя автоматически повторять внешнюю отправку:
        # предыдущий запрос мог дойти до Telegram, а потерялся только ответ.
        await persist_now(app)
        review_ids = ", ".join(interrupted["review"][:20]) or "нет"
        publish_ids = ", ".join(interrupted["publish"][:20]) or "нет"
        try:
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    "⚠️ После перезапуска найдены незавершённые внешние операции.\n\n"
                    f"Доставка в чат проверки: {review_ids}\n"
                    f"Публикация в канал: {publish_ids}\n\n"
                    "Сначала проверьте последние сообщения/посты. Если результата точно нет, "
                    "используйте /retry_review ID или /retry_publish ID."
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось сообщить админу о незавершённых операциях: {e}")

    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Выбрать тип публикации"),
            BotCommand("partner", "Публикация для партнёра / риэлтора"),
            BotCommand("owner", "Разместить объявление как собственник"),
            BotCommand("mylistings", "Мои объявления"),
            BotCommand("drafts", "Незавершённые объявления"),
            BotCommand("cancel", "Отменить текущий шаг"),
            BotCommand("employee", "Сменить сотрудника для контакта"),
            BotCommand("mystats", "Моя статистика"),
            BotCommand("terms", "Условия оплаты и публикации"),
            BotCommand("support", "Поддержка по оплате и публикации"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось обновить меню команд Telegram: {e}")

    await start_stripe_webhook_server(app)
    start_stripe_delivery_recovery(app)


async def reject_non_private_callback(update, context):
    """Не позволяет запускать пользовательские сценарии из групповых чатов."""
    query = update.callback_query
    chat = getattr(getattr(query, "message", None), "chat", None)
    if chat is None or chat.id == ADMIN_CHAT_ID or str(getattr(chat, "type", "")) == "private":
        return
    await query.answer("Для защиты данных откройте бот в личном чате.", show_alert=True)
    raise ApplicationHandlerStop


async def reject_non_private_message(update, context):
    """Не позволяет запускать личные сценарии командами или файлами из групп."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or chat.id == ADMIN_CHAT_ID or str(chat.type) == "private":
        return
    await message.reply_text("Для защиты данных откройте бот в личном чате.")
    raise ApplicationHandlerStop


def get_state(context, user_id):
    return context.application.bot_data.get(f"state_{user_id}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def month_key(value=None):
    dt = parse_iso(value) if value else datetime.now(timezone.utc)
    if not dt:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def is_older_than(value, ttl_seconds):
    dt = parse_iso(value)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() > ttl_seconds


def touch_user_activity(context, user_id):
    context.application.bot_data[f"activity_updated_{user_id}"] = now_iso()


def set_state(context, user_id, state):
    context.application.bot_data[f"state_{user_id}"] = state
    touch_user_activity(context, user_id)


def consume_daily_gemini_request(context, user_id, key_prefix, limit):
    """Атомарно для одного пользователя расходует дневную квоту Gemini."""
    if is_admin(user_id) or limit <= 0:
        return True
    key = f"{key_prefix}{user_id}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = context.application.bot_data.get(key)
    if not isinstance(usage, dict) or usage.get("date") != today:
        usage = {"date": today, "count": 0}
    if int(usage.get("count", 0)) >= limit:
        return False
    usage["count"] = int(usage.get("count", 0)) + 1
    context.application.bot_data[key] = usage
    touch_user_activity(context, user_id)
    return True


def consume_public_gemini_request(context, user_id):
    return consume_daily_gemini_request(
        context, user_id, "public_gemini_usage_", PUBLIC_GEMINI_DAILY_LIMIT
    )


def consume_partner_gemini_request(context, user_id):
    return consume_daily_gemini_request(
        context, user_id, "partner_gemini_usage_", PARTNER_GEMINI_DAILY_LIMIT
    )


def refund_daily_gemini_request(context, user_id, key_prefix):
    """Не считает попытку, если внешний AI-сервис фактически завершился ошибкой."""
    key = f"{key_prefix}{user_id}"
    usage = context.application.bot_data.get(key)
    if not isinstance(usage, dict) or usage.get("date") != datetime.now(timezone.utc).date().isoformat():
        return
    usage["count"] = max(0, int(usage.get("count", 0)) - 1)
    context.application.bot_data[key] = usage


def is_admin(user_id):
    return user_id == ADMIN_TELEGRAM_ID


def is_admin_edit_sender(update):
    """Разрешает правку владельцу бота и анонимному админу служебной группы.

    При включённом Telegram Privacy Mode анонимный администратор приходит как
    sender_chat служебной группы, а не как обычный effective_user.
    """
    user = getattr(update, "effective_user", None)
    if user is not None and is_admin(getattr(user, "id", None)):
        return True

    message = getattr(update, "effective_message", None)
    chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
    sender_chat = getattr(message, "sender_chat", None)
    return bool(
        chat is not None
        and getattr(chat, "id", None) == ADMIN_CHAT_ID
        and sender_chat is not None
        and getattr(sender_chat, "id", None) == ADMIN_CHAT_ID
    )


def clear_admin_edit_session(bot_data, listing_id=None):
    """Закрывает одноразовый режим правки и связанные с ForceReply данные."""
    current = bot_data.get("admin_editing_listing_id")
    if listing_id is not None and current != listing_id:
        return
    bot_data.pop("admin_editing_listing_id", None)
    bot_data.pop("admin_editing_prompt_message_id", None)
    bot_data.pop("admin_editing_user_id", None)


def admin_edit_reply_markup(chat):
    """ForceReply допустим в группах, но Telegram-канал требует inline keyboard."""
    chat_type = getattr(chat, "type", "")
    chat_type = str(getattr(chat_type, "value", chat_type)).lower()
    if chat_type == "channel":
        return None
    return ForceReply(
        selective=False,
        input_field_placeholder="Вставьте исправленный текст",
    )


def get_property_type(type_key):
    return PROPERTY_TYPES.get(type_key) or PROPERTY_TYPES["other"]


def property_type_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(PROPERTY_TYPES["apartment"]["button"], callback_data="property_type_apartment"),
            InlineKeyboardButton(PROPERTY_TYPES["room"]["button"], callback_data="property_type_room"),
        ],
        [
            InlineKeyboardButton(PROPERTY_TYPES["house"]["button"], callback_data="property_type_house"),
            InlineKeyboardButton(PROPERTY_TYPES["land"]["button"], callback_data="property_type_land"),
        ],
        [InlineKeyboardButton(PROPERTY_TYPES["commercial"]["button"], callback_data="property_type_commercial")],
        [InlineKeyboardButton(PROPERTY_TYPES["non_residential"]["button"], callback_data="property_type_non_residential")],
        [InlineKeyboardButton(PROPERTY_TYPES["other"]["button"], callback_data="property_type_other")],
    ])


def new_listing_id():
    return uuid.uuid4().hex[:10]


def pending_key(listing_id):
    return f"pending_listing_{listing_id}"


def get_pending(context, listing_id):
    """Новые заявки хранятся по listing_id. Старый ключ pending_<user_id>
    оставлен как fallback, чтобы не сломать уже созданные кнопки после обновления."""
    return (
        context.application.bot_data.get(pending_key(listing_id))
        or context.application.bot_data.get(f"pending_{listing_id}")
    )


def save_pending(context, listing_id, data):
    timestamp = now_iso()
    data.setdefault("created_at", timestamp)
    data["updated_at"] = timestamp
    context.application.bot_data[pending_key(listing_id)] = data


def pending_busy(pending, field, ttl_seconds=300):
    value = pending.get(field)
    if not value:
        return False
    if not isinstance(value, dict):
        return False
    started_at = value.get("started_at")
    return bool(started_at and time.time() - started_at < ttl_seconds)


def mark_pending_busy(context, listing_id, pending, field, value=True):
    pending[field] = {"value": value, "started_at": time.time()}
    save_pending(context, listing_id, pending)


def clear_pending_busy(context, listing_id, pending, field):
    pending.pop(field, None)
    save_pending(context, listing_id, pending)


def delete_pending(context, listing_id):
    context.application.bot_data.pop(pending_key(listing_id), None)
    context.application.bot_data.pop(f"pending_{listing_id}", None)  # старый формат


def list_pending_keys(context):
    return [
        k for k in context.application.bot_data
        if k.startswith("pending_listing_") or k.startswith("pending_")
    ]


def list_unique_pending_items(context):
    items = {}
    for key in list_pending_keys(context):
        listing_id = listing_id_from_pending_key(key)
        pending = get_pending(context, listing_id)
        if isinstance(pending, dict):
            items[listing_id] = pending
    return items


def listing_id_from_pending_key(key):
    if key.startswith("pending_listing_"):
        return key.replace("pending_listing_", "", 1)
    return key.replace("pending_", "", 1)


def published_key(listing_id):
    return f"published_listing_{listing_id}"


def get_published(context, listing_id):
    return context.application.bot_data.get(published_key(listing_id))


def save_published(context, listing_id, data):
    timestamp = now_iso()
    data.setdefault("created_at", timestamp)
    data["updated_at"] = timestamp
    context.application.bot_data[published_key(listing_id)] = data


def payment_key(listing_id):
    return f"payment_record_{listing_id}"


def history_key(listing_id):
    return f"history_listing_{listing_id}"


def get_payment_record(context, listing_id):
    return context.application.bot_data.get(payment_key(listing_id))


def save_payment_record(context, listing_id, data):
    timestamp = now_iso()
    current = get_payment_record(context, listing_id)
    record = dict(current) if isinstance(current, dict) else {}
    record.update({key: value for key, value in data.items() if value is not None})
    record["listing_id"] = str(listing_id)
    record.setdefault("created_at", timestamp)
    record["updated_at"] = timestamp
    context.application.bot_data[payment_key(listing_id)] = record
    return record


def save_listing_history(context, listing_id, pending, status, **extra):
    """Сохраняет завершённую заявку, не теряя оплату и статистику."""
    timestamp = now_iso()
    history = dict(pending or {})
    history.update(extra)
    history["listing_id"] = str(listing_id)
    history["history_status"] = status
    history["status"] = status
    history.setdefault("created_at", timestamp)
    history["updated_at"] = timestamp
    history[f"{status}_at"] = timestamp
    context.application.bot_data[history_key(listing_id)] = history
    return history


def expected_payment_amount(pending):
    try:
        return int(pending.get("payment_expected_amount", PUBLIC_PAYMENT_AMOUNT))
    except (TypeError, ValueError):
        return PUBLIC_PAYMENT_AMOUNT


def expected_payment_currency(pending):
    return str(pending.get("payment_expected_currency") or PUBLIC_PAYMENT_CURRENCY).upper()


def record_confirmed_payment(context, listing_id, pending, provider, amount, currency, **identifiers):
    paid_at = pending.get("payment_paid_at") or now_iso()
    return save_payment_record(context, listing_id, {
        "partner_id": pending.get("partner_id"),
        "source": pending.get("source", "public"),
        "provider": provider,
        "paid": True,
        "payment_status": "paid",
        "payment_paid_at": paid_at,
        "payment_total_amount": amount,
        "payment_currency": str(currency or "").upper(),
        "payment_test_mode": bool(pending.get("payment_test_mode")),
        **identifiers,
    })


def find_payment_record_by_intent(context, payment_intent):
    if not payment_intent:
        return None, None
    for key, value in context.application.bot_data.items():
        if not key.startswith("payment_record_") or not isinstance(value, dict):
            continue
        if value.get("stripe_payment_intent") == payment_intent:
            return key.replace("payment_record_", "", 1), value
    return None, None


def requires_payment_manual_review(context, listing_id, pending=None):
    pending = pending or (get_pending(context, listing_id) if listing_id else None)
    if isinstance(pending, dict) and pending.get("payment_review_required"):
        return True
    record = get_payment_record(context, listing_id) if listing_id else None
    return bool(isinstance(record, dict) and record.get("payment_status") == "manual_review")


def deferred_stripe_event_key(event_id):
    return f"deferred_stripe_status_{event_id}"


def apply_stripe_status_to_payment(context, listing_id, event_type, stripe_object, event_id=None):
    """Применяет возврат/спор к журналу и возвращает новый статус платежа."""
    if event_type == "charge.refunded":
        try:
            charge_amount = int(stripe_object.get("amount") or 0)
        except (TypeError, ValueError):
            charge_amount = 0
        try:
            amount_refunded = int(stripe_object.get("amount_refunded") or 0)
        except (TypeError, ValueError):
            amount_refunded = 0
        fully_refunded = bool(stripe_object.get("refunded")) or (
            charge_amount > 0 and amount_refunded >= charge_amount
        )
        payment_status = "refunded" if fully_refunded else "partially_refunded"
        extra = {
            "stripe_charge_amount": charge_amount or None,
            "stripe_amount_refunded": amount_refunded,
        }
    elif event_type == "charge.dispute.created":
        payment_status = "disputed"
        extra = {"stripe_dispute_amount": stripe_object.get("amount")}
    else:
        dispute_status = str(stripe_object.get("status") or "closed")
        payment_status = f"dispute_{dispute_status}"
        extra = {"stripe_dispute_amount": stripe_object.get("amount")}

    save_payment_record(context, listing_id, {
        "payment_status": payment_status,
        "stripe_charge_id": stripe_object.get("charge") or stripe_object.get("id"),
        "stripe_last_event_id": event_id,
        "stripe_last_event_type": event_type,
        "stripe_last_event_at": now_iso(),
        **extra,
    })

    pending = get_pending(context, listing_id)
    if isinstance(pending, dict) and not pending.get("submitted_to_admin"):
        if payment_status == "dispute_won":
            pending.pop("payment_review_required", None)
            pending.pop("payment_review_reason", None)
            pending.pop("paid_delivery_needs_fix", None)
        else:
            pending["payment_review_required"] = True
            pending["payment_review_reason"] = f"stripe_status:{payment_status}"
            pending["paid_delivery_needs_fix"] = True
        save_pending(context, listing_id, pending)
    return payment_status


def apply_deferred_stripe_status_events(context, listing_id, payment_intent):
    applied = []
    if not payment_intent:
        return applied
    prefix = "deferred_stripe_status_"
    for key, value in list(context.application.bot_data.items()):
        if not key.startswith(prefix) or not isinstance(value, dict):
            continue
        if value.get("payment_intent") != payment_intent:
            continue
        payment_status = apply_stripe_status_to_payment(
            context,
            listing_id,
            value.get("event_type"),
            value,
            event_id=value.get("event_id"),
        )
        applied.append(payment_status)
        context.application.bot_data.pop(key, None)
    return applied


def public_invoice_active(pending):
    if pending.get("paid") or pending.get("submitted_to_admin"):
        return False
    invoice_created_at = pending.get("invoice_created_at")
    if not invoice_created_at:
        return False
    return not is_older_than(invoice_created_at, PUBLIC_INVOICE_TTL_HOURS * 60 * 60)


def public_monthly_count(context, user_id, target_month=None, exclude_listing_id=None):
    if is_admin(user_id) or PUBLIC_MONTHLY_LIMIT <= 0:
        return 0
    target_month = target_month or month_key()
    exclude_listing_id = str(exclude_listing_id) if exclude_listing_id else None
    total = 0

    for item in stats_all_items(context):
        listing_id = str(item.get("listing_id") or "")
        if exclude_listing_id and listing_id == exclude_listing_id:
            continue
        if item.get("partner_id") != user_id or item.get("source") != "public":
            continue
        if not (
            item.get("paid")
            or item.get("payment_status") == "paid"
            or item.get("payment_status") == "manual_review"
            or item.get("payment_review_required")
            or item.get("submitted_to_admin")
            or item.get("published_at")
            or public_invoice_active(item)
        ):
            continue
        item_month = month_key(
            item.get("payment_paid_at")
            or item.get("paid_at")
            or item.get("submitted_at")
            or item.get("published_at")
            or item.get("invoice_created_at")
            or item.get("updated_at")
            or item.get("created_at")
        )
        if item_month == target_month:
            total += 1

    return total


def public_limit_reached(context, user_id, exclude_listing_id=None):
    return (
        PUBLIC_MONTHLY_LIMIT > 0
        and public_monthly_count(context, user_id, exclude_listing_id=exclude_listing_id) >= PUBLIC_MONTHLY_LIMIT
    )


def public_limit_message(context, user_id):
    used = public_monthly_count(context, user_id)
    contact_safe = html.escape(DEFAULT_CONTACT, quote=True)
    return (
        "Лимит разовых публикаций на этот месяц уже использован.\n\n"
        f"Доступно: {PUBLIC_MONTHLY_LIMIT} публикации в месяц\n"
        f"Использовано: {used}\n\n"
        "Если вы публикуете объявления регулярно, напишите администратору и обсудите партнёрский доступ: "
        f'<a href="{contact_safe}">контакт Binio</a>'
    )


def bot_chat_url():
    """Open the existing bot chat without sending a new /start command."""
    return f"https://t.me/{BOT_USERNAME}"


def stripe_success_url():
    return f"{PUBLIC_BASE_URL}/stripe-success"


def stripe_cancel_url(listing_id):
    return f"{PUBLIC_BASE_URL}/stripe-cancel?listing_id={quote(str(listing_id), safe='')}"


def stripe_configuration_status():
    if STRIPE_ENABLED:
        return "подключён"
    if not stripe or not web:
        return "не подключён: библиотека Stripe/aiohttp недоступна"
    if not STRIPE_SECRET_KEY:
        return "не подключён: STRIPE_SECRET_KEY не задан"
    if not STRIPE_SECRET_KEY_VALID:
        return "не подключён: STRIPE_SECRET_KEY должен начинаться с sk_test_ или sk_live_"
    if not STRIPE_WEBHOOK_SECRET:
        return "не подключён: STRIPE_WEBHOOK_SECRET не задан"
    if not STRIPE_WEBHOOK_SECRET_VALID:
        return "не подключён: STRIPE_WEBHOOK_SECRET должен начинаться с whsec_"
    if not PUBLIC_BASE_URL:
        return "не подключён: PUBLIC_BASE_URL не задан"
    return "не подключён: проверьте Railway Logs"


class BotReplyTarget:
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def reply_text(self, text, **kwargs):
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)


class AppContext:
    def __init__(self, app):
        self.application = app
        self.bot = app.bot


class PaidUser:
    def __init__(self, user_id):
        self.id = user_id
        self.username = None
        self.full_name = None
        self.first_name = None


def context_from_app(app):
    return AppContext(app)


def get_pending_from_app(app, listing_id):
    return get_pending(context_from_app(app), listing_id)


def save_pending_to_app(app, listing_id, data):
    return save_pending(context_from_app(app), listing_id, data)


def public_limit_reached_for_app(app, user_id, exclude_listing_id=None):
    return public_limit_reached(context_from_app(app), user_id, exclude_listing_id=exclude_listing_id)


async def persist_now(app):
    """Немедленно фиксирует критическое состояние, если приложение уже запущено."""
    updater = getattr(app, "update_persistence", None)
    if updater is not None:
        await updater()


async def _paid_delivery_job(app, listing_id):
    try:
        pending = get_pending_from_app(app, listing_id)
        if (
            not pending
            or not pending.get("paid")
            or pending.get("submitted_to_admin")
            or pending.get("payment_review_required")
            or pending.get("paid_delivery_needs_fix")
        ):
            return
        user_id = pending.get("partner_id")
        if user_id is None:
            return
        await submit_paid_public_listing(
            context_from_app(app),
            listing_id,
            PaidUser(user_id),
            BotReplyTarget(app.bot, user_id),
            test_mode=bool(pending.get("payment_test_mode")),
            raise_on_delivery_failure=False,
            background_delivery=True,
        )
        await persist_now(app)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Фоновая доставка оплаченного объявления не удалась: listing_id=%s", listing_id)
    finally:
        current = asyncio.current_task()
        if STRIPE_DELIVERY_TASKS.get(listing_id) is current:
            STRIPE_DELIVERY_TASKS.pop(listing_id, None)


def schedule_paid_delivery(app, listing_id):
    existing = STRIPE_DELIVERY_TASKS.get(listing_id)
    if existing is not None and not existing.done():
        return existing
    creator = getattr(app, "create_task", None)
    coroutine = _paid_delivery_job(app, listing_id)
    if creator is not None:
        task = creator(coroutine, name=f"paid-delivery-{listing_id}")
    else:
        task = asyncio.create_task(coroutine, name=f"paid-delivery-{listing_id}")
    STRIPE_DELIVERY_TASKS[listing_id] = task
    return task


async def stripe_delivery_recovery_loop(app):
    """После рестарта и сетевых сбоев повторно подхватывает оплаченные заявки."""
    # post_init вызывается непосредственно перед переходом Application в running.
    # Не планируем дочерние PTB-задачи раньше этого момента.
    while getattr(app, "running", True) is False:
        await asyncio.sleep(0.25)
    while True:
        try:
            for listing_id, pending in list_unique_pending_items(context_from_app(app)).items():
                if (
                    pending.get("source") == "public"
                    and pending.get("paid")
                    and not pending.get("submitted_to_admin")
                    and not pending.get("paid_delivery_needs_fix")
                    and not pending.get("payment_review_required")
                ):
                    if pending.get("submit_in_progress") and not pending_busy(pending, "submit_in_progress", ttl_seconds=90):
                        pending.pop("submit_in_progress", None)
                        save_pending_to_app(app, listing_id, pending)
                    schedule_paid_delivery(app, listing_id)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фонового восстановления оплаченных заявок")
            await asyncio.sleep(10)


def start_stripe_delivery_recovery(app):
    global STRIPE_RECOVERY_TASK
    if STRIPE_RECOVERY_TASK is None or STRIPE_RECOVERY_TASK.done():
        # В post_init приложение ещё не running. Задача хранится глобально и
        # явно отменяется/ожидается при остановке, поэтому asyncio здесь надёжен.
        STRIPE_RECOVERY_TASK = asyncio.create_task(
            stripe_delivery_recovery_loop(app),
            name="stripe-delivery-recovery",
        )


def remember_stripe_event(app, event_id):
    if not event_id:
        return
    events = app.bot_data.get("processed_stripe_events")
    if not isinstance(events, dict):
        events = {}
    events[str(event_id)] = now_iso()
    if len(events) > 2000:
        oldest = sorted(events.items(), key=lambda item: item[1])[:-2000]
        for old_event_id, _ in oldest:
            events.pop(old_event_id, None)
    app.bot_data["processed_stripe_events"] = events


async def create_stripe_checkout_session(listing_id, pending):
    if not STRIPE_ENABLED:
        raise RuntimeError("Stripe не подключён")

    expires_at = int(pending.get("stripe_checkout_expires_at") or (time.time() + PUBLIC_INVOICE_TTL_HOURS * 60 * 60))
    idempotency_key = pending.get("stripe_checkout_idempotency_key")
    if not idempotency_key:
        # Миграция для старых незавершённых предпросмотров, созданных до
        # включения идемпотентности: новый ключ привязывается к этой попытке.
        idempotency_key = f"binio-checkout-{listing_id}-{uuid.uuid4().hex}"
        pending["stripe_checkout_idempotency_key"] = idempotency_key
        pending["stripe_checkout_expires_at"] = expires_at
    amount = expected_payment_amount(pending)
    currency = expected_payment_currency(pending)
    return await asyncio.wait_for(
        asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": currency.lower(),
                    "product_data": {
                        "name": "Публикация объявления Binio",
                    },
                    "unit_amount": amount,
                },
                "quantity": 1,
            }],
            success_url=stripe_success_url() + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=stripe_cancel_url(listing_id),
            client_reference_id=listing_id,
            expires_at=expires_at,
            metadata={
                "listing_id": listing_id,
                "user_id": str(pending.get("partner_id")),
                "source": "public",
                "expected_amount": str(amount),
                "expected_currency": currency,
            },
            idempotency_key=idempotency_key,
        ),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
    )


async def send_stripe_checkout_link(query, context, listing_id, pending):
    try:
        session = await create_stripe_checkout_session(listing_id, pending)
        checkout_url = getattr(session, "url", None) or session.get("url")
        session_id = getattr(session, "id", None) or session.get("id")
        if not checkout_url or not session_id:
            raise RuntimeError("Stripe не вернул ссылку оплаты")
    except Exception as e:
        logger.error(f"Stripe checkout create error: {e}")
        pending.pop("invoice_created_at", None)
        clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
        await query.message.reply_text("Не получилось создать страницу оплаты\n\nПопробуйте ещё раз позже")
        return

    pending["stripe_session_id"] = session_id
    pending["stripe_checkout_url"] = checkout_url
    pending["payment_provider"] = "stripe"
    clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
    display_amount = f"{expected_payment_amount(pending) / 100:g} {expected_payment_currency(pending)}"
    await persist_now(context.application)

    await query.message.reply_text(
        "Счёт создан\n\n"
        "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты Stripe. "
        "После успешной оплаты бот автоматически отправит объявление на проверку",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💳 Оплатить {display_amount}", url=checkout_url)
        ]])
    )


async def record_stripe_manual_review(app, listing_id, user_id, session, reason, pending=None):
    """Надёжно принимает webhook, но переводит платёж в ручную проверку."""
    context = context_from_app(app)
    record_id = str(listing_id or session.get("id") or f"unknown-{uuid.uuid4().hex[:10]}")
    previous = get_payment_record(context, record_id) or {}
    first_notice = not previous.get("manual_review_notice_sent")
    record = save_payment_record(context, record_id, {
        "partner_id": user_id,
        "source": "public",
        "provider": "stripe",
        "payment_status": "manual_review",
        "manual_review_reason": reason,
        "payment_paid_at": now_iso(),
        "payment_total_amount": session.get("amount_total"),
        "payment_currency": str(session.get("currency", "")).upper(),
        "stripe_session_id": session.get("id"),
        "stripe_payment_intent": session.get("payment_intent"),
        "manual_review_notice_sent": True,
    })
    if pending is not None:
        pending["payment_review_required"] = True
        pending["payment_review_reason"] = reason
        pending["payment_provider"] = "stripe"
        pending["stripe_session_id"] = session.get("id")
        pending["stripe_payment_intent"] = session.get("payment_intent")
        save_pending_to_app(app, record_id, pending)
    await persist_now(app)

    if first_notice:
        try:
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    "⚠️ Stripe-платёж принят в ручную проверку.\n\n"
                    f"listing_id={record_id}\n"
                    f"user_id={user_id}\n"
                    f"причина: {reason}\n"
                    f"сумма: {record.get('payment_currency')} {record.get('payment_total_amount')}"
                ),
            )
        except Exception as notify_error:
            logger.error(f"Stripe manual review: не удалось предупредить администратора: {notify_error}")
        if user_id is not None:
            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "✅ Платёж получен и передан администратору на ручную проверку.\n\n"
                        "Повторно оплачивать не нужно. Администратор уже уведомлён."
                    ),
                )
            except Exception as user_notify_error:
                logger.error(f"Stripe manual review: не удалось уведомить пользователя: {user_notify_error}")
    return True


async def handle_stripe_checkout_completed(app, session, background=False):
    metadata = session.get("metadata") or {}
    listing_id = metadata.get("listing_id") or session.get("client_reference_id")
    user_id_raw = metadata.get("user_id")
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        user_id = None

    if session.get("payment_status") != "paid":
        logger.info(f"Stripe webhook: payment_status={session.get('payment_status')}")
        return True

    pending = get_pending_from_app(app, listing_id) if listing_id else None
    if not listing_id or user_id is None:
        logger.warning("Stripe webhook: нет корректных listing_id/user_id в metadata")
        return await record_stripe_manual_review(
            app, listing_id, user_id, session, "нет корректных listing_id/user_id", pending
        )
    if not pending or pending.get("source") != "public":
        logger.warning(f"Stripe webhook: заявка не найдена listing_id={listing_id}")
        return await record_stripe_manual_review(
            app, listing_id, user_id, session, "сохранённая public-заявка не найдена", pending
        )
    if pending.get("partner_id") != user_id:
        logger.warning(f"Stripe webhook: user_id mismatch listing_id={listing_id}")
        return await record_stripe_manual_review(
            app, listing_id, user_id, session, "user_id не совпал с владельцем заявки", pending
        )

    amount_total = session.get("amount_total")
    currency = str(session.get("currency", "")).upper()
    metadata_amount = metadata.get("expected_amount")
    try:
        metadata_amount = int(metadata_amount) if metadata_amount is not None else None
    except (TypeError, ValueError):
        metadata_amount = None
    expected_amount = expected_payment_amount(pending) if pending else metadata_amount
    expected_currency = expected_payment_currency(pending) if pending else str(metadata.get("expected_currency") or "").upper()
    if amount_total != expected_amount or currency != expected_currency:
        logger.error(
            f"Stripe webhook amount mismatch: {currency} {amount_total}, "
            f"expected {expected_currency} {expected_amount}"
        )
        return await record_stripe_manual_review(
            app,
            listing_id,
            user_id,
            session,
            f"сумма/валюта не совпали с invoice: ожидалось {expected_currency} {expected_amount}",
            pending,
        )

    pending["paid"] = True
    pending.setdefault("payment_paid_at", now_iso())
    pending["payment_provider"] = "stripe"
    pending["payment_total_amount"] = amount_total
    pending["payment_currency"] = currency
    pending["stripe_session_id"] = session.get("id")
    pending["stripe_payment_intent"] = session.get("payment_intent")
    pending.pop("payment_review_required", None)
    pending.pop("payment_review_reason", None)
    save_pending_to_app(app, listing_id, pending)
    record_confirmed_payment(
        context_from_app(app),
        listing_id,
        pending,
        "stripe",
        amount_total,
        currency,
        stripe_session_id=session.get("id"),
        stripe_payment_intent=session.get("payment_intent"),
    )
    deferred_statuses = apply_deferred_stripe_status_events(
        context_from_app(app),
        listing_id,
        session.get("payment_intent"),
    )
    if deferred_statuses:
        logger.warning(
            "К Stripe-платежу применены ранее пришедшие события: listing_id=%s statuses=%s",
            listing_id,
            deferred_statuses,
        )
    # Сначала надёжно фиксируем факт оплаты. Даже если Railway перезапустится
    # сразу после ответа Stripe, recovery-loop найдёт заявку и продолжит доставку.
    await persist_now(app)

    if pending.get("payment_review_required"):
        try:
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    "⚠️ Оплата сопоставлена, но до завершения доставки уже был получен "
                    "возврат или спор. Автоматическая публикация остановлена.\n\n"
                    f"listing_id={listing_id}"
                ),
            )
        except Exception:
            pass
        return True

    if pending.get("submitted_to_admin"):
        logger.info(f"Stripe webhook: заявка уже отправлена listing_id={listing_id}")
        return True
    if public_limit_reached_for_app(app, user_id, exclude_listing_id=listing_id):
        logger.warning(f"Stripe webhook: оплата пришла сверх месячного лимита listing_id={listing_id}; продолжаю обработку")

    if background and hasattr(app, "create_task"):
        schedule_paid_delivery(app, listing_id)
        return True

    reply_target = BotReplyTarget(app.bot, user_id)
    await submit_paid_public_listing(
        context_from_app(app),
        listing_id,
        PaidUser(user_id),
        reply_target,
        test_mode=False,
        raise_on_delivery_failure=True,
    )
    await persist_now(app)
    return True


async def stripe_webhook(request):
    if not STRIPE_ENABLED:
        return web.Response(status=404, text="Stripe is not enabled")

    payload = await request.read()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return web.Response(status=400, text="Invalid payload")
    except Exception as e:
        logger.warning(f"Stripe webhook signature error: {e}")
        return web.Response(status=400, text="Invalid signature")

    event_id = event.get("id")
    known_events = request.app["telegram_app"].bot_data.get("processed_stripe_events", {})
    if event.get("type") != "checkout.session.completed" and event_id and event_id in known_events:
        return web.Response(text="ok")

    if event.get("type") == "checkout.session.completed":
        if event_id and event_id in known_events:
            session = event["data"]["object"]
            metadata = session.get("metadata") or {}
            duplicate_listing_id = metadata.get("listing_id") or session.get("client_reference_id")
            if duplicate_listing_id:
                schedule_paid_delivery(request.app["telegram_app"], duplicate_listing_id)
            return web.Response(text="ok")
        try:
            telegram_app = request.app["telegram_app"]
            handler = handle_stripe_checkout_completed(
                telegram_app,
                event["data"]["object"],
                background=True,
            )
            # В рабочем PTB-приложении обработка уходит в durable recovery
            # task и webhook отвечает быстро. Маленький fallback оставлен для
            # минимальных тестовых/локальных App-объектов без create_task.
            if hasattr(telegram_app, "create_task"):
                accepted = await handler
            else:
                accepted = await asyncio.wait_for(
                    handler,
                    timeout=STRIPE_FULFILLMENT_TIMEOUT_SECONDS,
                )
            if not accepted:
                return web.Response(status=500, text="payment was not accepted")
            remember_stripe_event(request.app["telegram_app"], event_id)
            await persist_now(request.app["telegram_app"])
        except asyncio.TimeoutError:
            logger.error(
                "Stripe webhook: Telegram не завершил доставку за %sс; Stripe повторит событие",
                STRIPE_FULFILLMENT_TIMEOUT_SECONDS,
            )
            return web.Response(status=500, text="temporary processing timeout")
        except Exception:
            logger.exception("Stripe webhook: необработанная ошибка checkout.session.completed")
            return web.Response(status=500, text="temporary processing error")

    if event.get("type") in ("charge.refunded", "charge.dispute.created", "charge.dispute.closed"):
        telegram_app = request.app["telegram_app"]
        context = context_from_app(telegram_app)
        stripe_object = event.get("data", {}).get("object", {})
        payment_intent = stripe_object.get("payment_intent")
        listing_id, record = find_payment_record_by_intent(context, payment_intent)
        event_type = event.get("type")
        if listing_id and record:
            payment_status = apply_stripe_status_to_payment(
                context,
                listing_id,
                event_type,
                stripe_object,
                event_id=event.get("id"),
            )
            # Сначала сохраняем финансовое изменение, и только затем помечаем
            # Stripe event обработанным. Иначе сбой диска мог скрыть возврат.
            await persist_now(telegram_app)
            remember_stripe_event(telegram_app, event.get("id"))
            await persist_now(telegram_app)
            current_pending = get_pending(context, listing_id)
            if (
                payment_status == "dispute_won"
                and isinstance(current_pending, dict)
                and current_pending.get("paid")
                and not current_pending.get("submitted_to_admin")
            ):
                schedule_paid_delivery(telegram_app, listing_id)
            try:
                await telegram_app.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        "⚠️ Изменился статус Stripe-платежа.\n\n"
                        f"listing_id={listing_id}\n"
                        f"событие={event_type}\n"
                        f"статус={payment_status}\n\n"
                        "Проверьте связанное объявление и решение по публикации."
                    ),
                )
            except Exception as notify_error:
                logger.warning(f"Не удалось уведомить администратора о Stripe status event: {notify_error}")
        else:
            event_id = event.get("id") or uuid.uuid4().hex
            try:
                amount = int(stripe_object.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            try:
                amount_refunded = int(stripe_object.get("amount_refunded") or 0)
            except (TypeError, ValueError):
                amount_refunded = 0
            telegram_app.bot_data[deferred_stripe_event_key(event_id)] = {
                "event_id": event_id,
                "event_type": event_type,
                "payment_intent": payment_intent,
                "id": stripe_object.get("id"),
                "charge": stripe_object.get("charge"),
                "status": stripe_object.get("status"),
                "amount": amount,
                "amount_refunded": amount_refunded,
                "refunded": bool(stripe_object.get("refunded")),
                "received_at": now_iso(),
            }
            await persist_now(telegram_app)
            remember_stripe_event(telegram_app, event.get("id"))
            await persist_now(telegram_app)
            logger.warning(
                "Stripe status event временно сохранён до появления payment record: type=%s payment_intent=%s",
                event_type,
                payment_intent,
            )
            try:
                await telegram_app.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        "⚠️ Stripe прислал возврат/спор раньше, чем удалось найти платёж. "
                        "Событие сохранено для последующего сопоставления.\n\n"
                        f"event_id={event_id}\n"
                        f"type={event_type}\n"
                        f"payment_intent={payment_intent}\n"
                        f"charge={stripe_object.get('charge') or stripe_object.get('id')}"
                    ),
                )
            except Exception:
                pass

    return web.Response(text="ok")


async def stripe_success_page(request):
    session_id = request.query.get("session_id", "").strip()
    page_heading = "Проверка оплаты"
    reconciliation_message = "Не удалось определить оплату. Вернитесь в Telegram и проверьте сообщения бота."
    if STRIPE_ENABLED and session_id.startswith("cs_") and len(session_id) <= 255:
        try:
            async with STRIPE_RECONCILIATION_SEMAPHORE:
                session = await asyncio.wait_for(
                    asyncio.to_thread(stripe.checkout.Session.retrieve, session_id),
                    timeout=STRIPE_API_TIMEOUT_SECONDS,
                )
            processed = await handle_stripe_checkout_completed(
                request.app["telegram_app"], session, background=True
            )
            session_metadata = session.get("metadata") or {}
            session_listing_id = session_metadata.get("listing_id") or session.get("client_reference_id")
            payment_record = (
                get_payment_record(context_from_app(request.app["telegram_app"]), session_listing_id)
                if session_listing_id else None
            )
            if payment_record and payment_record.get("payment_status") == "manual_review":
                page_heading = "Оплата получена"
                reconciliation_message = (
                    "Оплата получена и передана администратору на ручную проверку. "
                    "Повторно оплачивать не нужно."
                )
            elif processed:
                page_heading = "Оплата подтверждена"
                reconciliation_message = "Оплата подтверждена. Объявление принято в обработку и отправляется на проверку."
            elif session.get("payment_status") == "paid":
                page_heading = "Оплата получена"
                reconciliation_message = "Оплата получена. Проверьте сообщения бота: объявлению может требоваться исправление."
            else:
                reconciliation_message = "Оплата пока не подтверждена. Вернитесь в Telegram и при необходимости повторите оплату."
        except Exception:
            logger.exception(f"Stripe success reconciliation error session_id={session_id}")

    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Оплата Binio</title></head>"
            "<body style='font-family:Arial,sans-serif;padding:32px;line-height:1.45'>"
            f"<h2>{html.escape(page_heading)}</h2>"
            f"<p>{html.escape(reconciliation_message)}</p>"
            f"<p><a href='{bot_chat_url()}' style='font-size:18px'>Открыть бота</a></p>"
            "</body></html>"
        )
    )


async def health_page(request):
    return web.Response(text="Binio Partner Bot is running")


async def stripe_cancel_page(request):
    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Оплата отменена</title></head>"
            "<body style='font-family:Arial,sans-serif;padding:32px;line-height:1.45'>"
            "<h2>Оплата не завершена</h2>"
            "<p>Вы можете вернуться в бот и попробовать оплатить ещё раз.</p>"
            f"<p><a href='{bot_chat_url()}' style='font-size:18px'>Открыть бота</a></p>"
            "</body></html>"
        )
    )


async def start_stripe_webhook_server(app):
    global STRIPE_WEB_RUNNER
    if not STRIPE_ENABLED:
        if STRIPE_SECRET_KEY or STRIPE_WEBHOOK_SECRET or PUBLIC_BASE_URL:
            logger.warning(
                "Stripe настроен не полностью. Нужны STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PUBLIC_BASE_URL."
            )
        return

    web_app = web.Application()
    web_app["telegram_app"] = app
    web_app.router.add_get("/", health_page)
    web_app.router.add_get("/health", health_page)
    web_app.router.add_post("/stripe-webhook", stripe_webhook)
    web_app.router.add_get("/stripe-success", stripe_success_page)
    web_app.router.add_get("/stripe-cancel", stripe_cancel_page)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    STRIPE_WEB_RUNNER = runner
    logger.info(f"Stripe webhook server запущен на порту {WEB_PORT}")


async def stop_stripe_webhook_server(app):
    global STRIPE_WEB_RUNNER, STRIPE_RECOVERY_TASK, CHANNEL_FULL_SYNC_TASK
    channel_tasks = list(CHANNEL_SYNC_TASKS.values())
    for task in channel_tasks:
        task.cancel()
    if channel_tasks:
        await asyncio.gather(*channel_tasks, return_exceptions=True)
    CHANNEL_SYNC_TASKS.clear()
    if CHANNEL_FULL_SYNC_TASK is not None:
        CHANNEL_FULL_SYNC_TASK.cancel()
        await asyncio.gather(CHANNEL_FULL_SYNC_TASK, return_exceptions=True)
        CHANNEL_FULL_SYNC_TASK = None

    recovery_task = STRIPE_RECOVERY_TASK
    if recovery_task is not None:
        recovery_task.cancel()
        try:
            await recovery_task
        except asyncio.CancelledError:
            pass
        STRIPE_RECOVERY_TASK = None

    delivery_tasks = list(STRIPE_DELIVERY_TASKS.values())
    for task in delivery_tasks:
        task.cancel()
    if delivery_tasks:
        await asyncio.gather(*delivery_tasks, return_exceptions=True)
    STRIPE_DELIVERY_TASKS.clear()

    runner = STRIPE_WEB_RUNNER
    if runner:
        await runner.cleanup()
        STRIPE_WEB_RUNNER = None


def list_partner_published(context, partner_id, include_hidden=False):
    listings = []
    for key, value in context.application.bot_data.items():
        if not key.startswith("published_listing_") or not isinstance(value, dict):
            continue
        if value.get("hidden_from_list") and not include_hidden:
            continue
        if value.get("partner_id") == partner_id:
            listings.append(value)
    status_order = {"active": 0, "rented": 1, "removed": 2}
    listings.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    listings.sort(key=lambda item: (1 if item.get("channel_missing") else 0, status_order.get(item.get("status"), 9)))
    return listings


def normalize_published_filter(value):
    value = str(value or "all").strip().lower()
    return value if value in PUBLISHED_LISTING_FILTERS else "all"


def filter_published_listings(listings, filter_key):
    filter_key = normalize_published_filter(filter_key)
    if filter_key == "all":
        # «Основные» — только записи, которые действительно находятся в рабочем
        # списке. Снятые и исчезнувшие из канала записи всегда остаются в архиве,
        # даже если это старая запись без флага hidden_from_list.
        return [
            item for item in listings
            if not item.get("hidden_from_list")
            and not item.get("channel_missing")
            and item.get("status", "active") != "removed"
        ]
    if filter_key == "active":
        return [
            item for item in listings
            if not item.get("hidden_from_list")
            and not item.get("channel_missing")
            and item.get("status", "active") == "active"
        ]
    if filter_key == "rented":
        return [
            item for item in listings
            if not item.get("hidden_from_list")
            and not item.get("channel_missing")
            and item.get("status") == "rented"
        ]
    return [
        item for item in listings
        if item.get("hidden_from_list")
        or item.get("channel_missing")
        or item.get("status") == "removed"
    ]


def published_filter_counts(listings):
    return {
        key: len(filter_published_listings(listings, key))
        for key in PUBLISHED_LISTING_FILTERS
    }


def current_published_filter(context, user_id):
    return normalize_published_filter(
        context.application.bot_data.get(f"published_filter_{user_id}", "all")
    )


def current_filtered_published_listings(context, user_id):
    all_listings = list_partner_published(context, user_id, include_hidden=True)
    filter_key = current_published_filter(context, user_id)
    return filter_published_listings(all_listings, filter_key), filter_key


def cleanup_bot_memory(bot_data):
    """Удаляет только устаревшие временные данные. Опубликованные объявления и привязки партнёров не трогает."""
    removed = {
        "drafts": 0,
        "submitted": 0,
        "transient": 0,
    }

    draft_ttl = BOT_DRAFT_TTL_DAYS * 24 * 60 * 60
    submitted_ttl = BOT_SUBMITTED_TTL_DAYS * 24 * 60 * 60
    transient_ttl = BOT_TRANSIENT_TTL_DAYS * 24 * 60 * 60

    for key in list(bot_data.keys()):
        if not (key.startswith("pending_listing_") or key.startswith("pending_")):
            continue
        listing_id = listing_id_from_pending_key(key)
        if key.startswith("pending_") and not key.startswith("pending_listing_") and pending_key(listing_id) in bot_data:
            bot_data.pop(key, None)
            removed["drafts"] += 1
            continue
        pending = (
            bot_data.get(pending_key(listing_id))
            or bot_data.get(f"pending_{listing_id}")
        )
        if not isinstance(pending, dict):
            continue

        last_seen = pending.get("updated_at") or pending.get("created_at")
        changed = False
        if not last_seen:
            last_seen = now_iso()
            pending.setdefault("created_at", last_seen)
            pending["updated_at"] = last_seen
            changed = True

        for busy_field in ("submit_in_progress", "admin_action_in_progress", "invoice_in_progress"):
            if pending.get(busy_field) and not pending_busy(pending, busy_field, ttl_seconds=300):
                pending.pop(busy_field, None)
                changed = True

        if (
            pending.get("invoice_created_at")
            and not pending.get("paid")
            and not pending.get("submitted_to_admin")
            and not public_invoice_active(pending)
        ):
            pending.pop("invoice_created_at", None)
            changed = True

        deleted = False
        payment_record = bot_data.get(payment_key(listing_id))
        payment_under_review = bool(
            pending.get("payment_review_required")
            or (
                isinstance(payment_record, dict)
                and payment_record.get("payment_status") == "manual_review"
            )
        )
        confirmed_public_payment = bool(
            pending.get("source") == "public" and pending.get("paid")
        )
        if confirmed_public_payment or payment_under_review:
            # Любая подтверждённая оплата хранится до явного переноса в
            # опубликованную/историческую запись. Заявку ручной проверки тоже
            # нельзя удалять: без текста и фото платёж невозможно разрешить.
            pass
        elif pending.get("submitted_to_admin"):
            if is_older_than(last_seen, submitted_ttl):
                history = dict(pending)
                history["listing_id"] = str(listing_id)
                history["history_status"] = "expired_review"
                history["status"] = "expired_review"
                history["expired_review_at"] = now_iso()
                history["updated_at"] = history["expired_review_at"]
                bot_data[history_key(listing_id)] = history
                bot_data.pop(pending_key(listing_id), None)
                bot_data.pop(f"pending_{listing_id}", None)
                removed["submitted"] += 1
                deleted = True
        elif is_older_than(last_seen, draft_ttl):
            bot_data.pop(pending_key(listing_id), None)
            bot_data.pop(f"pending_{listing_id}", None)
            removed["drafts"] += 1
            deleted = True

        if changed and not deleted:
            bot_data[pending_key(listing_id)] = pending

    transient_prefixes = (
        "state_",
        "photos_",
        "property_type_",
        "flow_",
        "editing_listing_",
        "published_money_listing_",
        "published_money_field_",
        "published_filter_",
        "session_contact_",
        "session_partner_code_",
        "employee_choice_mode_",
        "public_gemini_usage_",
                "partner_gemini_usage_",
    )
    activity_keys = [key for key in bot_data if key.startswith("activity_updated_")]
    for activity_key in activity_keys:
        user_id = activity_key.replace("activity_updated_", "", 1)
        if not is_older_than(bot_data.get(activity_key), transient_ttl):
            continue
        for prefix in transient_prefixes:
            if bot_data.pop(f"{prefix}{user_id}", None) is not None:
                removed["transient"] += 1
        bot_data.pop(activity_key, None)
        removed["transient"] += 1

    admin_editing_listing_id = bot_data.get("admin_editing_listing_id")
    if admin_editing_listing_id and not (
        bot_data.get(pending_key(admin_editing_listing_id))
        or bot_data.get(f"pending_{admin_editing_listing_id}")
    ):
        clear_admin_edit_session(bot_data, admin_editing_listing_id)
        removed["transient"] += 1

    return removed


def recover_interrupted_states(bot_data):
    """На старте переводит оборванные внешние операции в безопасный ручной режим."""
    interrupted = {"review": [], "publish": []}
    seen = set()
    for key in list(bot_data.keys()):
        if not (key.startswith("pending_listing_") or key.startswith("pending_")):
            continue
        listing_id = listing_id_from_pending_key(key)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        pending = bot_data.get(pending_key(listing_id)) or bot_data.get(f"pending_{listing_id}")
        if not isinstance(pending, dict):
            continue
        changed = False
        if pending.get("review_state") == "sending":
            pending["review_state"] = "unknown"
            pending["review_error"] = "Процесс был перезапущен во время доставки"
            pending.pop("submit_in_progress", None)
            interrupted["review"].append(str(listing_id))
            changed = True
        if pending.get("publish_state") == "sending":
            pending["publish_state"] = "unknown"
            pending["publish_error"] = "Процесс был перезапущен во время публикации"
            pending.pop("admin_action_in_progress", None)
            interrupted["publish"].append(str(listing_id))
            changed = True
        if changed:
            pending["updated_at"] = now_iso()
            bot_data[pending_key(listing_id)] = pending
    return interrupted


def cleanup_summary_text(removed):
    total = sum(removed.values())
    if not total:
        return "ничего не удалено"
    return (
        f"черновики: {removed.get('drafts', 0)}, "
        f"старые заявки: {removed.get('submitted', 0)}, "
        f"временные шаги: {removed.get('transient', 0)}"
    )


def format_bytes(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "0 Б"
    units = ("Б", "КБ", "МБ", "ГБ")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def rough_pickle_size(value):
    try:
        return len(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        try:
            return len(repr(value).encode("utf-8", errors="ignore"))
        except Exception:
            return 0


def memory_category_for_key(key):
    if key.startswith("published_listing_"):
        return "Опубликованные объявления"
    if key.startswith("pending_listing_") or key.startswith("pending_"):
        return "Заявки и черновики"
    if key.startswith("photos_"):
        return "Временные фото-сессии"
    if key.startswith("partner_code_") or key.startswith("contact_"):
        return "Привязки партнёров"
    if key.startswith((
        "state_",
        "activity_updated_",
        "property_type_",
        "flow_",
        "editing_listing_",
        "published_money_listing_",
        "published_money_field_",
        "session_contact_",
        "session_partner_code_",
        "employee_choice_mode_",
        "public_gemini_usage_",
    )):
        return "Временные шаги пользователей"
    if key.startswith("admin_"):
        return "Админ-состояние"
    return "Другое"


def memory_breakdown(bot_data):
    categories = {}
    largest = []
    for key, value in bot_data.items():
        item_size = rough_pickle_size({key: value})
        category = memory_category_for_key(str(key))
        current = categories.setdefault(category, {"count": 0, "bytes": 0})
        current["count"] += 1
        current["bytes"] += item_size
        largest.append((item_size, str(key)))

    largest.sort(reverse=True, key=lambda item: item[0])
    return categories, largest[:10]


def safe_file_size(path):
    try:
        if os.path.exists(path):
            return os.path.getsize(path)
    except Exception:
        return None
    return None


def data_dir_usage(path):
    total = 0
    files = []
    try:
        if not os.path.isdir(path):
            return None, []
        for root, _, filenames in os.walk(path):
            for filename in filenames:
                full_path = os.path.join(root, filename)
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                total += size
                rel_path = os.path.relpath(full_path, path)
                files.append((size, rel_path))
    except Exception:
        return None, []

    files.sort(reverse=True, key=lambda item: item[0])
    return total, files[:8]


def channel_post_url(channel_username, message_id):
    username = str(channel_username).strip()
    if not username or not message_id:
        return None
    if username.startswith("@"):
        username = username[1:]
    if username.startswith("-"):
        return None
    return f"https://t.me/{username}/{message_id}"


def strip_listing_status(text):
    return re.sub(
        r'^\s*(?:<b>)?(?:(?:✅|🔴)\s*)?СДАНО(?:</b>)?\s*\n+',
        '',
        text,
        flags=re.I,
    ).strip()


def listing_with_status(text, status):
    base = strip_listing_status(text)
    if status == "rented":
        return f"<b>🔴 СДАНО</b>\n\n{base}"
    return base


def published_status_label(status, channel_missing=False, archived=False):
    if channel_missing:
        base = "⚫ Нет в канале"
    elif status == "rented":
        base = "🔴 Сдано"
    elif status == "removed":
        base = "⚪ Снято"
    else:
        base = "🟢 Активно"
    return f"🗄️ Архив · {base}" if archived else base


FINANCIAL_FIELDS = {
    "price": {
        "label": "цену",
        "line_label": "Аренда",
        "aliases": ["Арендная плата", "Стоимость аренды", "Аренда", "Цена"],
        "button": "💰 Цена",
    },
    "deposit": {
        "label": "залог",
        "line_label": "Залог",
        "aliases": ["Залог"],
        "button": "🔐 Залог",
    },
    "commission": {
        "label": "комиссию",
        "line_label": "Комиссия",
        "aliases": ["Комиссия агентства", "Комиссия"],
        "button": "🤝 Комиссия",
    },
}


def get_financial_field(field_key):
    return FINANCIAL_FIELDS.get(field_key)


def normalize_financial_value(value):
    value = re.sub(r'\s+', ' ', value.strip())
    if not value:
        return ""

    digits_only = re.sub(r'\D', '', value)
    if digits_only and re.fullmatch(r'[\d\s.,]+', value):
        amount = int(digits_only)
        return f"{amount:,}".replace(",", " ") + " Kč"

    if re.search(r'\d', value) and not re.search(r'\b(?:Kč|CZK|€|EUR|евро|крон)\b', value, flags=re.I):
        return value + " Kč"
    return value


def validate_financial_value(field_key, raw_value):
    """Возвращает безопасное значение и понятную ошибку для денежного поля."""
    field = get_financial_field(field_key)
    value = re.sub(r'\s+', ' ', str(raw_value or '').strip())
    if not field:
        return None, "Неизвестное финансовое поле."
    if not value:
        return None, f"Напишите новое значение для поля «{field['label']}»."
    if len(value) > 80:
        return None, "Значение слишком длинное. Укажите сумму или короткое условие (до 80 символов)."
    if '<' in value or '>' in value:
        return None, "HTML и служебная разметка в денежных полях запрещены."
    if re.search(r'(^|\s)[−–—-]\s*\d', value):
        return None, "Сумма не может быть отрицательной."

    digits = re.sub(r'\D', '', value)
    if digits:
        amount = int(digits)
        if amount <= 0:
            return None, "Сумма должна быть больше нуля."
        if amount > 100_000_000:
            return None, "Сумма выглядит слишком большой. Проверьте введённое значение."
    else:
        allowed_phrases = {
            "price": (r'по\s+договор[её]нности', r'по\s+запросу', r'уточняется'),
            "deposit": (r'без\s+залога', r'не\s+требуется', r'по\s+договор[её]нности'),
            "commission": (r'без\s+комиссии', r'не\s+требуется', r'по\s+договор[её]нности'),
        }
        if not any(re.fullmatch(pattern, value, flags=re.I) for pattern in allowed_phrases.get(field_key, ())):
            return None, "Укажите сумму цифрами или короткое допустимое условие."

    # Экранируем пользовательский ввод до вставки в Telegram HTML.
    return html.escape(normalize_financial_value(value)), None


def replace_financial_line(text, field_key, value):
    field = get_financial_field(field_key)
    if not field:
        return text

    aliases = "|".join(re.escape(alias) for alias in field["aliases"])
    pattern = re.compile(
        rf'^(\s*(?:[—-]\s*)?(?:<b>)?(?:{aliases})(?:</b>)?\s*[:\-–]\s*)(.*)$',
        flags=re.I | re.M,
    )

    if pattern.search(text):
        return pattern.sub(lambda match: f"{match.group(1)}{value}", text, count=1)

    new_line = f"— {field['line_label']}: {value}"
    finance_header = re.search(r'(?im)^\s*(?:<b>)?Финансовые условия:?(?:</b>)?\s*$', text)
    if finance_header:
        insert_at = finance_header.end()
        return text[:insert_at] + "\n" + new_line + text[insert_at:]

    contact_match = re.search(r'(?im)^\s*(?:<b>)?Контакт:?', text)
    if contact_match:
        return text[:contact_match.start()].rstrip() + "\n\n<b>Финансовые условия:</b>\n" + new_line + "\n\n" + text[contact_match.start():].lstrip()

    return text.rstrip() + "\n\n<b>Финансовые условия:</b>\n" + new_line


def published_channel_messages(item):
    messages = item.get("channel_messages")
    if isinstance(messages, list) and messages:
        return messages
    message_id = item.get("channel_message_id")
    if not message_id:
        return []
    return [{
        "chat_id": item.get("channel_chat_id") or CHANNEL_USERNAME,
        "message_id": message_id,
        "has_photos": item.get("has_photos", True),
    }]


def published_item_check_url(item):
    post_url = item.get("channel_post_url")
    if post_url:
        return post_url
    messages = published_channel_messages(item)
    if not messages:
        return None
    return channel_post_url(CHANNEL_USERNAME, messages[0].get("message_id"))


def telegram_post_preview_url(post_url):
    """Возвращает URL Telegram preview, где видно именно содержимое поста.

    Обычный GET ссылки t.me/<channel>/<id> может вернуть HTTP 200 даже для
    удалённого/несуществующего поста — Telegram в этом случае отдаёт главную
    страницу канала. Параметр embed=1 возвращает widget с data-post только
    для реально доступного сообщения.
    """
    if not post_url:
        return None
    separator = "&" if "?" in post_url else "?"
    return f"{post_url}{separator}embed=1"


def telegram_post_marker(post_url):
    match = re.match(r"^https://t\.me/([^/?#]+)/([0-9]+)", str(post_url or ""), re.IGNORECASE)
    if not match:
        return None
    return f'data-post="{match.group(1)}/{match.group(2)}"'.lower()


async def check_public_channel_post(session, item):
    """Безопасно проверяет публичную ссылку поста без изменения сообщения."""
    url = published_item_check_url(item)
    if not url or not url.startswith("https://t.me/"):
        return None
    preview_url = telegram_post_preview_url(url)
    expected_marker = telegram_post_marker(url)
    if not preview_url or not expected_marker:
        return None
    try:
        async with session.get(preview_url, allow_redirects=True) as response:
            body = (await response.text(errors="ignore")).lower()
            if response.status == 404:
                return False
            missing_markers = (
                "message not found",
                "this message is not available",
                "message was deleted",
                "post not found",
            )
            if any(marker in body for marker in missing_markers):
                return False
            if response.status >= 500:
                return None
            if response.status >= 400:
                return None
            # Отсутствие маркера при HTTP 200 неоднозначно: Telegram меняет
            # embed-разметку, применяет защиту/геоограничение и иногда отдаёт
            # общую страницу. Архивировать можно только по явному 404/маркеру.
            if expected_marker not in body:
                return None
            return True
    except Exception as error:
        logger.info(f"Проверка поста временно недоступна: {error}")
        return None


async def verify_published_items(context, items, archive_missing=False):
    """Проверяет переданные посты и сохраняет только достоверные результаты."""
    if not items or aiohttp is None:
        return 0, len(items)

    timeout = aiohttp.ClientTimeout(total=4)
    headers = {"User-Agent": "Binio Partner Bot channel check"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            results = await asyncio.gather(
                *(check_public_channel_post(session, item) for item in items),
                return_exceptions=False,
            )
    except Exception as error:
        logger.info(f"Проверка страницы канала временно недоступна: {error}")
        return 0, len(items)

    missing = 0
    unknown = 0
    checked_at = now_iso()
    for item, result in zip(items, results):
        if result is False:
            item["channel_missing"] = True
            item["channel_checked_at"] = checked_at
            item["channel_check_version"] = CHANNEL_CHECK_VERSION
            if archive_missing:
                item["hidden_from_list"] = True
                item["hidden_at"] = checked_at
                item["hidden_reason"] = "channel_missing"
            save_published(context, item["listing_id"], item)
            missing += 1
        elif result is True:
            was_channel_missing = bool(item.get("channel_missing"))
            item.pop("channel_missing", None)
            if item.get("hidden_reason") == "channel_missing" or was_channel_missing:
                item.pop("hidden_from_list", None)
                item.pop("hidden_at", None)
                item.pop("hidden_reason", None)
            item["channel_checked_at"] = checked_at
            item["channel_check_version"] = CHANNEL_CHECK_VERSION
            save_published(context, item["listing_id"], item)
        else:
            unknown += 1
    return missing, unknown


async def verify_published_page(context, listings, page, archive_missing=False):
    """Совместимость со старыми кнопками; новые списки проверяются автоматически."""
    start = page * PUBLISHED_LISTINGS_PAGE_SIZE
    page_items = listings[start:start + PUBLISHED_LISTINGS_PAGE_SIZE]
    return await verify_published_items(context, page_items, archive_missing=archive_missing)


def channel_check_age_seconds(item):
    checked_at = item.get("channel_checked_at")
    if not checked_at:
        return None
    try:
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - checked).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def channel_check_is_due(item):
    if item.get("channel_check_version") != CHANNEL_CHECK_VERSION:
        return True
    age = channel_check_age_seconds(item)
    return age is None or age >= CHANNEL_AUTO_SYNC_INTERVAL_SECONDS


async def auto_sync_user_channel(app, user_id):
    """Фоновая сверка небольшой пачки старых записей после открытия списка."""
    try:
        context = context_from_app(app)
        listings = [
            item for item in list_partner_published(context, user_id, include_hidden=True)
            if (
                not item.get("hidden_from_list")
                or item.get("hidden_reason") == "channel_missing"
                or item.get("channel_missing")
            )
        ]
        due = [item for item in listings if channel_check_is_due(item)]
        due.sort(key=lambda item: item.get("channel_checked_at") or "")
        due = due[:CHANNEL_AUTO_SYNC_MAX_ITEMS]
        if not due:
            return
        missing, unknown = await verify_published_items(context, due, archive_missing=True)
        await persist_now(app)
        if missing:
            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🧹 Список обновлён: {missing} старых объявлений больше нет в канале и они перемещены в архив.\n"
                        "История и статистика сохранены."
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Открыть актуальный список", callback_data="my_listings")]
                    ]),
                    disable_web_page_preview=True,
                )
            except Exception as error:
                logger.info("Не удалось отправить уведомление о синхронизации пользователю %s: %s", user_id, error)
        elif unknown:
            logger.info("Проверка канала для пользователя %s частично недоступна: unknown=%s", user_id, unknown)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Фоновая сверка канала не удалась: user_id=%s", user_id)
    finally:
        current = asyncio.current_task()
        if CHANNEL_SYNC_TASKS.get(user_id) is current:
            CHANNEL_SYNC_TASKS.pop(user_id, None)


def schedule_channel_auto_sync(app, user_id):
    existing = CHANNEL_SYNC_TASKS.get(user_id)
    if existing is not None and not existing.done():
        return existing
    creator = getattr(app, "create_task", None)
    if creator is None:
        return None
    task = creator(auto_sync_user_channel(app, user_id), name=f"channel-sync-user-{user_id}")
    CHANNEL_SYNC_TASKS[user_id] = task
    return task


async def full_channel_sync_job(app, admin_chat_id):
    global CHANNEL_FULL_SYNC_TASK
    try:
        context = context_from_app(app)
        items = [
            value for key, value in app.bot_data.items()
            if key.startswith("published_listing_")
            and isinstance(value, dict)
            and value.get("published_at")
            and (
                not value.get("hidden_from_list")
                or value.get("hidden_reason") == "channel_missing"
                or value.get("channel_missing")
            )
        ]
        total_missing = 0
        total_unknown = 0
        checked = 0
        for start in range(0, len(items), PUBLISHED_LISTINGS_PAGE_SIZE):
            chunk = items[start:start + PUBLISHED_LISTINGS_PAGE_SIZE]
            missing, unknown = await verify_published_items(context, chunk, archive_missing=True)
            total_missing += missing
            total_unknown += unknown
            checked += len(chunk)
        await persist_now(app)
        text = (
            "✅ Синхронизация канала завершена.\n\n"
            f"Проверено объявлений: {checked}\n"
            f"Перемещено в архив: {total_missing}\n"
            f"Временно не удалось проверить: {total_unknown}"
        )
        await app.bot.send_message(chat_id=admin_chat_id, text=text)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.exception("Полная сверка канала не удалась")
        try:
            await app.bot.send_message(
                chat_id=admin_chat_id,
                text=f"⚠️ Синхронизация канала не завершилась: {error}",
            )
        except Exception:
            pass
    finally:
        current = asyncio.current_task()
        if CHANNEL_FULL_SYNC_TASK is current:
            CHANNEL_FULL_SYNC_TASK = None


def schedule_full_channel_sync(app, admin_chat_id):
    global CHANNEL_FULL_SYNC_TASK
    if CHANNEL_FULL_SYNC_TASK is not None and not CHANNEL_FULL_SYNC_TASK.done():
        return CHANNEL_FULL_SYNC_TASK
    creator = getattr(app, "create_task", None)
    if creator is None:
        return None
    CHANNEL_FULL_SYNC_TASK = creator(
        full_channel_sync_job(app, admin_chat_id),
        name="channel-sync-full",
    )
    return CHANNEL_FULL_SYNC_TASK


async def edit_published_channel_posts(context, item, visible_listing):
    messages = published_channel_messages(item)
    if not messages:
        raise RuntimeError("не сохранён message_id поста в канале")

    for message in messages:
        chat_id = message.get("chat_id") or CHANNEL_USERNAME
        message_id = message.get("message_id")
        if not message_id:
            continue
        if message.get("has_photos", item.get("has_photos", True)):
            caption = visible_listing
            if len(caption) > TELEGRAM_CAPTION_LIMIT:
                raise ValueError(
                    f"обновлённый текст длиннее лимита подписи Telegram: {len(caption)} > {TELEGRAM_CAPTION_LIMIT}"
                )
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=visible_listing,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )


def published_list_keyboard(listings, page=0, filter_key="all", counts=None):
    filter_key = normalize_published_filter(filter_key)
    counts = counts or published_filter_counts(listings)
    total_pages = max(1, (len(listings) + PUBLISHED_LISTINGS_PAGE_SIZE - 1) // PUBLISHED_LISTINGS_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    start = page * PUBLISHED_LISTINGS_PAGE_SIZE
    page_items = listings[start:start + PUBLISHED_LISTINGS_PAGE_SIZE]
    rows = []
    for item in page_items:
        headline = listing_headline(item.get("listing", "Объявление"))
        if len(headline) > 34:
            headline = headline[:31].rstrip() + "..."
        status_label = published_status_label(
            item.get('status'),
            item.get('channel_missing'),
            archived=(filter_key == 'archive' or item.get('hidden_from_list')),
        )
        rows.append([
            InlineKeyboardButton(
                f"{status_label} · {headline}",
                callback_data=f"pub_view_{item['listing_id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            f"{('✅ ' if filter_key == 'all' else '')}Основные · {counts['all']}",
            callback_data="my_listings_filter_all",
        ),
        InlineKeyboardButton(
            f"{('✅ ' if filter_key == 'active' else '')}Активные · {counts['active']}",
            callback_data="my_listings_filter_active",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            f"{('✅ ' if filter_key == 'rented' else '')}Сданные · {counts['rented']}",
            callback_data="my_listings_filter_rented",
        ),
        InlineKeyboardButton(
            f"{('✅ ' if filter_key == 'archive' else '')}Архив · {counts['archive']}",
            callback_data="my_listings_filter_archive",
        ),
    ])
    navigation = []
    if total_pages > 3 and page > 0:
        navigation.append(InlineKeyboardButton(
            "⏮️",
            callback_data=f"my_listings_page_{filter_key}_0",
        ))
    if page > 0:
        navigation.append(InlineKeyboardButton(
            "⬅️ Назад",
            callback_data=f"my_listings_page_{filter_key}_{page - 1}",
        ))
    if page < total_pages - 1:
        navigation.append(InlineKeyboardButton(
            "Далее ➡️",
            callback_data=f"my_listings_page_{filter_key}_{page + 1}",
        ))
    if total_pages > 3 and page < total_pages - 1:
        navigation.append(InlineKeyboardButton(
            "⏭️",
            callback_data=f"my_listings_page_{filter_key}_{total_pages - 1}",
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("➕ Новая публикация", callback_data="my_listings_new")])
    return InlineKeyboardMarkup(rows)


def published_page_for_listing(listings, listing_id):
    for index, item in enumerate(listings):
        if item.get("listing_id") == listing_id:
            return index // PUBLISHED_LISTINGS_PAGE_SIZE
    return 0


def published_manage_keyboard(item, list_page=0, filter_key="all"):
    listing_id = item["listing_id"]
    rows = []
    system_archived = bool(item.get("channel_missing") or item.get("status") == "removed")
    if item.get("hidden_from_list") and not system_archived:
        rows.append([
            InlineKeyboardButton("↩️ Вернуть в список", callback_data=f"pub_unarchive_{listing_id}")
        ])
    elif system_archived:
        rows.append([
            InlineKeyboardButton("ℹ️ Почему в архиве", callback_data=f"pub_archive_info_{listing_id}")
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "🗄️ В архив",
                callback_data=f"pub_archive_{listing_id}",
            )
        ])

    if not item.get("channel_missing") and item.get("status") != "removed":
        if item.get("status") == "rented":
            rows.append([InlineKeyboardButton("↩️ Вернуть в активные", callback_data=f"pub_active_{listing_id}")])
        else:
            rows.append([InlineKeyboardButton("🔴 Отметить как сдано", callback_data=f"pub_rented_{listing_id}")])
            rows.append([
                InlineKeyboardButton(FINANCIAL_FIELDS["price"]["button"], callback_data=f"pub_money_price_{listing_id}"),
                InlineKeyboardButton(FINANCIAL_FIELDS["deposit"]["button"], callback_data=f"pub_money_deposit_{listing_id}"),
            ])
            rows.append([InlineKeyboardButton(FINANCIAL_FIELDS["commission"]["button"], callback_data=f"pub_money_commission_{listing_id}")])
    filter_key = normalize_published_filter(filter_key)
    rows.append([
        InlineKeyboardButton(
            "📋 К списку",
            callback_data=f"my_listings_page_{filter_key}_{list_page}",
        )
    ])
    return InlineKeyboardMarkup(rows)


def format_partner_for_admin(user):
    """Если username нет, админ увидит имя как кликабельную ссылку tg://user?id=... ."""
    if user.username:
        return f"@{html.escape(user.username)}"
    display_name = user.full_name or user.first_name or f"Партнёр {user.id}"
    display_name = html.escape(display_name)
    return f'<a href="tg://user?id={user.id}">{display_name}</a> <code>ID {user.id}</code>'


def user_contact_url(user):
    if user.username:
        return f"https://t.me/{user.username}"
    return f"tg://user?id={user.id}"


def is_partner_contact_url(contact_url):
    return contact_url == DEFAULT_CONTACT or contact_url in set(EMPLOYEES.values())


def canonical_employee_code(employee_code):
    code = str(employee_code or "").strip().lower()
    return EMPLOYEE_CODE_ALIASES.get(code, code)


def has_partner_access(context, user_id):
    if str(user_id) in REVOKED_PARTNER_IDS:
        return False
    partner_code = canonical_employee_code(
        context.application.bot_data.get(f"partner_code_{user_id}")
    )
    contact_url = context.application.bot_data.get(f"contact_{user_id}", "")
    return partner_code in EMPLOYEES or is_partner_contact_url(contact_url)


def employee_display_name(employee_key):
    return EMPLOYEE_NAMES.get(employee_key, "Binio")


def employee_key_by_contact(contact_url):
    for key in EMPLOYEE_CHOICE_KEYS:
        if EMPLOYEES.get(key) == contact_url:
            return key
    for key, url in EMPLOYEES.items():
        if url == contact_url:
            return key
    return None


def employee_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(employee_display_name(key), callback_data=f"employee_{key}")]
        for key in EMPLOYEE_CHOICE_KEYS
    ])


async def ask_employee_choice(message, context, user_id, mode="start_partner"):
    context.application.bot_data[f"employee_choice_mode_{user_id}"] = mode
    set_state(context, user_id, "choosing_employee")

    if mode == "change_employee":
        text = (
            "Выберите сотрудника, чей контакт будет указан в ваших следующих объявлениях.\n\n"
            "Уже опубликованные объявления от этого не изменятся"
        )
    else:
        text = (
            "Партнёрский доступ активируется через сотрудника Binio.\n\n"
            "Выберите сотрудника, чей контакт должен быть указан в объявлениях"
        )

    await message.reply_text(text, reply_markup=employee_choice_keyboard())
    return False


def update_current_pending_contact(context, user_id, contact_url):
    listing_id = context.application.bot_data.get(f"editing_listing_{user_id}")
    pending = get_pending(context, listing_id) if listing_id else None
    if not pending or pending.get("partner_id") != user_id or pending.get("submitted_to_admin"):
        return False
    if pending.get("source", "partner") != "partner":
        return False

    previous_contact = pending.get("contact_url", DEFAULT_CONTACT)
    listing = pending.get("formatted_listing", "")
    listing = remove_contact_from_listing(listing, previous_contact)
    pending["formatted_listing"] = ensure_contact_line(listing, contact_url)
    if pending.get("editable_listing"):
        editable = remove_contact_from_listing(pending["editable_listing"], previous_contact)
        pending["editable_listing"] = ensure_contact_line(editable, contact_url)
    pending["contact_url"] = contact_url
    save_pending(context, listing_id, pending)
    return True


def public_payment_payload(listing_id):
    return f"public_listing:{listing_id}"


def listing_id_from_payment_payload(payload):
    if not payload.startswith("public_listing:"):
        return None
    return payload.split(":", 1)[1]


def make_contact_line(contact_url):
    safe_url = html.escape(contact_url, quote=True)
    return f'<b>Контакт:</b> <a href="{safe_url}">автор</a>'


def strip_html_tags_keep_text(text):
    """Превращает HTML-подпись в обычный текст и сохраняет URL из ссылок."""
    def replace_link(match):
        url = match.group(1)
        label = re.sub(r'<[^>]+>', '', match.group(2))
        return f"{label} ({url})"

    text = re.sub(r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', replace_link, text, flags=re.I | re.S)
    text = re.sub(r'</?b>', '', text, flags=re.I)
    text = re.sub(r'</?code>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def convert_markdown_bold_to_html(text):
    """Telegram отправляет объявления как HTML, поэтому markdown **...** убираем заранее."""
    if not text or "**" not in text:
        return text

    def replace_bold(match):
        inner = match.group(1).strip()
        if not inner:
            return ""
        return f"<b>{html.escape(inner, quote=False)}</b>"

    text = re.sub(r'\*\*([^*\n][\s\S]*?[^*\n])\*\*', replace_bold, text)
    text = text.replace("**", "")
    return text


def sanitize_gemini_listing_output(text):
    """Оставляет только объявление, если Gemini добавил служебное пояснение."""
    value = str(text or "").strip()
    if not value:
        return ""

    value = re.sub(r'^\s*```(?:html|markdown|text)?\s*', '', value, flags=re.I)
    value = re.sub(r'\s*```\s*$', '', value, flags=re.I)
    lines = value.splitlines()

    # Частый ответ: «Вот ваше объявление...», затем --- и само объявление.
    for index, line in enumerate(lines[:6]):
        if re.fullmatch(r'\s*[-—_=]{3,}\s*', line):
            prefix = " ".join(lines[:index]).lower()
            if any(word in prefix for word in ("объявлен", "текст", "вариант", "символ")):
                lines = lines[index + 1:]
            break

    def is_meta_line(line):
        compact = line.strip()
        lowered = compact.lower()
        if not compact or re.fullmatch(r'[-—_=]{3,}', compact):
            return True
        if re.match(r'^(?:конечно[,.!]?\s*)?(?:вот|ниже)\b', lowered) and any(
            word in lowered for word in ("объявлен", "текст", "вариант", "символ")
        ):
            return True
        return bool(re.fullmatch(r'(?:готово|результат|готовый вариант)\s*[:.!]?', lowered))

    while lines and is_meta_line(lines[0]):
        lines.pop(0)
    while lines and (not lines[-1].strip() or re.fullmatch(r'\s*```\s*', lines[-1])):
        lines.pop()

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return convert_markdown_bold_to_html(cleaned)


def remove_contact_from_listing(text, contact_url):
    exact = make_contact_line(contact_url)
    text = text.replace(exact, "")
    text = re.sub(r'\n?\s*<b>Контакт:</b>\s*<a\s+href=["\'][^"\']+["\']>автор</a>\s*', '\n', text, flags=re.I)
    if contact_url:
        text = re.sub(
            r'(?im)^[^\n]*' + re.escape(contact_url) + r'[^\n]*(?:\n|$)',
            '\n',
            text,
        )
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def insert_contact_before_hashtags(text, contact_line):
    """Ставит контакт перед финальной строкой хештегов, а не после неё."""
    value = text.strip()
    lines = value.splitlines()
    if lines and re.fullmatch(
        r'\s*(?:#[A-Za-zА-Яа-я0-9_+-]+\s*)+',
        lines[-1],
    ):
        tags = lines[-1].strip()
        body = "\n".join(lines[:-1]).rstrip()
        return f"{body}\n\n{contact_line}\n\n{tags}" if body else f"{contact_line}\n\n{tags}"
    return value.rstrip() + "\n\n" + contact_line


def ensure_contact_line(text, contact_url):
    """Гарантирует одну каноническую строку контакта перед публикацией."""
    contact_line = make_contact_line(contact_url)
    text = str(text or '').replace("[[CONTACT]]", "")
    # Даже если ссылка случайно встретилась в описании, публикация должна иметь
    # ровно одну каноническую кликабельную строку контакта.
    cleaned = remove_contact_from_listing(text, contact_url)
    cleaned = re.sub(
        r'\n?\s*(?:<b>)?Контакт:(?:</b>)?.*(?=\n|$)',
        '\n',
        cleaned,
        flags=re.I,
    ).strip()
    return insert_contact_before_hashtags(cleaned, contact_line)


def normalize_russian_headline(text):
    """Исправляет частый транслит Gemini в первой строке объявления."""
    replacements = {
        "kvartira": "Квартира",
        "apartman": "Квартира",
        "apartment": "Квартира",
        "komnata": "Комната",
        "pokoj": "Комната",
        "room": "Комната",
        "dum": "Дом",
        "dům": "Дом",
        "house": "Дом",
        "pozemek": "Участок",
        "land": "Участок",
    }

    lines = text.splitlines()
    if not lines:
        return text

    headline = lines[0]
    prefix = ""
    suffix = ""
    inner = headline.strip()

    bold_match = re.fullmatch(r'\s*<b>(.*?)</b>\s*', headline, flags=re.I | re.S)
    if bold_match:
        prefix = "<b>"
        suffix = "</b>"
        inner = bold_match.group(1).strip()

    for bad, good in replacements.items():
        pattern = re.compile(rf'^{re.escape(bad)}(?=[\s,.:;\-–]|$)', flags=re.I)
        if pattern.search(inner):
            inner = pattern.sub(good, inner, count=1)
            lines[0] = f"{prefix}{inner}{suffix}"
            return "\n".join(lines)

    return text


def normalize_listing_visual_format(text):
    """Возвращает базовое красивое оформление после ручной правки.

    В редакторе пользователь получает plain text без HTML. Если он отправит
    этот текст обратно, заголовок и названия разделов всё равно должны остаться
    визуально такими же, как в готовом варианте Gemini.
    """
    lines = text.splitlines()
    first_content_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_content_index is not None:
        headline = lines[first_content_index].strip()
        if not re.fullmatch(r'<b>.*</b>', headline, flags=re.I | re.S):
            lines[first_content_index] = f"<b>{headline}</b>"

    section_names = {
        "локация:": "Локация:",
        "финансовые условия:": "Финансовые условия:",
    }
    for index, line in enumerate(lines):
        compact = strip_html_tags_keep_text(line).strip()
        canonical = section_names.get(compact.lower())
        if canonical:
            lines[index] = f"<b>{canonical}</b>"

    return "\n".join(lines)


def listing_type_hashtag(text):
    text_without_hashtags = re.sub(r'#[A-Za-zА-Яа-я0-9_+-]+', '', text)
    plain = strip_html_tags_keep_text(text_without_hashtags).lower().replace("кк", "kk")
    headline = listing_headline(text_without_hashtags).lower().replace("кк", "kk")

    layout_plus = re.search(r'\b([1-5])\s*\+\s*1\b', headline) or re.search(r'\b([1-5])\s*\+\s*1\b', plain)
    if layout_plus:
        return f"#{layout_plus.group(1)}plus1"

    layout_kk = re.search(r'\b([1-5])\s*\+?\s*kk\b', headline) or re.search(r'\b([1-5])\s*\+?\s*kk\b', plain)
    if layout_kk:
        return f"#{layout_kk.group(1)}kk"

    if re.search(r'\b(комната|непроходная комната|проходная комната|pokoj|room)\b', plain):
        return "#pokoj"
    if re.search(r'\b(дом|dům|dum|house)\b', plain):
        return "#dum"
    if re.search(r'\b(участок|pozemek|land)\b', plain):
        return "#pozemek"
    if re.search(r'(коммерческ|коммерц|нежил|офис|магазин|салон|склад|komerce)', plain):
        return "#komerce"
    return None


def prague_area_hashtag(text):
    match = re.search(r'\bPra(?:ha|gue)\s*([1-9]|10)\b|\bПрага\s*([1-9]|10)\b', text, flags=re.I)
    if not match:
        return None
    return f"#Praha{match.group(1) or match.group(2)}"


def normalize_listing_hashtags(text):
    tags = []
    type_tag = listing_type_hashtag(text)
    area_tag = prague_area_hashtag(text)
    if type_tag:
        tags.append(type_tag)
    if area_tag and area_tag.lower() not in {tag.lower() for tag in tags}:
        tags.append(area_tag)
    tags.append("#pronajem")

    without_tags = re.sub(r'(?m)^\s*(?:#[A-Za-zА-Яа-я0-9_+-]+\s*)+\s*$', '', text)
    without_tags = re.sub(r'#[A-Za-zА-Яа-я0-9_+-]+', '', without_tags)
    without_tags = re.sub(r'[ \t]+\n', '\n', without_tags).strip()
    return without_tags.rstrip() + "\n\n" + " ".join(tags[:3])


def listing_headline(text):
    plain = strip_html_tags_keep_text(text)
    return next((line.strip() for line in plain.splitlines() if line.strip()), "")


def extract_financial_value(text, labels):
    plain = strip_html_tags_keep_text(text)
    aliases = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf'(?im)^\s*[—-]?\s*(?:{aliases})\s*[:\-–]\s*(.+?)\s*$',
        plain,
    )
    return match.group(1).strip() if match else None


def listing_financial_summary(text):
    parts = []
    price = extract_financial_value(text, ["Арендная плата", "Стоимость аренды", "Аренда", "Цена"])
    deposit = extract_financial_value(text, ["Возвратный залог", "Залог"])
    commission = extract_financial_value(text, ["Комиссия агентства", "Комиссия"])

    if price:
        parts.append(f"Цена: {price}")
    if deposit:
        parts.append(f"Залог: {deposit}")
    if commission:
        parts.append(f"Комиссия: {commission}")

    return "\n".join(parts)


def published_card_text(item):
    headline = listing_headline(item.get("listing", "Объявление")) or "Объявление"
    status = published_status_label(
        item.get("status"),
        item.get("channel_missing"),
        archived=bool(item.get("hidden_from_list") or item.get("channel_missing") or item.get("status") == "removed"),
    )
    financial_summary = listing_financial_summary(item.get("listing", ""))
    post_url = None if item.get("channel_missing") else item.get("channel_post_url")

    text = f"{status}\n\n<b>{html.escape(headline)}</b>"
    if financial_summary:
        text += f"\n\n{html.escape(financial_summary)}"
    if post_url:
        text += f'\n\n<a href="{html.escape(post_url, quote=True)}">Открыть пост в канале</a>'
    return text


def listing_has_price(text):
    # Валюта в строке залога/комиссии не считается ценой аренды.
    return bool(re.search(
        r'^\s*(?:[—-]\s*)?(?:<b>)?'
        r'(?:Арендная\s+плата|Стоимость\s+аренды|Аренда|Цена)(?:</b>)?\s*[:\-–]\s*'
        r'(?:\d[\d\s.,]*(?:Kč|CZK|EUR|€|крон|korun)|'
        r'(?:цена\s+)?по\s+(?:договор[её]нности|запросу)|уточняется|'
        r'info\s*(?:v|u)?\s*(?:rk|realit))',
        text,
        flags=re.I | re.M,
    ))


def headline_matches_property_type(headline, property_type_key):
    # Gemini и ручная правка могут вернуть одинаковый жирный заголовок как
    # Telegram HTML (<b>...</b>) или как Markdown (**...**). Сначала приводим
    # Markdown к HTML, иначе строка визуально начинается с «Квартира», но для
    # строгой проверки фактически начинается со звёздочек и ложно отклоняется.
    normalized = normalize_russian_headline(convert_markdown_bold_to_html(headline))
    plain = strip_html_tags_keep_text(normalized).strip().lower()

    if property_type_key == "apartment":
        return plain.startswith("квартира")
    if property_type_key == "room":
        return (
            plain.startswith("комната")
            or plain.startswith("непроходная комната")
            or plain.startswith("проходная комната")
        ) and not plain.startswith("квартира")
    if property_type_key == "house":
        return plain.startswith("дом")
    if property_type_key == "land":
        return plain.startswith("участок")
    if property_type_key == "commercial":
        return (
            plain.startswith("коммерческое помещение")
            or plain.startswith("коммерция")
            or plain.startswith("офис")
            or plain.startswith("магазин")
            or plain.startswith("салон")
            or plain.startswith("склад")
            or plain.startswith("помещение")
        ) and not (plain.startswith("квартира") or plain.startswith("комната") or plain.startswith("дом"))
    if property_type_key == "non_residential":
        return (
            plain.startswith("нежилое помещение")
            or plain.startswith("помещение")
            or plain.startswith("склад")
            or plain.startswith("гараж")
        ) and not (plain.startswith("квартира") or plain.startswith("комната") or plain.startswith("дом"))
    return bool(plain)


def validate_listing_ready(pending, listing):
    issues = []
    contact_url = pending.get('contact_url', DEFAULT_CONTACT)
    property_type_key = pending.get('property_type')
    headline = listing_headline(listing)

    if len(listing) > TELEGRAM_CAPTION_LIMIT:
        issues.append(
            f"сократите текст до {TELEGRAM_CAPTION_LIMIT} символов (сейчас {len(listing)})"
        )

    if property_type_key not in PROPERTY_TYPES:
        issues.append("выберите тип недвижимости")
    elif not headline_matches_property_type(headline, property_type_key):
        label = get_property_type(property_type_key)["button"]
        issues.append(f"заголовок не похож на выбранный тип «{label}»")

    if make_contact_line(contact_url) not in listing:
        issues.append("не найден контакт автора")
    if not listing_has_price(listing):
        issues.append("добавьте цену аренды")

    return issues


def listing_fix_keyboard(listing_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Улучшить текст", callback_data=f"regen_{listing_id}")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}")],
    ])


def admin_fix_keyboard(listing_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Исправить", callback_data=f"edit_more_{listing_id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{listing_id}")],
    ])


async def update_admin_action_message(query, text):
    try:
        await query.edit_message_text(text=text)
        return
    except Exception:
        pass
    try:
        await query.edit_message_caption(caption=text)
        return
    except Exception:
        pass
    try:
        await query.message.reply_text(text)
    except Exception as e:
        logger.warning(f"Не удалось обновить админское сообщение: {e}")


def validation_message(issues):
    lines = "\n".join(f"— {issue}" for issue in issues)
    return (
        "Перед отправкой нужно немного поправить объявление:\n"
        f"{lines}\n\n"
        "Можно нажать «Улучшить текст» или исправить описание вручную"
    )


def close_known_html_tags(cut):
    if cut.count("<b>") > cut.count("</b>"):
        cut += "</b>"
    if len(re.findall(r'<a\s+href=', cut)) > cut.count("</a>"):
        cut += "</a>"
    if cut.count("<code>") > cut.count("</code>"):
        cut += "</code>"
    return cut


def remove_incomplete_html_tag(cut):
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]
    return cut


def truncate(text, max_len=1000):
    """Обрезает HTML-подпись так, чтобы не резать теги в финальной строке."""
    if len(text) <= max_len:
        return text

    ellipsis = "…"
    cut_len = max(0, max_len - 20)
    while cut_len > 0:
        cut = remove_incomplete_html_tag(text[:cut_len]).rstrip()
        result = close_known_html_tags(cut) + ellipsis
        if len(result) <= max_len:
            return result
        cut_len -= 5

    return ellipsis


def safe_plain_caption(html_caption, limit=1024):
    plain = strip_html_tags_keep_text(html_caption)
    return truncate(plain, limit)


def safe_plain_text(html_text):
    return strip_html_tags_keep_text(html_text)


def fit_to_caption(text, limit=TELEGRAM_CAPTION_LIMIT):
    """Ужимает текст под лимит подписи к фото. Сначала убирает необязательную строку
    "Рядом: ...", затем безопасно обрезает остальное."""
    if len(text) <= limit:
        return text

    trimmed = re.sub(r'\n—\s*Рядом:.*?(?=\n|$)', '', text)
    if len(trimmed) <= limit:
        return trimmed

    return truncate(trimmed, limit)


def build_media_group(photos, caption, caption_index=0, parse_mode="HTML"):
    return [
        InputMediaPhoto(media=p, caption=caption, parse_mode=parse_mode) if i == caption_index else InputMediaPhoto(media=p)
        for i, p in enumerate(photos)
    ]


def split_plain_text(text, limit=4096):
    """Делит длинный plain text на сообщения Telegram без потери содержимого."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = text
    while len(current) > limit:
        split_at = current.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = current.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = current.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(current[:split_at].strip())
        current = current[split_at:].strip()
    if current:
        chunks.append(current)
    return chunks


async def send_with_retry(coro_factory, retries=2, delay=0.8, label="", retry_ambiguous=True):
    """Пытается выполнить отправку в Telegram до `retries` раз — короткие сетевые
    сбои (Timed out и т.п.) не должны сразу проваливать всю операцию.
    coro_factory — функция без аргументов, возвращающая новую корутину на каждый вызов."""
    last_error = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            if not is_transient_network_error(e):
                raise
            # RetryAfter означает, что Telegram явно не выполнил запрос.
            # TimedOut/ReadError неоднозначны: сообщение могло быть принято,
            # а потерялся только ответ. Для публикации в канал повтор запрещён.
            if not retry_ambiguous and not isinstance(e, RetryAfter):
                raise
            last_error = e
            logger.warning(f"{label} попытка {attempt + 1}/{retries} не удалась: {e}")
            if attempt < retries - 1:
                retry_delay = delay
                if isinstance(e, RetryAfter):
                    retry_delay = min(30.0, float(e.retry_after) + 0.25)
                await asyncio.sleep(retry_delay)
    raise last_error


async def shorten_listing_if_needed(text, limit=LISTING_SOFT_LIMIT, timeout=None):
    """Один раз просит Gemini сжать объявление в пределах общего бюджета."""
    if len(text) <= limit:
        return text

    if gemini_client is None:
        logger.warning("Gemini недоступен для сжатия объявления")
        return fit_to_caption(text)

    prompt = SHORTEN_TEMPLATE.format(limit=limit, text=text)
    import time

    timeout = max(1.0, float(timeout or GEMINI_SHORTEN_TIMEOUT_SECONDS))
    attempt_start = time.monotonic()
    try:
        async with GEMINI_SEMAPHORE:
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=GEMINI_GENERATION_CONFIG,
                ),
                timeout=timeout,
            )
        shortened = sanitize_gemini_listing_output(response.text)
        elapsed = time.monotonic() - attempt_start
        if not shortened:
            raise RuntimeError("Gemini вернул пустой текст после очистки")
        logger.info(
            f"Gemini сжатие: успех за {elapsed:.1f}с, "
            f"{len(text)} → {len(shortened)} символов"
        )
        if len(shortened) <= TELEGRAM_CAPTION_LIMIT:
            return shortened
        return fit_to_caption(shortened)
    except Exception as e:
        elapsed = time.monotonic() - attempt_start
        logger.warning(f"Gemini сжатие: ошибка за {elapsed:.1f}с — {e}")
        return fit_to_caption(text)


def prepare_listing_for_editing(text, contact_url):
    """Очищает хороший вариант Gemini, не сокращая и не переписывая его."""
    prepared = sanitize_gemini_listing_output(text)
    prepared = normalize_listing_visual_format(prepared)
    prepared = normalize_listing_hashtags(normalize_russian_headline(prepared))
    return ensure_contact_line(prepared, contact_url)


async def prepare_listing_for_caption(text, contact_url, allow_gemini_shortening=True):
    """Финальная подготовка объявления к подписи под фото.

    Здесь не меняется сценарий: мы только гарантируем контакт и размер подписи,
    чтобы Telegram принял фото вместе с текстом.
    """
    prepared = prepare_listing_for_editing(text, contact_url)
    # Текст, который уже помещается в подпись Telegram, нельзя переписывать
    # повторно. Мягкий ориентир из промпта не является причиной портить
    # хороший готовый вариант дополнительным запросом к Gemini.
    if len(prepared) <= TELEGRAM_CAPTION_LIMIT:
        return prepared

    if allow_gemini_shortening:
        prepared = await shorten_listing_if_needed(prepared)
        prepared = prepare_listing_for_editing(prepared, contact_url)

    if len(prepared) <= TELEGRAM_CAPTION_LIMIT:
        return prepared

    if not allow_gemini_shortening:
        # Ручной или администраторский текст нельзя обрезать без согласия.
        # Вызывающая ветка покажет точное превышение через validate_listing_ready.
        return prepared

    contact_line = make_contact_line(contact_url)
    body = remove_contact_from_listing(prepared, contact_url)
    body_limit = max(100, TELEGRAM_CAPTION_LIMIT - len(contact_line) - 2)
    body = fit_to_caption(body, body_limit).rstrip()
    return f"{body}\n\n{contact_line}"


def prepare_listing_without_gemini_shortening(text, contact_url):
    """Быстрая страховка лимита подписи, когда общий бюджет Gemini исчерпан."""
    prepared = prepare_listing_for_editing(text, contact_url)
    if len(prepared) <= TELEGRAM_CAPTION_LIMIT:
        return prepared

    contact_line = make_contact_line(contact_url)
    body = remove_contact_from_listing(prepared, contact_url)
    body_limit = max(100, TELEGRAM_CAPTION_LIMIT - len(contact_line) - 2)
    body = fit_to_caption(body, body_limit).rstrip()
    return f"{body}\n\n{contact_line}"


async def update_processing_status(message, text, reply_markup=None):
    """Плавно обновляет одно служебное сообщение вместо отправки новых."""
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        # Fallback нужен для старых/тестовых объектов Message, которые нельзя
        # редактировать. Ошибка статуса не должна останавливать объявление.
        try:
            await message.reply_text(text, reply_markup=reply_markup)
        except Exception as status_error:
            logger.warning(f"Не удалось обновить статус обработки: {status_error}")


async def generate_formatted_listing(
    raw_text,
    property_type_key,
    contact_url,
    status_message=None,
    return_editable=False,
):
    property_type = get_property_type(property_type_key)
    prompt = LISTING_TEMPLATE.format(
        text=raw_text,
        property_type_label=property_type["label"],
        property_type_rules=property_type["rules"],
        description_length_rules=listing_description_length_rules(raw_text),
    )

    import time

    formatted_listing = None
    last_error = None
    slow_notice_task = None
    overall_started = time.monotonic()

    async def notify_if_slow():
        await asyncio.sleep(GEMINI_SLOW_NOTICE_SECONDS)
        await update_processing_status(
            status_message,
            "✨ Улучшаю текст объявления…\n\nОбычно это занимает 15–25 секунд",
        )
        await asyncio.sleep(GEMINI_SLOW_NOTICE_SECONDS)
        await update_processing_status(
            status_message,
            "🔎 Проверяю форматирование и важные детали…",
        )

    if status_message is not None:
        slow_notice_task = asyncio.create_task(notify_if_slow())

    try:
        for attempt in range(GEMINI_MAX_ATTEMPTS):
            attempt_start = time.monotonic()
            remaining = GEMINI_TIMEOUT_SECONDS - (attempt_start - overall_started)
            if remaining <= 0:
                last_error = TimeoutError(f"Gemini не ответил за {GEMINI_TIMEOUT_SECONDS} секунд")
                break

            async def call_gemini():
                # Ожидание свободного места в семафоре тоже входит в общий
                # 25-секундный бюджет, поэтому очередь не зависает незаметно.
                async with GEMINI_SEMAPHORE:
                    return await gemini_client.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=GEMINI_GENERATION_CONFIG,
                    )

            try:
                response = await asyncio.wait_for(call_gemini(), timeout=remaining)
                formatted_listing = sanitize_gemini_listing_output(response.text)
                elapsed = time.monotonic() - attempt_start
                logger.info(f"Gemini попытка {attempt + 1}: успех за {elapsed:.1f}с")
                break
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - attempt_start
                logger.warning(f"Gemini попытка {attempt + 1}: тайм-аут после {elapsed:.1f}с")
                last_error = TimeoutError(f"Gemini не ответил за {GEMINI_TIMEOUT_SECONDS} секунд")
                # Не запускаем второй запрос поверх первого после полного
                # тайм-аута. Это снижает нагрузку и исключает минутное ожидание.
                break
            except Exception as e:
                elapsed = time.monotonic() - attempt_start
                logger.warning(f"Gemini попытка {attempt + 1}: ошибка за {elapsed:.1f}с — {e}")
                last_error = e
                if attempt < GEMINI_MAX_ATTEMPTS - 1:
                    remaining = GEMINI_TIMEOUT_SECONDS - (time.monotonic() - overall_started)
                    if remaining <= 0:
                        break
                    # Первая пауза короткая, перед третьей немного больше:
                    # это даёт перегруженному API время восстановиться, но все
                    # попытки по-прежнему входят в общий 25-секундный бюджет.
                    retry_delay = min(0.8 * (2 ** attempt), remaining)
                    await asyncio.sleep(retry_delay)
                    continue
    finally:
        if slow_notice_task is not None:
            slow_notice_task.cancel()
            try:
                await slow_notice_task
            except asyncio.CancelledError:
                pass

    if not formatted_listing:
        raise last_error or RuntimeError("Gemini не вернул текст")

    editable_listing = prepare_listing_for_editing(formatted_listing, contact_url)
    remaining = GEMINI_TIMEOUT_SECONDS - (time.monotonic() - overall_started)
    if remaining <= 0:
        publication_listing = prepare_listing_without_gemini_shortening(editable_listing, contact_url)
    else:
        try:
            publication_listing = await asyncio.wait_for(
                prepare_listing_for_caption(
                    editable_listing,
                    contact_url,
                    allow_gemini_shortening=True,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            logger.warning("Gemini сжатие остановлено общим 25-секундным бюджетом")
            publication_listing = prepare_listing_without_gemini_shortening(editable_listing, contact_url)

    if return_editable:
        return publication_listing, editable_listing
    return publication_listing


async def send_text_with_fallback(
    bot,
    chat_id,
    text,
    reply_markup=None,
    disable_web_page_preview=True,
    label="text",
    retry_ambiguous=True,
):
    """Отправляет HTML-текст целиком. Если HTML битый или текст слишком длинный,
    не теряет содержимое: уходит plain text, при необходимости несколькими частями."""
    if len(text) <= 4096:
        try:
            return await send_with_retry(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                ),
                label=f"{label} (HTML)",
                retry_ambiguous=retry_ambiguous,
            )
        except Exception as e:
            logger.error(f"{label} HTML text error: {e}")
            if not is_html_parse_error(e):
                raise

    plain_chunks = split_plain_text(safe_plain_text(text), limit=4096)
    result = None
    for index, chunk in enumerate(plain_chunks):
        is_last = index == len(plain_chunks) - 1
        result = await send_with_retry(
            lambda chunk=chunk, is_last=is_last: bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup if is_last else None,
                disable_web_page_preview=disable_web_page_preview,
            ),
            label=f"{label} (plain {index + 1}/{len(plain_chunks)})",
            retry_ambiguous=retry_ambiguous,
        )
    return result


async def send_plain_text_chunks(bot, chat_id, text, label="plain text"):
    chunks = split_plain_text(text, limit=4096)
    result = None
    for index, chunk in enumerate(chunks):
        result = await send_with_retry(
            lambda chunk=chunk: bot.send_message(chat_id=chat_id, text=chunk),
            label=f"{label} ({index + 1}/{len(chunks)})",
        )
    return result


def listing_control_text(text):
    plain = strip_html_tags_keep_text(text)
    headline = next((line.strip() for line in plain.splitlines() if line.strip()), "")
    if len(headline) > 120:
        headline = headline[:117].rstrip() + "..."
    if headline:
        return f"Предпросмотр объявления\n\n{headline}\n\nПроверьте текст и выберите действие:"
    return "Предпросмотр объявления\n\nПроверьте текст и выберите действие:"


async def send_listing_with_media(
    bot,
    chat_id,
    text,
    photos,
    reply_markup=None,
    caption_index=0,
    label="listing",
    retry_ambiguous=True,
):
    """Отправляет объявление вместе с фото как подпись.

    Готовый текст заранее подготавливается без лишнего переписывания. Обрезка
    здесь — только последняя страховка для фактического лимита Telegram.
    """
    photos = photos or []

    if not photos:
        return await send_text_with_fallback(
            bot,
            chat_id,
            text,
            reply_markup=reply_markup,
            label=f"{label} (только текст)",
            retry_ambiguous=retry_ambiguous,
        )

    caption = text if len(text) <= TELEGRAM_CAPTION_LIMIT else fit_to_caption(text)
    if caption != text:
        logger.warning(f"{label}: текст был длиннее лимита подписи Telegram и был укорочен")

    try:
        if len(photos) == 1:
            return await send_with_retry(
                lambda: bot.send_photo(
                    chat_id=chat_id,
                    photo=photos[0],
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                ),
                label=f"{label} (фото+подпись)",
                retry_ambiguous=retry_ambiguous,
            )

        safe_index = min(max(caption_index, 0), len(photos) - 1)
        media_group = build_media_group(photos, caption, caption_index=safe_index, parse_mode="HTML")
        sent_messages = await send_with_retry(
            lambda: bot.send_media_group(chat_id=chat_id, media=media_group),
            label=f"{label} (медиагруппа+подпись)",
            retry_ambiguous=retry_ambiguous,
        )
        if reply_markup:
            reply_to_message_id = sent_messages[safe_index].message_id if sent_messages else None
            return await send_with_retry(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=listing_control_text(caption),
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                ),
                label=f"{label} (кнопки к медиагруппе)",
                retry_ambiguous=retry_ambiguous,
            )
        return sent_messages[safe_index] if sent_messages else None
    except Exception as e:
        logger.error(f"{label} HTML media error: {e}")
        if not is_html_parse_error(e):
            raise
        plain_caption = safe_plain_caption(caption)
        if len(photos) == 1:
            return await send_with_retry(
                lambda: bot.send_photo(
                    chat_id=chat_id,
                    photo=photos[0],
                    caption=plain_caption,
                    reply_markup=reply_markup,
                ),
                label=f"{label} (plain фото+подпись)",
                retry_ambiguous=retry_ambiguous,
            )

        safe_index = min(max(caption_index, 0), len(photos) - 1)
        media_group = build_media_group(photos, plain_caption, caption_index=safe_index, parse_mode=None)
        sent_messages = await send_with_retry(
            lambda: bot.send_media_group(chat_id=chat_id, media=media_group),
            label=f"{label} (plain медиагруппа+подпись)",
            retry_ambiguous=retry_ambiguous,
        )
        if reply_markup:
            reply_to_message_id = sent_messages[safe_index].message_id if sent_messages else None
            return await send_with_retry(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=listing_control_text(plain_caption),
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                ),
                label=f"{label} (plain кнопки к медиагруппе)",
                retry_ambiguous=retry_ambiguous,
            )
        return sent_messages[safe_index] if sent_messages else None

    return None



async def send_role_choice(message):
    await message.reply_text(
        "Добро пожаловать в Binio.\n\n"
        "Выберите, как вы хотите опубликовать объявление:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Партнёр / риэлтор", callback_data="role_partner")],
            [InlineKeyboardButton("🏠 Собственник / разовое объявление", callback_data="role_public")],
        ])
    )


async def start_partner_flow(message, context, user, employee_key=None):
    user_id = user.id
    employee_key = canonical_employee_code(employee_key) if employee_key else None
    has_employee_link = employee_key and employee_key in EMPLOYEES

    if not is_admin(user_id) and str(user_id) in REVOKED_PARTNER_IDS:
        set_state(context, user_id, "choosing_role")
        await message.reply_text(
            "Партнёрский доступ для этого аккаунта отключён.\n\n"
            "Если это произошло по ошибке, напишите администратору Binio.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
                [InlineKeyboardButton("🏠 Публикация как собственник", callback_data="role_public")],
            ]),
        )
        return False

    if not has_employee_link and not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "choosing_role")
        await message.reply_text(
            "Вы пока не являетесь партнёром Binio.\n\n"
            "Партнёрский доступ активируется только по персональной ссылке сотрудника Binio. "
            "Если вы уже договорились о сотрудничестве, откройте ссылку, которую прислал сотрудник.\n\n"
            "Чтобы получить партнёрский доступ, напишите администратору.\n\n"
            "Если вы хотите разместить одно объявление без партнёрского доступа, выберите публикацию как собственник",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
                [InlineKeyboardButton("🏠 Публикация как собственник", callback_data="role_public")]
            ])
        )
        return False

    context.application.bot_data.pop(f"editing_listing_{user_id}", None)
    context.application.bot_data.pop(f"property_type_{user_id}", None)
    context.application.bot_data.pop(f"published_money_listing_{user_id}", None)
    context.application.bot_data.pop(f"published_money_field_{user_id}", None)
    if not has_employee_link:
        context.application.bot_data.pop(f"session_contact_{user_id}", None)
        context.application.bot_data.pop(f"session_partner_code_{user_id}", None)
    context.application.bot_data.pop(f"employee_choice_mode_{user_id}", None)
    context.application.bot_data[f"flow_{user_id}"] = "partner"

    if is_admin(user_id) and not has_employee_link:
        context.application.bot_data.pop(f"contact_{user_id}", None)
        context.application.bot_data.pop(f"partner_code_{user_id}", None)

    partner_code = canonical_employee_code(
        context.application.bot_data.get(f"partner_code_{user_id}")
    )

    if has_employee_link:
        contact_url = EMPLOYEES[employee_key]
        if not is_admin(user_id):
            context.application.bot_data[f"contact_{user_id}"] = contact_url
            context.application.bot_data[f"partner_code_{user_id}"] = employee_key
            logger.info(f"Партнёр {user_id} по ссылке: {employee_key} → {contact_url}")
        else:
            context.application.bot_data[f"session_contact_{user_id}"] = contact_url
            context.application.bot_data[f"session_partner_code_{user_id}"] = employee_key
            logger.info(f"Админ {user_id} тестирует партнёрскую ссылку: {employee_key} → {contact_url}")
    elif is_admin(user_id):
        contact_url = DEFAULT_CONTACT
        logger.info(f"Админ {user_id} открыл партнёрский режим без ссылки → {contact_url}")
    elif partner_code in EMPLOYEES:
        contact_url = EMPLOYEES[partner_code]
        context.application.bot_data[f"contact_{user_id}"] = contact_url
        logger.info(f"Партнёр {user_id} восстановлен по коду: {partner_code} → {contact_url}")
    elif is_partner_contact_url(context.application.bot_data.get(f"contact_{user_id}", "")):
        contact_url = context.application.bot_data[f"contact_{user_id}"]
        logger.info(f"Партнёр {user_id} выбрал партнёрский сценарий, сохранён контакт: {contact_url}")
    else:
        contact_url = DEFAULT_CONTACT
        context.application.bot_data[f"contact_{user_id}"] = contact_url
        logger.info(f"Партнёр {user_id} выбрал партнёрский сценарий → {contact_url}")

    context.application.bot_data[f"photos_{user_id}"] = []
    set_state(context, user_id, "waiting_photos")

    partner_code_for_name = (
        context.application.bot_data.get(f"session_partner_code_{user_id}")
        or employee_key
        or context.application.bot_data.get(f"partner_code_{user_id}")
        or employee_key_by_contact(contact_url)
    )
    partner_name = html.escape(employee_display_name(partner_code_for_name))
    contact_safe = html.escape(contact_url, quote=True)
    await message.reply_text(
        "Партнёрский доступ активирован.\n\n"
        f"В объявлениях будет указан контакт: <a href=\"{contact_safe}\">{partner_name}</a>.\n\n"
        "Чтобы создать объявление, отправьте фотографии объекта. После этого бот попросит выбрать тип недвижимости и прислать описание",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True


async def start_public_flow(message, context, user):
    user_id = user.id

    if public_limit_reached(context, user_id):
        set_state(context, user_id, "idle")
        await message.reply_text(
            public_limit_message(context, user_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    context.application.bot_data.pop(f"editing_listing_{user_id}", None)
    context.application.bot_data.pop(f"property_type_{user_id}", None)
    context.application.bot_data.pop(f"published_money_listing_{user_id}", None)
    context.application.bot_data.pop(f"published_money_field_{user_id}", None)
    context.application.bot_data.pop(f"session_contact_{user_id}", None)
    context.application.bot_data.pop(f"session_partner_code_{user_id}", None)
    context.application.bot_data.pop(f"employee_choice_mode_{user_id}", None)
    context.application.bot_data[f"flow_{user_id}"] = "public"
    context.application.bot_data[f"contact_{user_id}"] = user_contact_url(user)
    context.application.bot_data[f"photos_{user_id}"] = []
    set_state(context, user_id, "waiting_photos")

    await message.reply_text(
        "Разовая публикация объявления.\n\n"
        "Стоимость размещения: "
        f"{PUBLIC_LISTING_PRICE_CZK} Kč\n\n"
        f"Лимит: до {PUBLIC_MONTHLY_LIMIT} разовых публикаций в месяц\n\n"
        "Сначала отправьте фотографии объекта. Затем выберите тип недвижимости и пришлите описание.\n\n"
        "Перед оплатой бот покажет готовый предпросмотр: текст можно будет улучшить или исправить"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if get_state(context, user_id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 15–25 секунд")
        return

    context.application.bot_data.pop(f"employee_choice_mode_{user_id}", None)

    employee_key = context.args[0].strip().lower() if context.args else ""
    if employee_key in EMPLOYEES:
        await start_partner_flow(update.message, context, update.effective_user, employee_key)
        return

    set_state(context, user_id, "choosing_role")
    await send_role_choice(update.message)


async def public_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_state(context, update.effective_user.id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 15–25 секунд")
        return
    await start_public_flow(update.message, context, update.effective_user)


async def payment_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_safe = html.escape(DEFAULT_CONTACT, quote=True)
    await update.message.reply_text(
        "<b>Условия разовой публикации</b>\n\n"
        f"Стоимость размещения объявления: <b>{PUBLIC_LISTING_PRICE_CZK} Kč</b>.\n\n"
        "После предпросмотра бот создаёт защищённую страницу оплаты. "
        "После успешной оплаты объявление автоматически отправляется администратору на проверку. "
        "В канале оно появляется только после одобрения. Оплата сама по себе не гарантирует публикацию.\n\n"
        "При отклонении данные платежа сохраняются, но автоматический возврат бот не выполняет. "
        "Решение по возврату принимает администратор после обращения пользователя.\n\n"
        "Если объявление нельзя опубликовать или нужна помощь по оплате, напишите администратору: "
        f'<a href="{contact_safe}">контакт Binio</a>.\n\n'
        "Бот не хранит данные банковской карты. Оплата проходит через подключённого платёжного провайдера",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def payment_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_safe = html.escape(DEFAULT_CONTACT, quote=True)
    await update.message.reply_text(
        "<b>Поддержка Binio</b>\n\n"
        "По вопросам оплаты, публикации или исправления объявления напишите администратору: "
        f'<a href="{contact_safe}">контакт Binio</a>.\n\n'
        "Если вопрос по оплате, укажите, что публикация была через бот, и пришлите время оплаты или скриншот",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def list_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает пользователю его незавершённые заявки после перезапуска/паузы."""
    user_id = update.effective_user.id
    drafts = []
    for key, value in context.application.bot_data.items():
        if not key.startswith("pending_listing_") or not isinstance(value, dict):
            continue
        if value.get("partner_id") == user_id:
            drafts.append(value)
    drafts.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    if not drafts:
        await update.effective_message.reply_text("Незавершённых объявлений нет. Начать новое: /start")
        return
    rows = []
    for item in drafts[:10]:
        listing_id = item.get("listing_id") or next(
            (key.replace("pending_listing_", "", 1) for key, value in context.application.bot_data.items() if value is item and key.startswith("pending_listing_")),
            None,
        )
        if not listing_id:
            continue
        headline = listing_headline(item.get("formatted_listing", "")) or f"Объявление {listing_id}"
        prefix = "✅ На проверке · " if item.get("submitted_to_admin") else "✏️ "
        rows.append([InlineKeyboardButton((prefix + headline)[:60], callback_data=f"draft_resume_{listing_id}")])
    await update.effective_message.reply_text(
        "Ваши незавершённые объявления:\n\nВыберите нужное. Уже отправленные заявки доступны только для просмотра статуса.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def resume_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.replace("draft_resume_", "", 1)
    pending = get_pending(context, listing_id)
    if not pending or pending.get("partner_id") != update.effective_user.id:
        await query.answer("Черновик не найден.", show_alert=True)
        return
    if pending.get("submitted_to_admin"):
        await query.answer("Объявление уже находится на проверке.", show_alert=True)
        return
    if pending.get("source") == "partner" and not has_partner_access(context, update.effective_user.id):
        await query.answer("Доступ партнёра отозван. Обратитесь к администратору.", show_alert=True)
        return
    context.application.bot_data[f"editing_listing_{update.effective_user.id}"] = listing_id
    set_state(context, update.effective_user.id, "done")
    await query.answer()
    if pending.get("source") == "public":
        await show_public_preview(query.message, context, pending.get("formatted_listing", ""), pending.get("photos", []), listing_id)
    else:
        await show_partner_preview(query.message, context, pending.get("formatted_listing", ""), pending.get("photos", []), listing_id)


async def cancel_current_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    for prefix in ("photos_", "property_type_", "session_contact_", "session_partner_code_", "flow_", "editing_listing_"):
        context.application.bot_data.pop(f"{prefix}{user_id}", None)
    set_state(context, user_id, "idle")
    await update.effective_message.reply_text(
        "Текущий шаг отменён. Сохранённые объявления не удалены: /drafts\n\nНачать заново: /start"
    )


async def partner_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_state(context, update.effective_user.id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 15–25 секунд")
        return
    await start_partner_flow(update.message, context, update.effective_user)


async def employee_change_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if get_state(context, user_id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 15–25 секунд")
        return
    if not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "choosing_role")
        await update.message.reply_text(
            "Сменить сотрудника могут только партнёры, которые уже вошли по персональной ссылке.\n\n"
            f'Чтобы получить партнёрский доступ, напишите <a href="{html.escape(DEFAULT_CONTACT, quote=True)}">администратору</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Публикация как собственник", callback_data="role_public")]
            ])
        )
        return

    await ask_employee_choice(update.message, context, user_id, mode="change_employee")


async def role_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if get_state(context, user.id) != "choosing_role":
        await query.answer("Эта кнопка уже устарела. Напишите /start и выберите действие заново.", show_alert=True)
        return

    await query.answer()

    if query.data == "role_partner":
        await start_partner_flow(query.message, context, user)
    elif query.data == "role_public":
        await start_public_flow(query.message, context, user)


async def employee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id

    if get_state(context, user_id) != "choosing_employee":
        await query.answer("Эта кнопка уже устарела. Используйте /employee или /partner.", show_alert=True)
        return

    await query.answer()

    employee_key = query.data.replace("employee_", "", 1)
    if employee_key not in EMPLOYEES:
        await query.message.reply_text("Не удалось выбрать сотрудника\n\nИспользуйте /employee и попробуйте ещё раз")
        return

    mode = context.application.bot_data.pop(f"employee_choice_mode_{user_id}", "start_partner")
    contact_url = EMPLOYEES[employee_key]

    if mode == "change_employee" and not is_admin(user_id):
        context.application.bot_data[f"contact_{user_id}"] = contact_url
        context.application.bot_data[f"partner_code_{user_id}"] = employee_key
        current_preview_updated = update_current_pending_contact(context, user_id, contact_url)
        set_state(context, user_id, "idle")
        contact_safe = html.escape(contact_url, quote=True)
        name_safe = html.escape(employee_display_name(employee_key))
        extra_text = (
            "\n\nТекущий незавершённый предпросмотр тоже обновлён. "
            "Если в старом сообщении визуально остался прежний контакт, при отправке на проверку бот всё равно использует новый"
            if current_preview_updated else ""
        )
        await query.message.reply_text(
            f"Сотрудник изменён: <a href=\"{contact_safe}\">{name_safe}</a>.\n\n"
            "В следующих объявлениях будет указан этот контакт"
            f"{extra_text}\n\n"
            "Чтобы создать новое объявление, используйте /partner",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await start_partner_flow(query.message, context, user, employee_key)


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state == "choosing_role":
        await update.message.reply_text("Сначала выберите тип публикации кнопкой выше")
        return

    if state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee")
        return

    if state != "waiting_photos":
        await update.message.reply_text(
            "Пожалуйста, начните с команды /start"
        )
        return

    key = f"photos_{user_id}"
    if key not in context.application.bot_data:
        context.application.bot_data[key] = []

    MAX_PHOTOS = 10  # ограничение Telegram на кол-во фото в одной медиагруппе

    if len(context.application.bot_data[key]) >= MAX_PHOTOS:
        await update.message.reply_text(
            f"Уже загружено {MAX_PHOTOS} фото — это максимум для одного объявления\n\n"
            "Нажмите «Фото загружены», чтобы продолжить с уже добавленными фотографиями"
        )
        return

    photo = update.message.photo[-1]
    context.application.bot_data[key].append(photo.file_id)
    touch_user_activity(context, user_id)

    count = len(context.application.bot_data[key])

    if count == 1:
        await update.message.reply_text(
            "Фото получено\n\nМожете добавить ещё фотографии. Когда всё будет готово, нажмите «Фото загружены»",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")]
            ])
        )
    elif count == MAX_PHOTOS:
        await update.message.reply_text(
            f"Загружено {MAX_PHOTOS} фото — это максимум\n\n"
            "Нажмите «Фото загружены», чтобы продолжить"
        )


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    # Если уже не в режиме ожидания фото — игнорируем старые кнопки.
    if state != "waiting_photos":
        await query.answer("Эта кнопка уже устарела. Напишите /start, чтобы начать заново.", show_alert=True)
        return

    await query.answer()

    photos = context.application.bot_data.get(f"photos_{user_id}", [])
    if not photos:
        await query.message.reply_text(
            "Сначала загрузите хотя бы одно фото объекта"
        )
        return

    set_state(context, user_id, "waiting_type")
    await query.message.reply_text(
        "Фото приняты\n\n"
        "Выберите тип недвижимости, чтобы бот правильно оформил заголовок и текст",
        reply_markup=property_type_keyboard()
    )


async def property_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state != "waiting_type":
        await query.answer("Эта кнопка уже устарела. Напишите /start, чтобы начать заново.", show_alert=True)
        return

    type_key = query.data.replace("property_type_", "", 1)
    if type_key not in PROPERTY_TYPES:
        type_key = "other"
    property_type = get_property_type(type_key)
    context.application.bot_data[f"property_type_{user_id}"] = type_key
    touch_user_activity(context, user_id)
    set_state(context, user_id, "waiting_text")

    await query.answer()
    try:
        await query.edit_message_text(
            text=f"Выбрано: {property_type['button']}\n\n"
                 "Теперь отправьте описание объекта одним сообщением\n\n"
                 "Лучше всего указать:\n"
                 "— район или адрес\n"
                 "— метраж и планировку\n"
                 "— состояние, мебель и технику\n"
                 "— аренду, коммунальные платежи, залог и комиссию\n"
                 "— дату заезда и важные условия"
        )
    except Exception:
        await query.message.reply_text(
            f"Выбрано: {property_type['button']}\n\n"
            "Теперь отправьте описание объекта одним сообщением\n\n"
            "Лучше всего указать:\n"
            "— район или адрес\n"
            "— метраж и планировку\n"
            "— состояние, мебель и технику\n"
            "— аренду, коммунальные платежи, залог и комиссию\n"
            "— дату заезда и важные условия"
        )


async def handle_wrong_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Партнёр прислал видео, документ или стикер вместо фото"""
    state = get_state(context, update.effective_user.id)
    if state in {"waiting_text", "partner_editing", "published_money_edit"}:
        await update.message.reply_text(
            "На этом шаге нужен обычный текст без файла. Отправьте описание или новое значение текстовым сообщением."
        )
        return
    await update.message.reply_text(
        "Пожалуйста, отправьте именно фотографии объекта\n\n"
        "Видео, документы и стикеры бот не принимает"
    )


async def handle_partner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик текста от партнёра — принимает описание и правки"""
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state == "waiting_text":
        set_state(context, user_id, "processing")
        await process_listing(update, context, update.message.text)
    elif state == "partner_editing":
        set_state(context, user_id, "processing")
        await apply_partner_manual_edit(update, context, update.message.text)
    elif state == "published_money_edit":
        await partner_apply_money_update(update, context, update.message.text)
    elif state == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 15–25 секунд")
    elif state == "submitted":
        await update.message.reply_text(
            "Объявление уже отправлено на проверку\n\nДля нового объявления используйте /start"
        )
    elif state == "waiting_type":
        await update.message.reply_text(
            "Сначала выберите тип объекта кнопкой выше: квартира, комната, дом, участок, коммерция или другое"
        )
    elif state == "choosing_role":
        await update.message.reply_text("Сначала выберите тип публикации кнопкой выше")
    elif state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee")
    elif state == "waiting_photos":
        await update.message.reply_text(
            "Сначала отправьте фотографии объекта. Когда всё будет готово, нажмите «Фото загружены»"
        )
    else:
        await update.message.reply_text(
            "Добро пожаловать в Binio.\n\nИспользуйте /start, чтобы начать"
        )


async def process_listing(update, context, text):
    """Обрабатывает текст через Gemini и показывает предпросмотр."""
    user_id = update.effective_user.id
    set_state(context, user_id, "processing")

    processing_message = await update.message.reply_text(
        "⏳ Обрабатываю объявление…\n\nОбычно это занимает 15–25 секунд"
    )
    typing_task = asyncio.create_task(
        keep_chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING)
    )
    gemini_quota_prefix = None

    try:
        if gemini_client is None:
            raise RuntimeError("Gemini API key не задан")

        editing_listing_id = context.application.bot_data.get(f"editing_listing_{user_id}")
        existing_pending = get_pending(context, editing_listing_id) if editing_listing_id else None

        if existing_pending:
            listing_id = editing_listing_id
            contact_url = existing_pending.get('contact_url', context.application.bot_data.get(f"contact_{user_id}", DEFAULT_CONTACT))
            photos = existing_pending.get('photos', [])
            property_type_key = existing_pending.get('property_type', context.application.bot_data.get(f"property_type_{user_id}", "other"))
            source = existing_pending.get('source', context.application.bot_data.get(f"flow_{user_id}", "partner"))
            employee_code = existing_pending.get("employee_code")
        else:
            listing_id = new_listing_id()
            session_partner_code = context.application.bot_data.get(f"session_partner_code_{user_id}")
            contact_url = context.application.bot_data.pop(
                f"session_contact_{user_id}",
                context.application.bot_data.get(f"contact_{user_id}", DEFAULT_CONTACT)
            )
            context.application.bot_data.pop(f"session_partner_code_{user_id}", None)
            photos = list(context.application.bot_data.get(f"photos_{user_id}", []))
            property_type_key = context.application.bot_data.get(f"property_type_{user_id}", "other")
            source = context.application.bot_data.get(f"flow_{user_id}", "partner")
            employee_code = canonical_employee_code(
                session_partner_code or context.application.bot_data.get(f"partner_code_{user_id}")
            )
            if source == "partner" and employee_code not in EMPLOYEES and contact_url != DEFAULT_CONTACT:
                employee_code = employee_key_by_contact(contact_url)

        if source != "partner" or employee_code not in EMPLOYEES:
            employee_code = None

        quota_allowed = (
            consume_public_gemini_request(context, user_id)
            if source == "public"
            else consume_partner_gemini_request(context, user_id)
        )
        if not quota_allowed:
            set_state(context, user_id, "waiting_text")
            await update_processing_status(
                processing_message,
                "Дневной лимит автоматического улучшения текста исчерпан.\n\n"
                "Попробуйте снова завтра или напишите администратору Binio.",
            )
            return
        gemini_quota_prefix = "public_gemini_usage_" if source == "public" else "partner_gemini_usage_"

        formatted_listing, editable_listing = await generate_formatted_listing(
            text,
            property_type_key,
            contact_url,
            status_message=processing_message,
            return_editable=True,
        )

        partner_label = format_partner_for_admin(update.effective_user)

        save_pending(context, listing_id, {
            'formatted_listing': formatted_listing,
            'editable_listing': editable_listing,
            'photos': photos,
            'partner_id': user_id,
            'partner_label': partner_label,
            'contact_url': contact_url,
            'employee_code': employee_code,
            'property_type': property_type_key,
            'source_text': text,
            'source': source,
            'paid': bool(existing_pending.get('paid')) if existing_pending else source == "partner",
            'submitted_to_admin': bool(existing_pending.get('submitted_to_admin')) if existing_pending else False,
        })

        context.application.bot_data[f"editing_listing_{user_id}"] = listing_id
        set_state(context, user_id, "done")
        await update_processing_status(processing_message, "✅ Готово — показываю предпросмотр")
        if source == "public":
            await show_public_preview(update.message, context, formatted_listing, photos, listing_id)
        else:
            await show_partner_preview(update.message, context, formatted_listing, photos, listing_id)

    except Exception as e:
        logger.error(f"process_listing error: {e}")
        if gemini_quota_prefix:
            refund_daily_gemini_request(context, user_id, gemini_quota_prefix)
        set_state(context, user_id, "waiting_text")
        await update_processing_status(
            processing_message,
            "Сервис временно недоступен\n\nПожалуйста, отправьте текст ещё раз через несколько секунд"
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def apply_partner_manual_edit(update, context, edited_text):
    """Сохраняет ручную правку напрямую, не отправляя её обратно в Gemini."""
    user_id = update.effective_user.id
    listing_id = context.application.bot_data.get(f"editing_listing_{user_id}")
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("partner_id") != user_id:
        set_state(context, user_id, "done")
        await update.message.reply_text(
            "Объявление для редактирования не найдено\n\nОткройте нужное объявление и нажмите «Изменить текст» ещё раз"
        )
        return
    if pending.get("submitted_to_admin") or pending_busy(pending, "submit_in_progress"):
        set_state(context, user_id, "submitted")
        await update.message.reply_text("Объявление уже отправлено на проверку. Изменения закрыты.")
        return

    contact_url = pending.get("contact_url", DEFAULT_CONTACT)
    editable_listing = prepare_listing_for_editing(edited_text, contact_url)
    if len(editable_listing) > TELEGRAM_CAPTION_LIMIT:
        set_state(context, user_id, "partner_editing")
        await update.message.reply_text(
            f"Текст слишком длинный: {len(editable_listing)} символов при лимите {TELEGRAM_CAPTION_LIMIT}.\n\n"
            "Сократите его и отправьте снова. Ничего не было обрезано или потеряно."
        )
        return
    formatted_listing = await prepare_listing_for_caption(
        editable_listing,
        contact_url,
        allow_gemini_shortening=False,
    )
    pending["editable_listing"] = editable_listing
    pending["formatted_listing"] = formatted_listing
    # Если после ручной правки нажать «Улучшить текст», Gemini начинает с
    # актуальной ручной версии, а не с устаревшего первоначального описания.
    pending["source_text"] = edited_text
    save_pending(context, listing_id, pending)
    set_state(context, user_id, "done")

    await update.message.reply_text("✅ Изменения сохранены — показываю предпросмотр")
    if pending.get("source") == "public":
        await show_public_preview(
            update.message,
            context,
            formatted_listing,
            pending.get("photos", []),
            listing_id,
        )
    else:
        await show_partner_preview(
            update.message,
            context,
            formatted_listing,
            pending.get("photos", []),
            listing_id,
        )


async def show_partner_preview(message, context, listing, photos, listing_id):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить на проверку", callback_data=f"submit_{listing_id}")],
        [
            InlineKeyboardButton("✨ Улучшить текст", callback_data=f"regen_{listing_id}"),
            InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}"),
        ],
    ])

    await send_listing_with_media(
        context.bot,
        message.chat_id,
        listing,
        photos,
        reply_markup=keyboard,
        caption_index=len(photos) - 1,
        label="show_partner_preview",
    )


async def show_public_preview(message, context, listing, photos, listing_id):
    pending = get_pending(context, listing_id)
    if pending and pending.get("paid"):
        rows = [[InlineKeyboardButton("✅ Отправить на проверку", callback_data=f"submit_paid_public_{listing_id}")]]
    else:
        rows = [[InlineKeyboardButton(f"💳 Оплатить {PUBLIC_LISTING_PRICE_CZK} Kč", callback_data=f"pay_public_{listing_id}")]]
    if (
        PUBLIC_PAYMENT_TEST_MODE
        and pending
        and is_admin(pending.get("partner_id"))
        and not pending.get("paid")
    ):
        rows.append([InlineKeyboardButton("🧪 Тест: пропустить оплату", callback_data=f"test_pay_public_{listing_id}")])
    rows.extend([
        [
            InlineKeyboardButton("✨ Улучшить текст", callback_data=f"regen_{listing_id}"),
            InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}"),
        ],
    ])
    keyboard = InlineKeyboardMarkup(rows)

    await send_listing_with_media(
        context.bot,
        message.chat_id,
        listing,
        photos,
        reply_markup=keyboard,
        caption_index=len(photos) - 1,
        label="show_public_preview",
    )


async def send_public_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.split("_", 2)[2]
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer("Объявление не найдено. Начните заново через /owner.", show_alert=True)
        return
    if pending.get("source") != "public":
        await query.answer("Это действие доступно только для платной публикации.", show_alert=True)
        return
    if update.effective_user.id != pending.get("partner_id"):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return
    if pending.get("submitted_to_admin"):
        await query.answer("Объявление уже отправлено на проверку.", show_alert=True)
        return
    if requires_payment_manual_review(context, listing_id, pending):
        await query.answer(
            "Этот платёж уже проверяется администратором. Повторно оплачивать нельзя.",
            show_alert=True,
        )
        return
    if pending.get("paid"):
        await query.answer()
        await submit_paid_public_listing(context, listing_id, update.effective_user, query.message, test_mode=False)
        return
    if public_limit_reached(context, update.effective_user.id):
        await query.answer()
        await query.message.reply_text(
            public_limit_message(context, update.effective_user.id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    if pending_busy(pending, "invoice_in_progress", ttl_seconds=60):
        await query.answer("Счёт уже формируется или недавно отправлен.", show_alert=True)
        return
    if not STRIPE_ENABLED and not PAYMENT_PROVIDER_TOKEN:
        await query.answer("Оплата сейчас не подключена. Пожалуйста, напишите администратору.", show_alert=True)
        return
    if STRIPE_ENABLED and public_invoice_active(pending) and pending.get("stripe_checkout_url"):
        display_amount = f"{expected_payment_amount(pending) / 100:g} {expected_payment_currency(pending)}"
        await query.answer()
        await query.message.reply_text(
            "Ссылка на оплату уже создана\n\n"
            "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты Stripe",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"💳 Оплатить {display_amount}", url=pending["stripe_checkout_url"])
            ]])
        )
        return
    if (
        not STRIPE_ENABLED
        and public_invoice_active(pending)
        and pending.get("invoice_delivery_state") in {"delivered", "unknown"}
    ):
        await query.answer(
            "Действующий счёт уже был отправлен. Используйте его; повторный счёт появится только после истечения срока.",
            show_alert=True,
        )
        return
    # Новая платёжная попытка получает неизменяемую сумму. Уже созданная
    # активная Stripe-сессия выше сохраняет свою первоначальную цену.
    pending["payment_expected_amount"] = PUBLIC_PAYMENT_AMOUNT
    pending["payment_expected_currency"] = PUBLIC_PAYMENT_CURRENCY
    checkout_expires_at = int(pending.get("stripe_checkout_expires_at") or 0)
    if (
        not pending.get("stripe_checkout_idempotency_key")
        or checkout_expires_at <= int(time.time()) + 60
    ):
        pending["stripe_checkout_idempotency_key"] = f"binio-checkout-{listing_id}-{uuid.uuid4().hex}"
        pending["stripe_checkout_expires_at"] = int(time.time() + PUBLIC_INVOICE_TTL_HOURS * 60 * 60)
        pending["stripe_checkout_attempt_at"] = now_iso()
    pending["invoice_created_at"] = now_iso()
    mark_pending_busy(context, listing_id, pending, "invoice_in_progress")
    # Ключ идемпотентности должен попасть на диск до сетевого запроса Stripe.
    # Тогда ReadError не сможет создать второй независимый счёт при повторе.
    await persist_now(context.application)

    formatted_listing = await prepare_listing_for_caption(
        pending['formatted_listing'],
        pending.get('contact_url', DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    if formatted_listing != pending.get('formatted_listing'):
        pending['formatted_listing'] = formatted_listing
        save_pending(context, listing_id, pending)
    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        pending.pop("invoice_created_at", None)
        clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
        await query.answer()
        await query.message.reply_text(
            validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    await query.answer()
    if STRIPE_ENABLED:
        await send_stripe_checkout_link(query, context, listing_id, pending)
        return

    if not PAYMENT_PROVIDER_TOKEN:
        pending.pop("invoice_created_at", None)
        clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
        await query.message.reply_text(
            "Оплата сейчас не подключена\n\nПожалуйста, напишите администратору"
        )
        return

    try:
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="Публикация объявления",
            description="Платное размещение объявления в канале Binio.",
            payload=public_payment_payload(listing_id),
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=expected_payment_currency(pending),
            prices=[LabeledPrice(label="Публикация объявления", amount=expected_payment_amount(pending))],
            start_parameter=f"public-{listing_id}",
        )
    except Exception as e:
        logger.error(f"send_public_invoice error: {e}")
        clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
        if is_transient_network_error(e) and not isinstance(e, RetryAfter):
            pending["invoice_delivery_state"] = "unknown"
            await persist_now(context.application)
            await query.message.reply_text(
                "Telegram не подтвердил доставку счёта. Не нажимайте оплату повторно: "
                "если счёт не появился в чате, подождите срок его действия или напишите администратору."
            )
        else:
            pending.pop("invoice_created_at", None)
            pending["invoice_delivery_state"] = "failed"
            await persist_now(context.application)
            await query.message.reply_text("Не получилось отправить счёт на оплату\n\nПопробуйте ещё раз позже")
        return
    clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
    pending["invoice_delivery_state"] = "delivered"
    await persist_now(context.application)


async def partner_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.split("_", 1)[1]
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer("Объявление не найдено. Начните заново через /start.", show_alert=True)
        return

    if update.effective_user.id != pending.get('partner_id'):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return

    user_id = update.effective_user.id
    if get_state(context, user_id) == "processing":
        await query.answer("Объявление ещё обрабатывается. Обычно это занимает 15–25 секунд.", show_alert=True)
        return

    if pending.get('submitted_to_admin'):
        await query.answer("Объявление уже отправлено на проверку. Изменения закрыты.", show_alert=True)
        return
    if pending_busy(pending, "submit_in_progress"):
        await query.answer("Объявление уже отправляется на проверку. Изменения закрыты.", show_alert=True)
        return

    source_text = pending.get('source_text')
    if not source_text:
        await query.answer()
        await query.message.reply_text(
            "Для этой старой заявки не сохранился исходный текст.\n\n"
            "Нажмите «Изменить текст» и отправьте описание заново",
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    is_public = pending.get("source") == "public"
    quota_allowed = (
        consume_public_gemini_request(context, user_id)
        if is_public else consume_partner_gemini_request(context, user_id)
    )
    if not quota_allowed:
        await query.answer(
            "Дневной лимит автоматического улучшения исчерпан. Попробуйте снова завтра.",
            show_alert=True,
        )
        return
    gemini_quota_prefix = "public_gemini_usage_" if is_public else "partner_gemini_usage_"

    await query.answer()
    set_state(context, user_id, "processing")
    processing_message = await query.message.reply_text("⏳ Готовлю более аккуратный вариант текста…")
    typing_task = asyncio.create_task(
        keep_chat_action(context.bot, query.message.chat_id, ChatAction.TYPING)
    )

    try:
        contact_url = pending.get('contact_url', context.application.bot_data.get(f"contact_{user_id}", DEFAULT_CONTACT))
        property_type_key = pending.get('property_type', context.application.bot_data.get(f"property_type_{user_id}", "other"))
        formatted_listing, editable_listing = await generate_formatted_listing(
            source_text,
            property_type_key,
            contact_url,
            status_message=processing_message,
            return_editable=True,
        )

        current = get_pending(context, listing_id)
        if (
            current is not pending
            or current is None
            or current.get("submitted_to_admin")
            or pending_busy(current, "submit_in_progress")
        ):
            set_state(
                context,
                user_id,
                "submitted" if current and current.get("submitted_to_admin") else "done",
            )
            await update_processing_status(
                processing_message,
                "Объявление уже было оплачено или отправлено на проверку, пока готовился новый вариант. "
                "Новая версия не применена, чтобы текст у вас и у администратора не различался.",
            )
            return
        pending = current
        pending['formatted_listing'] = formatted_listing
        pending['editable_listing'] = editable_listing
        pending['contact_url'] = contact_url
        pending['property_type'] = property_type_key
        save_pending(context, listing_id, pending)
        set_state(context, user_id, "done")

        await update_processing_status(processing_message, "✅ Готово — показываю новый вариант")
        if pending.get('source') == "public":
            await show_public_preview(
                query.message,
                context,
                formatted_listing,
                pending.get('photos', []),
                listing_id,
            )
        else:
            await show_partner_preview(
                query.message,
                context,
                formatted_listing,
                pending.get('photos', []),
                listing_id,
            )
    except Exception as e:
        logger.error(f"partner_regenerate error: {e}")
        refund_daily_gemini_request(context, user_id, gemini_quota_prefix)
        set_state(context, user_id, "done")
        await update_processing_status(
            processing_message,
            "Не получилось улучшить текст автоматически\n\nПопробуйте ещё раз или исправьте его вручную",
            reply_markup=listing_fix_keyboard(listing_id),
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def partner_edit_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.split("_", 2)[2]
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer("Объявление не найдено. Начните заново через /start.", show_alert=True)
        return

    # Действие может выполнить только владелец этого объявления.
    if update.effective_user.id != pending.get('partner_id'):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return
    if get_state(context, update.effective_user.id) == "processing":
        await query.answer("Объявление ещё обрабатывается. Обычно это занимает 15–25 секунд.", show_alert=True)
        return

    if pending.get('submitted_to_admin'):
        await query.answer("Объявление уже отправлено на проверку. Изменения закрыты.", show_alert=True)
        return
    if pending_busy(pending, "submit_in_progress"):
        await query.answer("Объявление уже отправляется на проверку. Изменения закрыты.", show_alert=True)
        return

    await query.answer()
    context.application.bot_data[f"editing_listing_{update.effective_user.id}"] = listing_id
    set_state(context, update.effective_user.id, "partner_editing")

    try:
        await query.edit_message_text(text="Отправьте исправленный текст объявления одним сообщением\n\nПосле этого бот покажет новый предпросмотр")
    except Exception:
        try:
            await query.edit_message_caption(caption="Отправьте исправленный текст объявления одним сообщением\n\nПосле этого бот покажет новый предпросмотр")
        except Exception:
            await query.message.reply_text("Отправьте исправленный текст объявления одним сообщением\n\nПосле этого бот покажет новый предпросмотр")

    # Редактор всегда получает последний полный вариант Gemini/пользователя,
    # а не техническую подпись, которая могла быть подогнана под лимит фото.
    plain_text = pending.get('editable_listing') or pending['formatted_listing']
    contact_url_saved = pending.get('contact_url', DEFAULT_CONTACT)
    plain_text = remove_contact_from_listing(plain_text, contact_url_saved)
    plain_text = strip_html_tags_keep_text(plain_text)
    await send_plain_text_chunks(
        context.bot,
        query.message.chat_id,
        f"Текущий текст для редактирования:\n\n{plain_text}",
        label="partner_edit current_text",
    )


async def send_pending_to_admin(
    context,
    listing_id,
    pending,
    submitter_label,
    label="submit_to_admin",
    retry_ambiguous=False,
):
    formatted_listing = await prepare_listing_for_caption(
        pending['formatted_listing'],
        pending.get('contact_url', DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    if formatted_listing != pending.get('formatted_listing'):
        pending['formatted_listing'] = formatted_listing
        save_pending(context, listing_id, pending)

    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        return issues

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{listing_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{listing_id}")
        ],
        [InlineKeyboardButton("✏️ Исправить", callback_data=f"edit_more_{listing_id}")]
    ])
    # Если заголовок уже дошёл, а отправка фото временно оборвалась, повторная
    # попытка не должна дублировать этот заголовок в чате одобрения.
    if not pending.get("admin_submitter_info_sent"):
        await send_text_with_fallback(
            context.bot,
            ADMIN_CHAT_ID,
            f"📋 Новое объявление от {submitter_label}",
            label=f"{label} submitter_info",
            retry_ambiguous=retry_ambiguous,
        )
        pending["admin_submitter_info_sent"] = True
        save_pending(context, listing_id, pending)
    review_message = await send_listing_with_media(
        context.bot,
        ADMIN_CHAT_ID,
        formatted_listing,
        pending['photos'],
        reply_markup=admin_keyboard,
        caption_index=len(pending['photos']) - 1,
        label=label,
        retry_ambiguous=retry_ambiguous,
    )
    review_message_id = getattr(review_message, "message_id", None)
    if review_message_id is not None:
        pending["admin_review_message_id"] = review_message_id
        save_pending(context, listing_id, pending)
    return []


async def partner_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.split("_", 1)[1]
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer()
        await query.message.reply_text("Не удалось найти это объявление\n\nПожалуйста, начните заново через /start")
        return

    # Действие может выполнить только владелец этого объявления.
    if update.effective_user.id != pending.get('partner_id'):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return

    if get_state(context, update.effective_user.id) == "processing":
        await query.answer("Подождите, объявление ещё обрабатывается.", show_alert=True)
        return

    if pending.get('submitted_to_admin'):
        await query.answer("Объявление уже отправлено на проверку.", show_alert=True)
        return
    if pending_busy(pending, 'submit_in_progress'):
        await query.answer("Объявление уже отправляется на проверку.", show_alert=True)
        return

    # Сначала проверяем полностью локальные условия. Затем ДО первого внешнего
    # сообщения фиксируем статус отправки на диск: администратор может нажать
    # кнопку очень быстро, и поздний ответ этой функции не должен воскресить
    # уже опубликованную/отклонённую заявку.
    formatted_listing = await prepare_listing_for_caption(
        pending.get('formatted_listing', ''),
        pending.get('contact_url', DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        await query.answer()
        await query.message.reply_text(
            validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    mark_pending_busy(context, listing_id, pending, "submit_in_progress")
    pending['formatted_listing'] = formatted_listing
    pending['submitted_to_admin'] = True
    pending.setdefault('submitted_at', now_iso())
    pending['review_state'] = 'sending'
    save_pending(context, listing_id, pending)
    await persist_now(context.application)
    await query.answer()

    partner_label = pending.get('partner_label') or format_partner_for_admin(update.effective_user)
    try:
        issues = await send_pending_to_admin(context, listing_id, pending, partner_label, label="partner_submit")
    except asyncio.CancelledError:
        current = get_pending(context, listing_id)
        if current is pending:
            current.pop('submit_in_progress', None)
            current['review_state'] = 'unknown'
            save_pending(context, listing_id, current)
            await persist_now(context.application)
        raise
    except Exception as e:
        logger.error(f"partner_submit send error: {e}")
        current = get_pending(context, listing_id)
        if current is pending:
            current.pop('submit_in_progress', None)
            if is_transient_network_error(e) and not isinstance(e, RetryAfter):
                current['review_state'] = 'unknown'
                message = (
                    "Telegram не вернул надёжный ответ. Заявка сохранена, повторная отправка остановлена, "
                    "чтобы не создать дубль. Администратор проверит её вручную."
                )
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            "⚠️ Неизвестен результат доставки заявки в чат проверки.\n\n"
                            f"listing_id={listing_id}\nПроверьте последние сообщения; повтор пользователем остановлен."
                        ),
                    )
                except Exception:
                    pass
            else:
                current['submitted_to_admin'] = False
                current['review_state'] = 'failed'
                message = "Не получилось отправить объявление на проверку. Попробуйте нажать кнопку ещё раз."
            current['review_error'] = str(e)[:300]
            save_pending(context, listing_id, current)
            await persist_now(context.application)
            await query.message.reply_text(message)
        return
    if issues:
        current = get_pending(context, listing_id)
        if current is pending:
            current.pop('submit_in_progress', None)
            current['submitted_to_admin'] = False
            current['review_state'] = 'failed'
            save_pending(context, listing_id, current)
            await persist_now(context.application)
            await query.message.reply_text(
                validation_message(issues),
                reply_markup=listing_fix_keyboard(listing_id),
            )
        return

    current = get_pending(context, listing_id)
    if current is not pending:
        # Администратор успел завершить действие, пока Telegram возвращал ответ.
        set_state(context, update.effective_user.id, "submitted")
        return
    pending.pop('submit_in_progress', None)
    pending['review_state'] = 'delivered'
    pending.pop('review_error', None)
    save_pending(context, listing_id, pending)
    await persist_now(context.application)
    set_state(context, update.effective_user.id, "submitted")

    try:
        await query.edit_message_text(
            text="✅ Объявление отправлено на проверку\n\nДля нового объявления напишите /start"
        )
    except Exception:
        try:
            await query.edit_message_caption(
                caption="✅ Объявление отправлено на проверку\n\nДля нового объявления напишите /start"
            )
        except Exception:
            await query.message.reply_text(
                "✅ Объявление отправлено на проверку\n\nДля нового объявления напишите /start"
            )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    listing_id = listing_id_from_payment_payload(query.invoice_payload)
    pending = get_pending(context, listing_id) if listing_id else None

    if (
        not pending
        or pending.get("source") != "public"
        or pending.get("partner_id") != query.from_user.id
        or pending.get("submitted_to_admin")
        or pending.get("paid")
        or requires_payment_manual_review(context, listing_id, pending)
        or query.currency != expected_payment_currency(pending)
        or query.total_amount != expected_payment_amount(pending)
    ):
        await query.answer(ok=False, error_message="Счёт устарел. Создайте публикацию заново через /owner.")
        return

    if not public_invoice_active(pending):
        await query.answer(
            ok=False,
            error_message="Счёт устарел. Создайте публикацию заново через /owner."
        )
        return

    if public_limit_reached(context, query.from_user.id, exclude_listing_id=listing_id):
        await query.answer(
            ok=False,
            error_message=f"Лимит разовых публикаций на месяц: {PUBLIC_MONTHLY_LIMIT}. Напишите администратору Binio."
        )
        return

    await query.answer(ok=True)


async def submit_paid_public_listing(
    context,
    listing_id,
    user,
    reply_message,
    test_mode=False,
    raise_on_delivery_failure=False,
    background_delivery=False,
):
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("source") != "public":
        # Между постановкой фоновой задачи и её выполнением пользователь или
        # администратор мог уже завершить/удалить заявку. Для recovery это
        # нормальная идемпотентная гонка, а не ошибка каждые 30 секунд.
        if not background_delivery:
            logger.warning(
                "submit_paid_public_listing: pending public listing not found listing_id=%s",
                listing_id,
            )
        return False
    if pending.get("partner_id") != user.id:
        logger.warning("submit_paid_public_listing: user_id mismatch")
        return False
    if pending.get("payment_review_required"):
        if not background_delivery:
            await reply_message.reply_text(
                "Платёж или его статус проверяется администратором. Повторная оплата и отправка не требуются."
            )
        return False
    if pending.get("submitted_to_admin"):
        await reply_message.reply_text("✅ Объявление уже отправлено на проверку")
        return True
    # Пользователь нажал «Повторить отправку» после ручного исправления.
    # Фоновая доставка до этого была остановлена, чтобы не повторять одну и ту
    # же ошибку и не отправлять пользователю одинаковые сообщения каждые 30 сек.
    pending.pop("paid_delivery_needs_fix", None)
    if pending_busy(pending, "submit_in_progress"):
        # Нельзя сообщать Stripe об успешной обработке, пока доставка ещё не
        # завершилась. После отмены/сбоя recovery-loop безопасно повторит её.
        if pending.get("paid"):
            schedule_paid_delivery(context.application, listing_id)
        if not raise_on_delivery_failure:
            await reply_message.reply_text("Объявление уже отправляется на проверку. Повторная оплата не нужна.")
        return False

    # Обычный Telegram update содержит актуальный username. Фоновый Stripe
    # знает только user_id: в этом случае сохраняем уже проверенный t.me-контакт,
    # потому что tg://user?id может не открыться из-за настроек приватности.
    if getattr(user, "username", None):
        pending["contact_url"] = user_contact_url(user)
    elif not pending.get("contact_url"):
        pending["contact_url"] = user_contact_url(user)
    formatted_listing = await prepare_listing_for_caption(
        pending.get('formatted_listing', ''),
        pending.get('contact_url', DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        pending["paid"] = True
        pending["paid_delivery_needs_fix"] = True
        save_pending(context, listing_id, pending)
        await persist_now(context.application)
        if not background_delivery:
            await reply_message.reply_text(
                "✅ Оплата получена, но перед проверкой объявление нужно поправить\n\n"
                + validation_message(issues),
                reply_markup=listing_fix_keyboard(listing_id),
            )
        return False

    pending["paid"] = True
    pending.setdefault("payment_paid_at", now_iso())
    mark_pending_busy(context, listing_id, pending, "submit_in_progress")
    pending['formatted_listing'] = formatted_listing
    pending['submitted_to_admin'] = True
    pending.setdefault('submitted_at', now_iso())
    pending['review_state'] = 'sending'
    if test_mode:
        pending["payment_test_mode"] = True
        pending["payment_total_amount"] = 0
        pending["payment_currency"] = PUBLIC_PAYMENT_CURRENCY
        save_payment_record(context, listing_id, {
            "listing_id": listing_id,
            "user_id": user.id,
            "partner_id": user.id,
            "source": "public",
            "paid": True,
            "payment_status": "test_paid",
            "payment_test_mode": True,
            "test_mode": True,
            "payment_total_amount": 0,
            "payment_currency": PUBLIC_PAYMENT_CURRENCY,
            "payment_paid_at": pending["payment_paid_at"],
        })
    save_pending(context, listing_id, pending)
    await persist_now(context.application)

    public_label = "платного пользователя " + (pending.get("partner_label") or format_partner_for_admin(user))
    if test_mode:
        public_label = "тестовое платное объявление от " + (pending.get("partner_label") or format_partner_for_admin(user))

    try:
        issues = await send_pending_to_admin(context, listing_id, pending, public_label, label="public_paid_submit")
    except asyncio.CancelledError:
        # CancelledError не является обычным Exception. Если его не обработать,
        # флаг submit_in_progress останется навсегда и повторная доставка будет
        # ложно считаться успешной.
        current = get_pending(context, listing_id)
        if current is pending:
            current.pop("submit_in_progress", None)
            current["review_state"] = "unknown"
            save_pending(context, listing_id, current)
            await persist_now(context.application)
        raise
    except Exception as e:
        logger.error(f"submit_paid_public_listing error: {e}")
        current = get_pending(context, listing_id)
        if current is not pending:
            return True
        current.pop("submit_in_progress", None)
        ambiguous = is_transient_network_error(e) and not isinstance(e, RetryAfter)
        if ambiguous:
            current["review_state"] = "unknown"
        else:
            current["submitted_to_admin"] = False
            current["review_state"] = "failed"
            current["paid_delivery_needs_fix"] = True
        current["review_error"] = str(e)[:300]
        save_pending(context, listing_id, current)
        await persist_now(context.application)
        should_notify = not background_delivery or not current.get("paid_delivery_notice_sent")
        if should_notify:
            current["paid_delivery_notice_sent"] = True
            save_pending(context, listing_id, current)
            retry_markup = None if ambiguous else InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Повторить отправку", callback_data=f"submit_paid_public_{listing_id}")
            ]])
            try:
                await reply_message.reply_text(
                    "✅ Оплата получена, но объявление не получилось отправить на проверку\n\n"
                    + (
                        "Результат отправки неизвестен. Автоматический повтор остановлен, чтобы не создать дубль. "
                        "Администратор уведомлён; повторная оплата не требуется."
                        if ambiguous else
                        "Нажмите кнопку ниже, чтобы безопасно повторить отправку. Повторная оплата не требуется."
                    ),
                    reply_markup=retry_markup,
                )
            except Exception as notify_error:
                logger.warning("Не удалось уведомить оплатившего пользователя: %s", notify_error)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        "⚠️ Оплата получена, но объявление не отправилось на проверку.\n\n"
                        f"listing_id={listing_id}\nreview_state={current.get('review_state')}\nошибка: {e}"
                    )
                )
            except Exception:
                pass
        if raise_on_delivery_failure:
            raise RuntimeError(
                f"Оплата подтверждена, но доставка объявления администратору не удалась: {e}"
            ) from e
        return False

    if issues:
        current = get_pending(context, listing_id)
        if current is pending:
            current.pop("submit_in_progress", None)
            current["submitted_to_admin"] = False
            current["review_state"] = "failed"
            current["paid_delivery_needs_fix"] = True
            save_pending(context, listing_id, current)
            await persist_now(context.application)
            await reply_message.reply_text(
                "✅ Оплата получена, но перед проверкой объявление нужно поправить\n\n"
                + validation_message(issues),
                reply_markup=listing_fix_keyboard(listing_id),
            )
        return False

    current = get_pending(context, listing_id)
    if current is not pending:
        set_state(context, user.id, "submitted")
        return True
    pending.pop("submit_in_progress", None)
    pending["review_state"] = "delivered"
    pending.pop("review_error", None)
    pending.pop("paid_delivery_needs_fix", None)
    pending.pop("paid_delivery_notice_sent", None)
    save_pending(context, listing_id, pending)
    payment_record = get_payment_record(context, listing_id)
    if payment_record:
        save_payment_record(context, listing_id, {"listing_status": "submitted", "submitted_at": now_iso()})
    await persist_now(context.application)
    set_state(context, user.id, "submitted")
    await reply_message.reply_text("✅ Оплата получена\n\nОбъявление отправлено на проверку")
    return True


async def test_public_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.replace("test_pay_public_", "", 1)

    if not PUBLIC_PAYMENT_TEST_MODE:
        await query.answer("Тестовый режим оплаты выключен.", show_alert=True)
        return
    if not is_admin(update.effective_user.id):
        await query.answer("Тестовая оплата доступна только администратору.", show_alert=True)
        return

    pending = get_pending(context, listing_id)
    if not pending:
        await query.answer("Объявление не найдено. Начните заново через /owner.", show_alert=True)
        return
    if pending.get("source") != "public":
        await query.answer("Это действие доступно только для платной публикации.", show_alert=True)
        return
    if update.effective_user.id != pending.get("partner_id"):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return
    if pending.get("submitted_to_admin"):
        await query.answer("Объявление уже отправлено на проверку.", show_alert=True)
        return
    if requires_payment_manual_review(context, listing_id, pending):
        await query.answer("Платёж уже находится на ручной проверке.", show_alert=True)
        return
    if public_limit_reached(context, update.effective_user.id):
        await query.answer()
        await query.message.reply_text(
            public_limit_message(context, update.effective_user.id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await query.answer("Тестовая оплата засчитана.", show_alert=True)
    await submit_paid_public_listing(context, listing_id, update.effective_user, query.message, test_mode=True)


async def submit_already_paid_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.replace("submit_paid_public_", "", 1)
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer("Объявление не найдено. Начните заново через /owner.", show_alert=True)
        return
    if pending.get("source") != "public":
        await query.answer("Это действие доступно только для платной публикации.", show_alert=True)
        return
    if update.effective_user.id != pending.get("partner_id"):
        await query.answer("Это объявление принадлежит другому пользователю.", show_alert=True)
        return
    if not pending.get("paid"):
        await query.answer("Сначала нужно оплатить публикацию.", show_alert=True)
        return
    if requires_payment_manual_review(context, listing_id, pending):
        await query.answer("Платёж проверяется администратором. Повторная отправка остановлена.", show_alert=True)
        return

    await query.answer()
    await submit_paid_public_listing(context, listing_id, update.effective_user, query.message, test_mode=False)


def mark_telegram_payment_review(context, listing_id, pending, payment, reason):
    pending["payment_review_required"] = True
    pending["payment_review_reason"] = reason
    pending["payment_provider"] = "telegram"
    pending["telegram_payment_charge_id"] = payment.telegram_payment_charge_id
    pending["provider_payment_charge_id"] = payment.provider_payment_charge_id
    save_pending(context, listing_id, pending)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    listing_id = listing_id_from_payment_payload(payment.invoice_payload)
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("source") != "public":
        logger.warning("successful_payment: pending public listing not found")
        orphan_id = listing_id or f"orphan_{payment.telegram_payment_charge_id}"
        save_payment_record(context, orphan_id, {
            "partner_id": update.effective_user.id,
            "provider": "telegram",
            "payment_status": "manual_review",
            "review_reason": "pending_not_found",
            "amount": payment.total_amount,
            "currency": payment.currency,
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id,
            "paid_at": now_iso(),
        })
        await persist_now(context.application)
        await update.message.reply_text(
            "✅ Оплата получена, но сохранённое объявление не найдено.\n\n"
            "Администратор уведомлён. Повторно оплачивать не нужно."
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Telegram подтвердил оплату, но заявка не найдена: listing_id={listing_id}, user_id={update.effective_user.id}",
            )
        except Exception as notify_error:
            logger.error(f"successful_payment: не удалось уведомить администратора: {notify_error}")
        return
    if pending.get("partner_id") != update.effective_user.id:
        logger.warning("successful_payment: user_id mismatch")
        mark_telegram_payment_review(
            context,
            listing_id,
            pending,
            payment,
            "user_id_mismatch",
        )
        save_payment_record(context, listing_id, {
            "partner_id": update.effective_user.id,
            "provider": "telegram",
            "payment_status": "manual_review",
            "review_reason": "user_id_mismatch",
            "amount": payment.total_amount,
            "currency": payment.currency,
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id,
            "paid_at": now_iso(),
        })
        await persist_now(context.application)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Telegram payment user_id mismatch: listing_id={listing_id}, user_id={update.effective_user.id}",
            )
        except Exception as notify_error:
            logger.error(f"successful_payment: не удалось уведомить о user_id mismatch: {notify_error}")
        return
    expected_currency = expected_payment_currency(pending)
    expected_amount = expected_payment_amount(pending)
    if payment.currency != expected_currency or payment.total_amount != expected_amount:
        logger.error(
            f"successful_payment amount mismatch: {payment.currency} {payment.total_amount}, "
            f"expected {expected_currency} {expected_amount}"
        )
        mark_telegram_payment_review(
            context,
            listing_id,
            pending,
            payment,
            "amount_mismatch",
        )
        save_payment_record(context, listing_id, {
            "partner_id": update.effective_user.id,
            "provider": "telegram",
            "payment_status": "manual_review",
            "review_reason": "amount_mismatch",
            "amount": payment.total_amount,
            "currency": payment.currency,
            "expected_amount": expected_amount,
            "expected_currency": expected_currency,
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id,
            "paid_at": now_iso(),
        })
        await persist_now(context.application)
        await update.message.reply_text(
            "✅ Платёж получен, но его параметры требуют ручной проверки.\n\n"
            "Администратор уведомлён. Повторно оплачивать не нужно."
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Telegram payment amount mismatch: listing_id={listing_id}",
            )
        except Exception as notify_error:
            logger.error(f"successful_payment: не удалось уведомить о сумме: {notify_error}")
        return

    pending["paid"] = True
    pending.setdefault("payment_paid_at", now_iso())
    pending["payment_total_amount"] = payment.total_amount
    pending["payment_currency"] = payment.currency
    pending["telegram_payment_charge_id"] = payment.telegram_payment_charge_id
    pending["provider_payment_charge_id"] = payment.provider_payment_charge_id
    save_pending(context, listing_id, pending)
    record_confirmed_payment(
        context,
        listing_id,
        pending,
        "telegram",
        payment.total_amount,
        payment.currency,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        provider_payment_charge_id=payment.provider_payment_charge_id,
    )
    # Telegram может больше не прислать successful_payment после того, как
    # polling update уже обработан. Факт списания должен попасть на диск первым.
    await persist_now(context.application)

    if pending.get("submitted_to_admin"):
        await update.message.reply_text("✅ Оплата уже получена, объявление уже отправлено на проверку.")
        return

    await submit_paid_public_listing(context, listing_id, update.effective_user, update.message, test_mode=False)


def clear_pending_for_user(context, user_id):
    removed = 0
    preserved = 0
    for listing_id, pending in list(list_unique_pending_items(context).items()):
        if pending.get("partner_id") == user_id:
            if pending.get("paid") or pending.get("submitted_to_admin"):
                preserved += 1
                continue
            save_listing_history(context, listing_id, pending, "cancelled", cancellation_reason="admin_clear_pending")
            delete_pending(context, listing_id)
            removed += 1

    for key in (
        f"editing_listing_{user_id}",
        f"photos_{user_id}",
        f"property_type_{user_id}",
        f"published_money_listing_{user_id}",
        f"published_money_field_{user_id}",
    ):
        context.application.bot_data.pop(key, None)
    set_state(context, user_id, "idle")
    return removed, preserved


async def admin_clear_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору")
        return

    removed, preserved = clear_pending_for_user(context, user.id)
    await persist_now(context.application)
    await update.message.reply_text(
        "Очистка завершена.\n\n"
        f"Удалено ваших неоплаченных черновиков: {removed}.\n"
        f"Сохранено оплаченных или уже отправленных: {preserved}.\n\n"
        "Опубликованные объявления и заявки других пользователей не тронуты"
    )


async def admin_retry_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После ручной проверки чата повторяет оборванную доставку одной заявки."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Эта команда доступна только администратору")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /retry_review ID\n\n"
            "Используйте только после проверки, что карточки с этим ID в чате одобрения нет."
        )
        return
    listing_id = str(context.args[0]).strip()
    pending = get_pending(context, listing_id)
    if not pending:
        await update.effective_message.reply_text("Заявка не найдена или уже завершена.")
        return
    if pending.get("review_state") not in {"unknown", "sending"}:
        await update.effective_message.reply_text(
            "У этой заявки нет неопределённой доставки в чат проверки."
        )
        return
    if pending_busy(pending, "submit_in_progress"):
        await update.effective_message.reply_text("Заявка уже отправляется. Подождите.")
        return

    formatted_listing = await prepare_listing_for_caption(
        pending.get("formatted_listing", ""),
        pending.get("contact_url", DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        pending["submitted_to_admin"] = False
        pending["review_state"] = "failed"
        save_pending(context, listing_id, pending)
        await persist_now(context.application)
        await update.effective_message.reply_text(validation_message(issues))
        return

    pending["formatted_listing"] = formatted_listing
    pending["submitted_to_admin"] = True
    pending["review_state"] = "sending"
    pending["review_retry_authorized_at"] = now_iso()
    mark_pending_busy(context, listing_id, pending, "submit_in_progress", "admin_retry")
    await persist_now(context.application)

    submitter_label = pending.get("partner_label") or f"пользователя ID {pending.get('partner_id')}"
    try:
        delivery_issues = await send_pending_to_admin(
            context,
            listing_id,
            pending,
            submitter_label,
            label="admin_retry_review",
            retry_ambiguous=False,
        )
    except Exception as e:
        pending.pop("submit_in_progress", None)
        if is_transient_network_error(e) and not isinstance(e, RetryAfter):
            pending["review_state"] = "unknown"
            message = "Telegram снова не вернул надёжный ответ. Повтор остановлен."
        else:
            pending["submitted_to_admin"] = False
            pending["review_state"] = "failed"
            if pending.get("source") == "public" and pending.get("paid"):
                pending["paid_delivery_needs_fix"] = True
            message = f"Не получилось доставить карточку: {e}"
        pending["review_error"] = str(e)[:300]
        save_pending(context, listing_id, pending)
        await persist_now(context.application)
        await update.effective_message.reply_text(message)
        return

    if delivery_issues:
        pending.pop("submit_in_progress", None)
        pending["submitted_to_admin"] = False
        pending["review_state"] = "failed"
        save_pending(context, listing_id, pending)
        await persist_now(context.application)
        await update.effective_message.reply_text(validation_message(delivery_issues))
        return

    current = get_pending(context, listing_id)
    if current is not pending:
        await update.effective_message.reply_text("Заявка уже была завершена другим действием.")
        return
    pending.pop("submit_in_progress", None)
    pending["review_state"] = "delivered"
    pending.pop("review_error", None)
    save_pending(context, listing_id, pending)
    partner_id = pending.get("partner_id")
    if partner_id is not None:
        set_state(context, partner_id, "submitted")
    payment_record = get_payment_record(context, listing_id)
    if payment_record:
        save_payment_record(context, listing_id, {"listing_status": "submitted", "submitted_at": now_iso()})
    await persist_now(context.application)
    await update.effective_message.reply_text(
        f"Карточка {listing_id} повторно доставлена в чат проверки."
    )


async def admin_confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После проверки провайдера подтверждает платёж из ручной очереди."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Эта команда доступна только администратору")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /confirm_payment ID\n\n"
            "Используйте только после проверки суммы, валюты и плательщика у платёжного провайдера."
        )
        return
    listing_id = str(context.args[0]).strip()
    pending = get_pending(context, listing_id)
    record = get_payment_record(context, listing_id)
    if not pending or pending.get("source") != "public":
        await update.effective_message.reply_text("Public-заявка не найдена или уже завершена.")
        return
    if not isinstance(record, dict) or record.get("payment_status") != "manual_review":
        await update.effective_message.reply_text("У этого объявления нет платежа на ручной проверке.")
        return

    amount = record.get("payment_total_amount")
    if amount is None:
        amount = record.get("amount")
    currency = record.get("payment_currency") or record.get("currency")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        await update.effective_message.reply_text("В журнале нет корректной суммы — подтвердить автоматически нельзя.")
        return
    currency = str(currency or "").upper()
    if not currency:
        await update.effective_message.reply_text("В журнале нет валюты — подтвердить автоматически нельзя.")
        return

    pending["paid"] = True
    pending["payment_paid_at"] = record.get("payment_paid_at") or record.get("paid_at") or now_iso()
    pending["payment_total_amount"] = amount
    pending["payment_currency"] = currency
    pending["payment_provider"] = record.get("provider") or "manual"
    for field in (
        "stripe_session_id",
        "stripe_payment_intent",
        "telegram_payment_charge_id",
        "provider_payment_charge_id",
    ):
        if record.get(field):
            pending[field] = record[field]
    pending.pop("payment_review_required", None)
    pending.pop("payment_review_reason", None)
    pending.pop("paid_delivery_needs_fix", None)
    save_pending(context, listing_id, pending)
    record_confirmed_payment(
        context,
        listing_id,
        pending,
        record.get("provider") or "manual",
        amount,
        currency,
        stripe_session_id=record.get("stripe_session_id"),
        stripe_payment_intent=record.get("stripe_payment_intent"),
        telegram_payment_charge_id=record.get("telegram_payment_charge_id"),
        provider_payment_charge_id=record.get("provider_payment_charge_id"),
    )
    save_payment_record(context, listing_id, {
        "manual_review_resolution": "confirmed_by_admin",
        "manual_review_resolved_at": now_iso(),
        "manual_review_resolved_by": user.id,
    })
    await persist_now(context.application)
    schedule_paid_delivery(context.application, listing_id)
    await update.effective_message.reply_text(
        f"Платёж {listing_id} подтверждён. Заявка поставлена в безопасную доставку администратору."
    )


async def admin_retry_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После ручной проверки канала разрешает один повтор неизвестной публикации."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Эта команда доступна только администратору")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /retry_publish ID\n\nИспользуйте только после ручной проверки, что поста с этой заявкой в канале нет."
        )
        return
    listing_id = str(context.args[0]).strip()
    pending = get_pending(context, listing_id)
    if not pending:
        await update.effective_message.reply_text("Заявка не найдена или уже завершена.")
        return
    if pending.get("publish_state") not in {"unknown", "sending"}:
        await update.effective_message.reply_text("У этой заявки нет неопределённой попытки публикации.")
        return
    pending["publish_state"] = "retry_authorized"
    pending["publish_retry_authorized_at"] = now_iso()
    pending.pop("publish_error", None)
    pending.pop("admin_action_in_progress", None)
    save_pending(context, listing_id, pending)
    await persist_now(context.application)
    await update.effective_message.reply_text(
        f"Повтор разрешён для {listing_id}. Теперь нажмите «Опубликовать» под сохранённой карточкой."
    )


def stats_employee_code(item):
    """Возвращает сотрудника, который был сохранён у объявления.

    Для старых записей без employee_code используем только однозначный
    контакт. DEFAULT_CONTACT намеренно не угадываем: он совпадает со старой
    ссылкой Ивана и не позволяет достоверно восстановить автора перехода.
    """
    code = canonical_employee_code(item.get("employee_code"))
    if code in EMPLOYEES:
        return code
    if item.get("source") == "partner":
        contact_url = item.get("contact_url")
        if contact_url and contact_url != DEFAULT_CONTACT:
            code = canonical_employee_code(employee_key_by_contact(contact_url))
            if code in EMPLOYEES:
                return code
    return None


def stats_revenue_czk(item):
    """Реальная выручка по объявлению; тестовые оплаты не считаются."""
    if item.get("source") != "public" or not item.get("paid") or item.get("payment_test_mode"):
        return 0.0
    payment_status = str(item.get("payment_status") or "").lower()
    if (
        payment_status == "refunded"
        or ("dispute" in payment_status and payment_status != "dispute_won")
        or payment_status in {"chargeback", "failed"}
    ):
        return 0.0
    amount = item.get("payment_total_amount")
    try:
        if amount is not None:
            amount = float(amount)
            if payment_status == "partially_refunded":
                amount -= float(item.get("stripe_amount_refunded") or 0)
            return max(0.0, amount / 100.0)
    except (TypeError, ValueError):
        pass
    # Старые записи не хранили сумму, но создавались по фиксированной цене.
    return float(PUBLIC_LISTING_PRICE_CZK)


def stats_all_items(context):
    """Единый список объявлений без двойного счёта pending/history/payment/published."""
    items = {}

    for key, history in context.application.bot_data.items():
        if not key.startswith("history_listing_") or not isinstance(history, dict):
            continue
        listing_id = history.get("listing_id") or key.replace("history_listing_", "", 1)
        row = dict(history)
        row["listing_id"] = listing_id
        items[listing_id] = row

    for listing_id, pending in list_unique_pending_items(context).items():
        row = items.setdefault(listing_id, {"listing_id": listing_id})
        row.update(pending)
        row["listing_id"] = listing_id

    for key, published in context.application.bot_data.items():
        if not key.startswith("published_listing_") or not isinstance(published, dict):
            continue
        listing_id = published.get("listing_id") or key.replace("published_listing_", "", 1)
        row = items.setdefault(listing_id, {"listing_id": listing_id})
        for field, value in published.items():
            if value is not None:
                row[field] = value

    for key, payment in context.application.bot_data.items():
        if not key.startswith("payment_record_") or not isinstance(payment, dict):
            continue
        listing_id = payment.get("listing_id") or key.replace("payment_record_", "", 1)
        row = items.setdefault(listing_id, {"listing_id": listing_id, "source": "public"})
        for field, value in payment.items():
            if value is not None:
                row[field] = value
    return list(items.values())


def stats_user_items(context, user_id):
    return [item for item in stats_all_items(context) if item.get("partner_id") == user_id]


def stats_employee_items(context, employee_code):
    """Объявления партнёров, пришедших по конкретной ссылке сотрудника."""
    if employee_code not in EMPLOYEES:
        return []
    return [
        item
        for item in stats_all_items(context)
        if item.get("source", "partner") == "partner"
        and item.get("partner_id") != ADMIN_TELEGRAM_ID
        and stats_employee_code(item) == employee_code
    ]


def stats_employee_linked_user_ids(context, employee_code, exclude_user_id=None):
    """Пользователи, у которых сохранена привязка к ссылке сотрудника.

    Считаем и тех, кто ещё не успел создать объявление. Текущий пользователь
    исключается, чтобы сотрудник не считался сам себе привлечённым партнёром.
    """
    employee_code = canonical_employee_code(employee_code)
    if employee_code not in EMPLOYEES:
        return set()
    excluded = str(exclude_user_id) if exclude_user_id is not None else None
    result = set()
    prefix = "partner_code_"
    for key, value in context.application.bot_data.items():
        if not key.startswith(prefix) or canonical_employee_code(value) != employee_code:
            continue
        user_id = key[len(prefix):]
        if user_id and user_id != excluded and user_id != str(ADMIN_TELEGRAM_ID):
            result.add(user_id)
    return result


def stats_employee_rows(context):
    rows = {
        key: {
            "partners": set(),
            "listings": set(),
            "submitted": 0,
            "published": 0,
        }
        for key in EMPLOYEE_CHOICE_KEYS
    }
    for item in stats_all_items(context):
        if item.get("source") != "partner" or item.get("partner_id") == ADMIN_TELEGRAM_ID:
            continue
        code = stats_employee_code(item)
        if code not in rows:
            continue
        row = rows[code]
        partner_id = item.get("partner_id")
        if partner_id is not None:
            row["partners"].add(partner_id)
        listing_id = item.get("listing_id")
        if listing_id:
            row["listings"].add(listing_id)
        if item.get("submitted_to_admin") or item.get("published_at"):
            row["submitted"] += 1
        if item.get("published_at"):
            row["published"] += 1
    return rows


def stats_percent(numerator, denominator):
    if not denominator:
        return "—"
    return f"{(100 * numerator / denominator):.0f}%"


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Личная статистика доступна любому пользователю, но только по его ID."""
    user = update.effective_user
    if not user:
        return
    if is_admin(user.id):
        # Для администратора /mystats — удобное имя общей статистики.
        await admin_stats(update, context)
        return
    items = stats_user_items(context, user.id)
    partner_items = [item for item in items if item.get("source", "partner") == "partner"]
    public_items = [item for item in items if item.get("source") == "public"]
    submitted = sum(1 for item in items if item.get("submitted_to_admin") or item.get("published_at"))
    published = sum(1 for item in items if item.get("published_at"))
    active = sum(
        1
        for item in items
        if item.get("published_at")
        and item.get("status", "active") == "active"
        and not item.get("channel_missing")
    )
    rented = sum(1 for item in items if item.get("status") == "rented")
    rejected = sum(1 for item in items if item.get("history_status") == "rejected" or item.get("listing_status") == "rejected")
    paid = sum(1 for item in public_items if item.get("paid") and not item.get("payment_test_mode"))
    test_paid = sum(1 for item in public_items if item.get("payment_test_mode"))
    public_published = sum(
        1 for item in public_items
        if item.get("published_at") and item.get("paid") and not item.get("payment_test_mode")
    )
    revenue = sum(stats_revenue_czk(item) for item in public_items)

    linked_employee_code = canonical_employee_code(
        context.application.bot_data.get(f"partner_code_{user.id}")
    )
    if linked_employee_code not in EMPLOYEES:
        linked_employee_code = None
    employee_owner_code = EMPLOYEE_STATS_OWNER_BY_ID.get(str(user.id))
    employee_code = employee_owner_code or linked_employee_code
    employee_line = ""
    employee_team_block = ""
    if employee_code:
        employee_line = f"\nВаша ссылка сотрудника: <b>{html.escape(employee_display_name(employee_code))}</b>\n"
    if employee_owner_code:
        employee_items = [
            item
            for item in stats_employee_items(context, employee_owner_code)
            if str(item.get("partner_id")) != str(user.id)
        ]
        linked_users = stats_employee_linked_user_ids(context, employee_owner_code, user.id)
        employee_submitted = sum(
            1 for item in employee_items if item.get("submitted_to_admin") or item.get("published_at")
        )
        employee_published = sum(1 for item in employee_items if item.get("published_at"))
        employee_active = sum(
            1
            for item in employee_items
            if (
                item.get("published_at")
                and item.get("status", "active") == "active"
                and not item.get("channel_missing")
            )
        )
        employee_team_block = (
            "\n<b>По вашей ссылке:</b>\n"
            f"— пользователей: {len(linked_users)}\n"
            f"— объявлений: {len(employee_items)}\n"
            f"— отправлено на проверку: {employee_submitted}\n"
            f"— опубликовано: {employee_published}\n"
            f"— активно в канале: {employee_active}\n"
        )

    text = (
        "<b>Моя статистика Binio</b>\n"
        f"{employee_line}\n"
        "<b>Ваши объявления:</b>\n"
        f"— всего сохранённых: {len(items)}\n"
        f"— партнёрских: {len(partner_items)}\n"
        f"— собственника: {len(public_items)}\n"
        f"— отправлено на проверку: {submitted}\n"
        f"— опубликовано: {published}\n"
        f"— активно в канале: {active}\n"
        f"— отмечено «сдано»: {rented}\n"
        f"— отклонено: {rejected}\n\n"
        "<b>Оплата:</b>\n"
        f"— подтверждено оплат: {paid}\n"
        f"— тестовых оплат: {test_paid}\n"
        f"— сумма реальных оплат: {revenue:.0f} Kč\n"
        f"— публикация после оплаты: {stats_percent(public_published, paid)}\n\n"
        f"Лимит собственника в этом месяце: {public_monthly_count(context, user.id)}/{PUBLIC_MONTHLY_LIMIT}\n\n"
        f"{employee_team_block}"
        "Это только ваши данные. Общая статистика доступна администратору."
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору")
        return

    removed = cleanup_bot_memory(context.application.bot_data)
    pending_items = list(list_unique_pending_items(context).values())
    published_items = [
        value for key, value in context.application.bot_data.items()
        if key.startswith("published_listing_") and isinstance(value, dict)
    ]

    pending_total = len(pending_items)
    pending_submitted = sum(1 for item in pending_items if item.get("submitted_to_admin"))
    pending_drafts = pending_total - pending_submitted
    pending_legacy = sum(1 for item in pending_items if "source" not in item or "submitted_to_admin" not in item)
    pending_partner = sum(1 for item in pending_items if item.get("source", "partner") == "partner")
    pending_public = sum(1 for item in pending_items if item.get("source") == "public")
    pending_paid = sum(1 for item in pending_items if item.get("source") == "public" and item.get("paid"))
    pending_test_paid = sum(1 for item in pending_items if item.get("payment_test_mode"))

    published_total = len(published_items)
    published_active = sum(
        1
        for item in published_items
        if item.get("status", "active") == "active" and not item.get("channel_missing")
    )
    published_rented = sum(1 for item in published_items if item.get("status") == "rented")
    published_removed = sum(1 for item in published_items if item.get("status") == "removed")
    published_missing = sum(1 for item in published_items if item.get("channel_missing"))
    published_partner_ids = {
        item.get("partner_id")
        for item in published_items
        if item.get("partner_id") is not None
    }

    all_stats_items = stats_all_items(context)
    employee_rows = stats_employee_rows(context)
    real_paid_total = sum(
        1 for item in all_stats_items
        if item.get("source") == "public" and item.get("paid") and not item.get("payment_test_mode")
    )
    real_revenue_total = sum(stats_revenue_czk(item) for item in all_stats_items)
    public_published_total = sum(
        1 for item in all_stats_items
        if (
            item.get("source") == "public"
            and item.get("published_at")
            and item.get("paid")
            and not item.get("payment_test_mode")
        )
    )
    rejected_total = sum(
        1 for item in all_stats_items
        if item.get("history_status") == "rejected" or item.get("listing_status") == "rejected"
    )
    manual_review_total = sum(1 for item in all_stats_items if item.get("payment_status") == "manual_review")
    refunded_total = sum(1 for item in all_stats_items if item.get("payment_status") == "refunded")
    disputed_total = sum(1 for item in all_stats_items if "dispute" in str(item.get("payment_status") or "").lower())
    employee_lines = []
    for employee_key in EMPLOYEE_CHOICE_KEYS:
        row = employee_rows[employee_key]
        employee_lines.append(
            f"— {html.escape(employee_display_name(employee_key))}: "
            f"партнёры {len(row['partners'])}, объявления {len(row['listings'])}, "
            f"на проверке {row['submitted']}, публикации {row['published']}"
        )

    partner_ids = set()
    for key, value in context.application.bot_data.items():
        if key.startswith("partner_code_") and canonical_employee_code(value) in EMPLOYEES:
            partner_ids.add(key.replace("partner_code_", "", 1))
        elif key.startswith("contact_") and value in set(EMPLOYEES.values()) and value != DEFAULT_CONTACT:
            partner_ids.add(key.replace("contact_", "", 1))

    stripe_status = stripe_configuration_status()
    telegram_payment_status = "подключена" if PAYMENT_PROVIDER_TOKEN else "не подключена"
    stripe_webhook_url = f"{PUBLIC_BASE_URL}/stripe-webhook" if PUBLIC_BASE_URL else "не задан"
    test_mode_status = "включён" if PUBLIC_PAYMENT_TEST_MODE else "выключен"
    storage_status = "Volume подключён" if BOT_DATA_PATH.startswith("/data") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") else "локальная папка сервиса"
    updated_at = datetime.now().strftime("%d.%m.%Y %H:%M")

    text = (
        "<b>Статистика Binio</b>\n\n"
        "<b>Заявки:</b>\n"
        f"— сохранено в памяти бота: {pending_total}\n"
        f"— помечены как отправленные на проверку: {pending_submitted}\n"
        f"— черновики/предпросмотры: {pending_drafts}\n"
        f"— старые тестовые записи: {pending_legacy}\n"
        f"— партнёрские: {pending_partner}\n"
        f"— собственники/разовые: {pending_public}\n"
        f"— оплаченные разовые: {pending_paid}\n"
         f"— тестовые оплаты: {pending_test_paid}\n\n"
        "<b>Точные показатели по сохранённым объявлениям:</b>\n"
        f"— уникальных объявлений: {len(all_stats_items)}\n"
        f"— подтверждённых реальных оплат: {real_paid_total}\n"
        f"— реальная выручка: {real_revenue_total:.0f} Kč\n"
        f"— конверсия оплата → публикация: {stats_percent(public_published_total, real_paid_total)}\n\n"
        f"— отклонённых заявок: {rejected_total}\n"
        f"— платежей на ручной проверке: {manual_review_total}\n"
        f"— возвратов: {refunded_total}\n"
        f"— споров/чарджбэков: {disputed_total}\n\n"
        "<b>Опубликованные объявления партнёров:</b>\n"
        f"— всего объявлений: {published_total}\n"
        f"— активные: {published_active}\n"
        f"— сдано: {published_rented}\n"
        f"— снято: {published_removed}\n"
        f"— отсутствуют в канале: {published_missing}\n"
        f"— авторов объявлений: {len(published_partner_ids)}\n\n"
        "<b>Партнёры:</b>\n"
         f"— с привязкой к сотруднику: {len(partner_ids)}\n\n"
        "<b>По сотрудникам:</b>\n"
        + "\n".join(employee_lines)
        + "\n\n"
        "<b>Оплата:</b>\n"
        f"— Stripe Checkout: {stripe_status}\n"
        f"— Stripe webhook: {html.escape(stripe_webhook_url)}\n"
        f"— Telegram-оплата: {telegram_payment_status}\n"
        f"— тестовый режим: {test_mode_status}\n\n"
        "<b>Хранение данных:</b>\n"
        f"— файл памяти: {html.escape(BOT_DATA_PATH)}\n"
        f"— режим: {storage_status}\n\n"
        "<b>Автоочистка памяти:</b>\n"
        f"— черновики удаляются через: {BOT_DRAFT_TTL_DAYS} дней\n"
        f"— заявки на проверке хранятся: {BOT_SUBMITTED_TTL_DAYS} дней\n"
        f"— временные шаги пользователя: {BOT_TRANSIENT_TTL_DAYS} дней\n"
        f"— сейчас очищено: {cleanup_summary_text(removed)}\n\n"
         f"Обновлено: {updated_at}\n\n"
        "Статистика строится по сохранённым данным; старые очищенные записи в неё не входят.\n"
        "Если после тестов остались лишние черновики, используйте /clearpending"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def admin_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору")
        return

    removed = cleanup_bot_memory(context.application.bot_data)
    try:
        await context.application.update_persistence()
    except Exception as e:
        logger.warning(f"Не удалось сразу сохранить очищенную память: {e}")

    bot_data = dict(context.application.bot_data)
    user_data = dict(context.application.user_data)
    chat_data = dict(context.application.chat_data)
    categories, largest = memory_breakdown(bot_data)

    bot_data_size = rough_pickle_size(bot_data)
    user_data_size = rough_pickle_size(user_data)
    chat_data_size = rough_pickle_size(chat_data)
    disk_size = safe_file_size(BOT_DATA_PATH)
    data_dir_size, data_dir_files = data_dir_usage(BOT_DATA_DIR)
    updated_at = datetime.now().strftime("%d.%m.%Y %H:%M")

    category_lines = []
    for category, info in sorted(categories.items(), key=lambda item: item[1]["bytes"], reverse=True):
        category_lines.append(
            f"— {html.escape(category)}: {format_bytes(info['bytes'])} / {info['count']} шт."
        )
    if not category_lines:
        category_lines.append("— данных пока нет")

    largest_lines = []
    for item_size, key in largest:
        largest_lines.append(f"— {html.escape(key)}: {format_bytes(item_size)}")
    if not largest_lines:
        largest_lines.append("— крупных записей нет")

    disk_text = format_bytes(disk_size) if disk_size is not None else "файл пока не найден"
    data_dir_text = format_bytes(data_dir_size) if data_dir_size is not None else "папка недоступна"
    data_file_lines = []
    for item_size, rel_path in data_dir_files:
        data_file_lines.append(f"— {html.escape(rel_path)}: {format_bytes(item_size)}")
    if not data_file_lines:
        data_file_lines.append("— отдельных файлов не найдено")

    text = (
        "<b>Память бота</b>\n\n"
        "<b>Размеры:</b>\n"
        f"— файл на Railway Volume: {disk_text}\n"
        f"— вся папка памяти: {data_dir_text}\n"
        f"— bot_data сейчас: {format_bytes(bot_data_size)}\n"
        f"— user_data сейчас: {format_bytes(user_data_size)}\n"
        f"— chat_data сейчас: {format_bytes(chat_data_size)}\n\n"
        "<b>Оплата:</b>\n"
        f"— Stripe Checkout: {html.escape(stripe_configuration_status())}\n"
        f"— Stripe webhook: {html.escape(f'{PUBLIC_BASE_URL}/stripe-webhook' if PUBLIC_BASE_URL else 'не задан')}\n\n"
        "<b>Файлы в папке памяти:</b>\n"
        + "\n".join(data_file_lines) +
        "\n\n"
        "<b>Что занимает bot_data:</b>\n"
        + "\n".join(category_lines) +
        "\n\n<b>Самые крупные записи:</b>\n"
        + "\n".join(largest_lines) +
        "\n\n<b>Очистка:</b>\n"
        f"— сейчас очищено: {cleanup_summary_text(removed)}\n\n"
        "Фото как файлы здесь не хранятся. Если файл на диске больше, чем данные сейчас, "
        "он обычно уменьшится после сохранения очищенной памяти.\n\n"
        f"Обновлено: {updated_at}"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def admin_sync_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору")
        return
    existing = CHANNEL_FULL_SYNC_TASK
    if existing is not None and not existing.done():
        await update.message.reply_text("Синхронизация канала уже выполняется. Повторно запускать её не нужно.")
        return
    task = schedule_full_channel_sync(context.application, user.id)
    if task is None:
        await update.message.reply_text("Фоновая синхронизация временно недоступна. Попробуйте ещё раз позже.")
        return
    await update.message.reply_text(
        "🔄 Полная синхронизация канала запущена в фоне.\n"
        "Я пришлю отчёт после проверки всех опубликованных объявлений."
    )


async def partner_my_listings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page=0,
    notice=None,
    answer_query=True,
    filter_key="all",
):
    user_id = update.effective_user.id
    filter_key = normalize_published_filter(filter_key)
    context.application.bot_data[f"published_filter_{user_id}"] = filter_key
    all_listings = list_partner_published(context, user_id, include_hidden=True)
    listings = filter_published_listings(all_listings, filter_key)
    counts = published_filter_counts(all_listings)
    schedule_channel_auto_sync(context.application, user_id)
    target = update.callback_query.message if update.callback_query else update.message

    total_pages = max(1, (len(listings) + PUBLISHED_LISTINGS_PAGE_SIZE - 1) // PUBLISHED_LISTINGS_PAGE_SIZE)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, total_pages - 1))

    if update.callback_query and answer_query:
        await update.callback_query.answer()

    if not listings:
        if all_listings:
            text = (
                f"Мои объявления · {PUBLISHED_LISTING_FILTERS[filter_key]}\n\n"
                "В этом разделе пока нет объявлений. Выберите другой фильтр или создайте новую публикацию."
            )
        else:
            text = (
                "У вас пока нет опубликованных объявлений.\n\n"
                "Когда объявление пройдёт проверку и появится в канале, оно будет доступно здесь."
            )
        keyboard = published_list_keyboard([], 0, filter_key, counts)
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
                return
            except Exception:
                pass
        await target.reply_text(text, reply_markup=keyboard)
        return

    notice_prefix = f"{notice}\n\n" if notice else ""
    text = notice_prefix + (
        f"Мои объявления · {PUBLISHED_LISTING_FILTERS[filter_key]}\n"
        f"В разделе: {len(listings)}\n"
        f"Основные: {counts['all']} · Архив: {counts['archive']}\n"
        f"Страница {page + 1} из {total_pages}\n\n"
        "Выберите объявление, чтобы открыть управление.\n\n"
        "Внутри можно отметить объект как сданный или изменить цену, залог и комиссию.\n"
        + (
            "В архиве карточки скрыты только из основного списка; посты, память и статистика сохраняются."
            if filter_key == "archive"
            else "Проверка наличия постов в канале выполняется автоматически."
        )
    )
    keyboard = published_list_keyboard(listings, page, filter_key, counts)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
            return
        except Exception:
            pass
    await target.reply_text(text, reply_markup=keyboard)


async def partner_apply_money_update(update, context, new_value):
    user_id = update.effective_user.id
    listing_id = context.application.bot_data.get(f"published_money_listing_{user_id}")
    field_key = context.application.bot_data.get(f"published_money_field_{user_id}")
    item = get_published(context, listing_id) if listing_id else None
    field = get_financial_field(field_key)

    if not item or item.get("partner_id") != user_id or not field:
        context.application.bot_data.pop(f"published_money_listing_{user_id}", None)
        context.application.bot_data.pop(f"published_money_field_{user_id}", None)
        set_state(context, user_id, "done")
        await update.message.reply_text("Объявление не найдено\n\nОткройте /mylistings и попробуйте ещё раз")
        return

    new_value, validation_error = validate_financial_value(field_key, new_value)
    if validation_error:
        await update.message.reply_text(
            validation_error + f"\n\nПоле: «{field['label']}». Например: 20 000 Kč"
        )
        return

    base_listing = replace_financial_line(item.get("listing", ""), field_key, new_value)
    visible_listing = listing_with_status(base_listing, item.get("status", "active"))
    if len(visible_listing) > TELEGRAM_CAPTION_LIMIT:
        await update.message.reply_text(
            "После изменения объявление не помещается в подпись Telegram. "
            "Сократите основной текст через администратора и повторите."
        )
        return

    try:
        await edit_published_channel_posts(context, item, visible_listing)
    except Exception as e:
        logger.error(f"partner_apply_money_update error: {e}")
        await update.message.reply_text("Не получилось обновить пост в канале\n\nПопробуйте позже")
        return

    item["listing"] = base_listing
    item["visible_listing"] = visible_listing
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_published(context, listing_id, item)
    await persist_now(context.application)

    context.application.bot_data.pop(f"published_money_listing_{user_id}", None)
    context.application.bot_data.pop(f"published_money_field_{user_id}", None)
    set_state(context, user_id, "done")
    await update.message.reply_text("Готово\n\nЗначение обновлено в объявлении")


async def edit_channel_listing_status(context, item, status):
    visible_listing = listing_with_status(item.get("listing", ""), status)
    await edit_published_channel_posts(context, item, visible_listing)

    item["status"] = status
    item["visible_listing"] = visible_listing
    item["status_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_published(context, item["listing_id"], item)
    await persist_now(context.application)


async def partner_published_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data

    if data.startswith("my_listings_check_"):
        try:
            page = int(data.replace("my_listings_check_", "", 1))
        except ValueError:
            page = 0
        listings = list_partner_published(context, user_id)
        total_pages = max(1, (len(listings) + PUBLISHED_LISTINGS_PAGE_SIZE - 1) // PUBLISHED_LISTINGS_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        await query.answer("Проверяю посты этой страницы…")
        missing, unknown = await verify_published_page(context, listings, page, archive_missing=True)
        if missing:
            notice = (
                f"Проверка завершена: {missing} объявлений больше нет в канале, они перемещены в архив.\n"
                "История и статистика сохранены."
            )
        elif unknown:
            notice = "Не все посты удалось проверить из-за временной недоступности Telegram. Попробуйте позже."
        else:
            notice = "Проверка завершена: посты этой страницы доступны в канале."
        await partner_my_listings(
            update,
            context,
            page=page,
            notice=notice,
            answer_query=False,
            filter_key=current_published_filter(context, user_id),
        )
        return

    if data.startswith("my_listings_filter_"):
        filter_key = normalize_published_filter(data.replace("my_listings_filter_", "", 1))
        await query.answer()
        await partner_my_listings(update, context, page=0, filter_key=filter_key)
        return

    if data == "my_listings_new":
        if get_state(context, user_id) == "processing":
            await query.answer("Текущее объявление ещё обрабатывается. Дождитесь результата.", show_alert=True)
            return
        await query.answer()
        set_state(context, user_id, "choosing_role")
        await send_role_choice(query.message)
        return

    if data == "my_listings" or data.startswith("my_listings_page_"):
        page = 0
        filter_key = current_published_filter(context, user_id)
        if data.startswith("my_listings_page_"):
            rest = data.replace("my_listings_page_", "", 1)
            parts = rest.rsplit("_", 1)
            try:
                if len(parts) == 2 and parts[0] in PUBLISHED_LISTING_FILTERS:
                    filter_key = parts[0]
                    page = int(parts[1])
                else:
                    # Старые кнопки имели формат my_listings_page_N.
                    page = int(rest)
            except ValueError:
                page = 0
        await partner_my_listings(update, context, page=page, filter_key=filter_key)
        return

    if data.startswith("pub_archive_info_"):
        listing_id = data.replace("pub_archive_info_", "", 1)
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return
        if item.get("channel_missing"):
            await query.answer(
                "Пост больше не найден в канале. Запись сохранена в архиве, чтобы не потерять историю.",
                show_alert=True,
            )
        else:
            await query.answer(
                "Запись находится в архиве и не показывается в основном списке.",
                show_alert=True,
            )
        return

    if data.startswith("pub_archive_") or data.startswith("pub_unarchive_"):
        archive = data.startswith("pub_archive_")
        prefix = "pub_archive_" if archive else "pub_unarchive_"
        listing_id = data.replace(prefix, "", 1)
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return
        filtered_listings, filter_key = current_filtered_published_listings(context, user_id)
        list_page = published_page_for_listing(filtered_listings, listing_id)
        if archive:
            item["hidden_from_list"] = True
            item["hidden_at"] = now_iso()
            item["hidden_reason"] = "manual"
            notice = "Объявление перемещено в архив. Канал, статистика и память не изменены."
            answer = "Перемещено в архив"
        else:
            if item.get("channel_missing") or item.get("status") == "removed":
                await query.answer(
                    "Эта запись остаётся в архиве: её пост отсутствует или снят.",
                    show_alert=True,
                )
                return
            item.pop("hidden_from_list", None)
            item.pop("hidden_at", None)
            item.pop("hidden_reason", None)
            notice = "Объявление возвращено в основной список. Канал, статистика и память сохранены."
            answer = "Возвращено в список"
        save_published(context, listing_id, item)
        await persist_now(context.application)
        await query.answer(answer)
        await partner_my_listings(
            update,
            context,
            page=list_page,
            notice=notice,
            answer_query=False,
            filter_key=filter_key,
        )
        return

    if data.startswith("pub_hide_"):
        listing_id = data.replace("pub_hide_", "", 1)
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return
        if not item.get("channel_missing"):
            await query.answer("Сначала проверьте страницу канала.", show_alert=True)
            return
        filtered_listings, filter_key = current_filtered_published_listings(context, user_id)
        list_page = published_page_for_listing(filtered_listings, listing_id)
        item["hidden_from_list"] = True
        item["hidden_at"] = now_iso()
        item["hidden_reason"] = "channel_missing"
        save_published(context, listing_id, item)
        await persist_now(context.application)
        await query.answer("Убрано из списка")
        await partner_my_listings(
            update,
            context,
            page=list_page,
            notice="Объявление убрано только из списка. В статистике и памяти оно сохранено.",
            answer_query=False,
            filter_key=filter_key,
        )
        return

    if data.startswith("pub_view_"):
        listing_id = data.replace("pub_view_", "", 1)
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return

        await query.answer()
        filtered_listings, filter_key = current_filtered_published_listings(context, user_id)
        list_page = published_page_for_listing(filtered_listings, listing_id)
        text = published_card_text(item)
        try:
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(item, list_page, filter_key),
            )
        except Exception:
            await query.message.reply_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(item, list_page, filter_key),
            )
        return

    if data.startswith("pub_money_"):
        rest = data.replace("pub_money_", "", 1)
        parts = rest.split("_", 1)
        if len(parts) != 2:
            await query.answer("Не удалось понять действие.", show_alert=True)
            return
        field_key, listing_id = parts
        field = get_financial_field(field_key)
        item = get_published(context, listing_id)
        if not field or not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return
        if item.get("channel_missing") or item.get("status") == "removed":
            await query.answer("Пост отсутствует в канале, это объявление доступно только в архиве.", show_alert=True)
            return
        if item.get("status") == "rented":
            await query.answer("Сначала верните объявление в активные.", show_alert=True)
            return

        context.application.bot_data[f"published_money_listing_{user_id}"] = listing_id
        context.application.bot_data[f"published_money_field_{user_id}"] = field_key
        set_state(context, user_id, "published_money_edit")
        await query.answer()
        await query.message.reply_text(
            f"Введите новое значение для поля «{field['label']}».\n\n"
            "Например: 20 000 Kč"
        )
        return

    if data.startswith("pub_rented_") or data.startswith("pub_active_"):
        status = "rented" if data.startswith("pub_rented_") else "active"
        listing_id = data.split("_", 2)[2]
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return

        if item.get("channel_missing") or item.get("status") == "removed":
            await query.answer("Пост отсутствует в канале, сначала опубликуйте объявление заново.", show_alert=True)
            return

        if item.get("status") == status:
            await query.answer("Статус уже такой", show_alert=True)
            return

        await query.answer("Обновляю пост в канале")
        try:
            await edit_channel_listing_status(context, item, status)
        except Exception as e:
            logger.error(f"partner_published_callback status update error: {e}")
            await query.message.reply_text(
                "⚠️ Не получилось изменить пост в канале\n\nВозможно, Telegram не дал отредактировать старый пост"
            )
            return

        updated = get_published(context, listing_id)
        filtered_listings, filter_key = current_filtered_published_listings(context, user_id)
        list_page = published_page_for_listing(filtered_listings, listing_id)
        text = published_card_text(updated)
        try:
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(updated, list_page, filter_key),
            )
        except Exception:
            await query.message.reply_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(updated, list_page, filter_key),
            )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("У вас нет прав для этого действия", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        listing_id = data.split("_", 1)[1]
        pending = get_pending(context, listing_id)
        if not pending:
            await query.message.reply_text("⚠️ Объявление не найдено.")
            return
        if pending.get("source") == "public" and not pending.get("paid"):
            await query.message.reply_text(
                "⚠️ Оплата этой публикации ещё не подтверждена. Публикация заблокирована."
            )
            return
        if not pending.get("submitted_to_admin"):
            await query.message.reply_text(
                "Карточка ещё не завершила отправку на проверку. Подождите несколько секунд и нажмите снова."
            )
            return
        if pending.get("publish_state") in {"unknown", "sending"}:
            await query.message.reply_text(
                "⚠️ Результат предыдущей публикации неизвестен из-за потери ответа Telegram.\n\n"
                "Сначала проверьте последние посты в канале. Если пост уже появился — не нажимайте публикацию повторно."
            )
            return
        if pending_busy(pending, "admin_action_in_progress"):
            await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
            return
        mark_pending_busy(context, listing_id, pending, "admin_action_in_progress", "approve")
        pending["publish_state"] = "sending"
        pending["publish_started_at"] = now_iso()
        save_pending(context, listing_id, pending)
        await persist_now(context.application)

        listing = await prepare_listing_for_caption(
            pending['formatted_listing'],
            pending.get('contact_url', DEFAULT_CONTACT),
            allow_gemini_shortening=False,
        )
        if listing != pending.get('formatted_listing'):
            pending['formatted_listing'] = listing
            save_pending(context, listing_id, pending)
        issues = validate_listing_ready(pending, listing)
        if issues:
            clear_pending_busy(context, listing_id, pending, "admin_action_in_progress")
            pending["publish_state"] = "failed"
            save_pending(context, listing_id, pending)
            await persist_now(context.application)
            await query.message.reply_text(
                validation_message(issues),
                reply_markup=admin_fix_keyboard(listing_id),
            )
            return
        photos = pending['photos']

        try:
            published_message = await send_listing_with_media(
                context.bot,
                CHANNEL_USERNAME,
                listing,
                photos,
                caption_index=0,
                label="approve publish",
                retry_ambiguous=False,
            )
        except Exception as e:
            logger.error(f"approve publish send error: {e}")
            clear_pending_busy(context, listing_id, pending, "admin_action_in_progress")
            if is_transient_network_error(e) and not isinstance(e, RetryAfter):
                pending["publish_state"] = "unknown"
                pending["publish_error"] = str(e)[:300]
                save_pending(context, listing_id, pending)
                await persist_now(context.application)
                await query.message.reply_text(
                    "⚠️ Telegram не вернул надёжный ответ на публикацию.\n\n"
                    f"Автоматический повтор остановлен, чтобы не создать дубль. Проверьте последние посты в канале.\n\n"
                    f"ID заявки: {listing_id}. Если поста точно нет: /retry_publish {listing_id}"
                )
            else:
                pending["publish_state"] = "failed"
                pending["publish_error"] = str(e)[:300]
                save_pending(context, listing_id, pending)
                await persist_now(context.application)
                await query.message.reply_text(f"⚠️ Ошибка публикации: {e}")
            return

        channel_message_id = getattr(published_message, "message_id", None)
        channel_chat_id = getattr(getattr(published_message, "chat", None), "id", CHANNEL_USERNAME)
        partner_id = pending.get('partner_id')
        is_public_paid = pending.get("source") == "public"
        post_url = channel_post_url(CHANNEL_USERNAME, channel_message_id) if channel_message_id else None
        if channel_message_id:
            published_data = {
                "listing_id": listing_id,
                "partner_id": partner_id,
                "partner_label": pending.get("partner_label"),
                "source": pending.get("source", "partner"),
                 "paid": bool(pending.get("paid")),
                 "payment_total_amount": pending.get("payment_total_amount"),
                 "payment_currency": pending.get("payment_currency"),
                 "payment_test_mode": bool(pending.get("payment_test_mode")),
                 "employee_code": pending.get("employee_code"),
                 "payment_paid_at": pending.get("payment_paid_at"),
                "invoice_created_at": pending.get("invoice_created_at"),
                "submitted_at": pending.get("submitted_at"),
                "listing": strip_listing_status(listing),
                "visible_listing": listing,
                "photos": photos,
                "has_photos": bool(photos),
                "contact_url": pending.get("contact_url", DEFAULT_CONTACT),
                "property_type": pending.get("property_type", "other"),
                "channel_message_id": channel_message_id,
                "channel_chat_id": channel_chat_id,
                "channel_messages": [{
                    "chat_id": channel_chat_id,
                    "message_id": channel_message_id,
                    "has_photos": bool(photos),
                }],
                "channel_post_url": post_url,
                "status": "active",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
            save_published(context, listing_id, published_data)
            save_listing_history(context, listing_id, published_data, "published")
        elif not channel_message_id:
            logger.warning(f"Опубликовано, но не удалось сохранить message_id для listing_id={listing_id}")
            pending["publish_state"] = "unknown"
            pending["publish_error"] = "Telegram не вернул message_id"
            clear_pending_busy(context, listing_id, pending, "admin_action_in_progress")
            save_pending(context, listing_id, pending)
            await persist_now(context.application)
            await query.message.reply_text(
                "⚠️ Telegram принял запрос, но не вернул message_id. Проверьте канал; автоматический повтор остановлен."
            )
            return

        payment_record = get_payment_record(context, listing_id)
        if payment_record:
            save_payment_record(context, listing_id, {
                "listing_status": "published",
                "published_at": published_data.get("published_at"),
                "channel_message_id": channel_message_id,
            })
        delete_pending(context, listing_id)
        clear_admin_edit_session(context.application.bot_data, listing_id)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)
        # Пост уже существует во внешнем мире — сразу фиксируем локальную запись
        # до косметического обновления кнопки и уведомления пользователя.
        await persist_now(context.application)

        await update_admin_action_message(query, f"✅ Опубликовано в {CHANNEL_USERNAME}")

        if partner_id is not None and channel_message_id and not is_public_paid:
            try:
                await context.bot.send_message(
                    chat_id=partner_id,
                    text="✅ Ваше объявление опубликовано. Теперь оно доступно в разделе «Мои объявления».",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Мои объявления", callback_data="my_listings")]
                    ])
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить партнёра {partner_id} о публикации: {e}")
        elif partner_id is not None and is_public_paid:
            try:
                text = "✅ Ваше объявление опубликовано."
                if post_url:
                    text += f'\n\n<a href="{html.escape(post_url, quote=True)}">Открыть пост в канале</a>'
                await context.bot.send_message(
                    chat_id=partner_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Мои объявления", callback_data="my_listings")]
                    ]),
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить платного пользователя {partner_id} о публикации: {e}")

    elif data.startswith("edit_more_"):
        listing_id = data.split("_", 2)[2]
        pending = get_pending(context, listing_id)
        if pending:
            bot_data = context.application.bot_data
            bot_data["admin_editing_listing_id"] = listing_id
            bot_data["admin_editing_user_id"] = update.effective_user.id
            bot_data.pop("admin_editing_prompt_message_id", None)
            current_text = pending['formatted_listing']
            await update_admin_action_message(
                query,
                "✏️ Редактирование открыто\n\n"
                "Ниже бот покажет текущий текст и отдельное поле для ответа",
            )
            await send_text_with_fallback(
                context.bot,
                ADMIN_CHAT_ID,
                current_text,
                label="admin edit_more current_text",
            )
            reply_markup = admin_edit_reply_markup(getattr(query.message, "chat", None))
            if reply_markup is None:
                prompt_text = (
                    "Вставьте исправленный текст и отправьте его следующим сообщением в этот канал.\n\n"
                    "Бот примет следующий текстовый пост как новую версию объявления."
                )
            else:
                prompt_text = (
                    "Вставьте исправленный текст в открывшееся поле ответа и отправьте его.\n\n"
                    "Важно: сообщение должно быть ответом именно на эту подсказку — так бот получит его даже при Privacy Mode Telegram."
                )
            prompt_message = await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=prompt_text,
                reply_markup=reply_markup,
            )
            prompt_message_id = getattr(prompt_message, "message_id", None)
            if reply_markup is not None and prompt_message_id is not None:
                bot_data["admin_editing_prompt_message_id"] = prompt_message_id
        else:
            await query.message.reply_text("⚠️ Объявление не найдено.")

    elif data.startswith("reject_"):
        listing_id = data.split("_", 1)[1]
        pending = get_pending(context, listing_id)
        if not pending:
            await query.message.reply_text("⚠️ Объявление не найдено.")
            return
        if pending_busy(pending, "admin_action_in_progress"):
            await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
            return
        mark_pending_busy(context, listing_id, pending, "admin_action_in_progress", "reject")
        partner_id = pending.get('partner_id')
        paid_rejection = pending.get("source") == "public" and pending.get("paid")
        save_listing_history(
            context,
            listing_id,
            pending,
            "rejected",
            paid=bool(pending.get("paid")),
            rejection_reason="admin_rejected",
        )
        payment_record = get_payment_record(context, listing_id)
        if payment_record:
            save_payment_record(context, listing_id, {
                "listing_status": "rejected",
                "rejected_at": now_iso(),
                "refund_status": "manual_review_required" if paid_rejection else "not_applicable",
            })
        delete_pending(context, listing_id)
        clear_admin_edit_session(context.application.bot_data, listing_id)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)
        await persist_now(context.application)
        await update_admin_action_message(
            query,
            "❌ Объявление отклонено."
            + ("\n\nОплата сохранена в журнале; возврат требует ручного решения." if paid_rejection else ""),
        )
        if partner_id is not None:
            try:
                if paid_rejection:
                    reject_text = (
                        "❌ Ваше объявление отклонено администратором.\n\n"
                        "Данные оплаты сохранены. Возврат не выполняется автоматически; "
                        "для решения вопроса свяжитесь с администратором Binio. Повторно оплачивать не нужно."
                    )
                else:
                    reject_text = "❌ Ваше объявление отклонено администратором."
                await context.bot.send_message(chat_id=partner_id, text=reject_text)
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {partner_id} об отклонении: {e}")


async def admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только админ может редактировать текст из чата одобрения."""
    message = update.effective_message
    if not message or not message.text:
        return

    if not is_admin_edit_sender(update):
        logger.warning(
            f"admin_edit: сообщение в чате одобрения от user_id={getattr(update.effective_user, 'id', None)} "
            f"проигнорировано — не совпадает с ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}"
        )
        await message.reply_text(
            "⚠️ Эту правку может отправить только администратор, который открыл редактирование"
        )
        return

    edited_text = message.text

    bot_data = context.application.bot_data
    listing_id = bot_data.get("admin_editing_listing_id")
    pending = get_pending(context, listing_id) if listing_id else None

    if not listing_id or not pending:
        clear_admin_edit_session(bot_data)
        await message.reply_text(
            "⚠️ Непонятно, какое объявление вы редактируете\n\n"
            "Нажмите «Исправить» под нужным объявлением и повторите"
        )
        return

    prompt_message_id = bot_data.get("admin_editing_prompt_message_id")
    reply_to = getattr(message, "reply_to_message", None)
    reply_to_message_id = getattr(reply_to, "message_id", None)
    if prompt_message_id is not None and reply_to_message_id != prompt_message_id:
        await message.reply_text(
            "⚠️ Текст не привязан к режиму редактирования\n\n"
            "Отправьте его через поле ответа под сообщением «Вставьте исправленный текст». "
            "Режим редактирования остаётся открытым."
        )
        return

    editable_listing = prepare_listing_for_editing(
        edited_text,
        pending.get('contact_url', DEFAULT_CONTACT),
    )
    if len(editable_listing) > TELEGRAM_CAPTION_LIMIT:
        await message.reply_text(
            f"⚠️ Текст слишком длинный: {len(editable_listing)} символов при лимите {TELEGRAM_CAPTION_LIMIT}.\n\n"
            "Сократите его и отправьте снова. Режим редактирования остаётся открытым; ничего не обрезано."
        )
        return
    edited_text = await prepare_listing_for_caption(
        editable_listing,
        pending.get('contact_url', DEFAULT_CONTACT),
        allow_gemini_shortening=False,
    )
    pending['formatted_listing'] = edited_text
    pending['editable_listing'] = editable_listing
    save_pending(context, listing_id, pending)
    # Одна кнопка «Исправить» разрешает ровно одно следующее текстовое сообщение.
    # Для повторной правки администратор нажимает «Исправить ещё» в новом предпросмотре.
    clear_admin_edit_session(bot_data, listing_id)
    photos = pending['photos']

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{listing_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{listing_id}")
        ],
        [InlineKeyboardButton("✏️ Исправить ещё", callback_data=f"edit_more_{listing_id}")]
    ])
    preview_text = edited_text

    try:
        await send_listing_with_media(
            context.bot,
            message.chat_id,
            preview_text,
            photos,
            reply_markup=keyboard,
            caption_index=len(photos) - 1,
            label="admin_edit",
        )
    except Exception as e:
        logger.error(f"admin_edit send error: {e}")
        await message.reply_text(f"⚠️ Ошибка отправки исправленной версии: {e}")


async def admin_chat_unrecognized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит в чате одобрения всё, что не является обычным текстом (фото, документы,
    стикеры и т.п.) — чтобы правка, отправленная не тем способом, не терялась молча."""
    if update.effective_user is not None and not is_admin(update.effective_user.id):
        return
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "⚠️ Не получилось распознать это как текст для правки\n\n"
        "Пожалуйста, отправьте исправленный текст обычным текстовым сообщением "
        "без прикреплённых фото или файлов"
    )


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит любые необработанные ошибки, чтобы бот не 'зависал' молча."""
    if isinstance(context.error, Conflict):
        logger.error(
            "Telegram завершил polling из-за второго процесса с тем же токеном. "
            "Оставьте только один Railway-инстанс этого бота."
        )
        return
    if is_transient_network_error(context.error):
        logger.warning(
            "Временная сетевая ошибка Telegram; polling восстановится автоматически: "
            f"{type(context.error).__name__}: {context.error}"
        )
        return

    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)

    # Сообщаем тебе, что что-то пошло не так — но не спамим, если ошибка в самом уведомлении
    try:
        error_text = str(context.error)[:300]
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ В боте партнёров произошла ошибка:\n{error_text}"
        )
    except Exception:
        pass

    # Пытаемся вежливо ответить пользователю, если это возможно
    try:
        is_admin_chat = bool(
            isinstance(update, Update)
            and update.effective_chat
            and update.effective_chat.id == ADMIN_CHAT_ID
        )
        if isinstance(update, Update) and update.effective_message and not is_admin_chat:
            await update.effective_message.reply_text(
                "⚠️ Произошла техническая ошибка\n\nПопробуйте ещё раз через /start"
            )
    except Exception:
        pass


def main():
    validate_config()
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    logger.info(f"Файл памяти бота: {BOT_DATA_PATH}")
    persistence = AtomicPicklePersistence(filepath=BOT_DATA_PATH, update_interval=15)
    app = (
        Application.builder()
        .token(PARTNER_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .post_shutdown(stop_stripe_webhook_server)
        .concurrent_updates(PerUserUpdateProcessor(max_concurrent_updates=MAX_CONCURRENT_UPDATES))
        .persistence(persistence)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # Группа -1 выполняется до обычных обработчиков. Личные сценарии не должны
    # публиковать данные пользователя в группах; служебный чат админа исключён.
    app.add_handler(CallbackQueryHandler(reject_non_private_callback), group=-1)
    app.add_handler(MessageHandler(
        ~filters.ChatType.PRIVATE & ~filters.Chat(ADMIN_CHAT_ID),
        reject_non_private_message,
    ), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("partner", partner_publish_start))
    app.add_handler(CommandHandler("owner", public_publish_start))
    app.add_handler(CommandHandler("publish", public_publish_start))
    app.add_handler(CommandHandler("mylistings", partner_my_listings))
    app.add_handler(CommandHandler("drafts", list_drafts))
    app.add_handler(CommandHandler("cancel", cancel_current_step))
    app.add_handler(CommandHandler("employee", employee_change_start))
    app.add_handler(CommandHandler("mystats", my_stats))
    app.add_handler(CommandHandler("terms", payment_terms))
    app.add_handler(CommandHandler("support", payment_support))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("memory", admin_memory))
    app.add_handler(CommandHandler("clearpending", admin_clear_pending))
    app.add_handler(CommandHandler("retry_review", admin_retry_review))
    app.add_handler(CommandHandler("confirm_payment", admin_confirm_payment))
    app.add_handler(CommandHandler("retry_publish", admin_retry_publish))
    # Скрытая админская команда: ручная полная сверка канала без кнопки в списках.
    app.add_handler(CommandHandler("sync_channel", admin_sync_channel))

    # Фото только от партнёров (не из чата одобрения)
    app.add_handler(MessageHandler(
        filters.PHOTO & ~filters.Chat(ADMIN_CHAT_ID),
        receive_photo
    ))

    # Защита от видео, документов, стикеров
    app.add_handler(MessageHandler(
        (
            filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION | filters.AUDIO
            | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL
            | filters.LOCATION | filters.CONTACT | filters.POLL | filters.Dice.ALL
        ) & ~filters.Chat(ADMIN_CHAT_ID),
        handle_wrong_file
    ))

    # Кнопка "Фото загружены"
    app.add_handler(CallbackQueryHandler(photos_done, pattern="^photos_done$"))

    # Выбор роли при обычном /start
    app.add_handler(CallbackQueryHandler(role_selected, pattern=r"^role_(partner|public)$"))
    app.add_handler(CallbackQueryHandler(employee_selected, pattern=r"^employee_[A-Za-z0-9_]+$"))

    # Личный кабинет партнёра: опубликованные объявления и статус "сдано"
    app.add_handler(CallbackQueryHandler(
        partner_published_callback,
    pattern=r"^(my_listings(?:_page_(?:[0-9]+|(?:all|active|rented|archive)_[0-9]+)|_check_[0-9]+|_filter_(?:all|active|rented|archive)|_new)?|pub_view_[A-Za-z0-9_-]+|pub_hide_[A-Za-z0-9_-]+|pub_archive_info_[A-Za-z0-9_-]+|pub_archive_[A-Za-z0-9_-]+|pub_unarchive_[A-Za-z0-9_-]+|pub_rented_[A-Za-z0-9_-]+|pub_active_[A-Za-z0-9_-]+|pub_money_(price|deposit|commission)_[A-Za-z0-9_-]+)$"
    ))

    # Платная публикация для обычных пользователей
    app.add_handler(CallbackQueryHandler(send_public_invoice, pattern=r"^pay_public_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(test_public_payment, pattern=r"^test_pay_public_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(submit_already_paid_public, pattern=r"^submit_paid_public_[A-Za-z0-9_-]+$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Выбор типа объекта перед описанием
    app.add_handler(CallbackQueryHandler(property_type_selected, pattern=r"^property_type_[A-Za-z0-9_]+$"))

    # Кнопки партнёра
    # Не перехватываем submit_paid_public_*: у платной публикации отдельная
    # проверка подтверждённой оплаты и свой обработчик выше.
    app.add_handler(CallbackQueryHandler(partner_submit, pattern=r"^submit_(?!paid_public_)[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_regenerate, pattern=r"^regen_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_edit_request, pattern=r"^partner_edit_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(resume_draft, pattern=r"^draft_resume_[A-Za-z0-9_-]+$"))

    # Кнопки админа
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(approve|reject|edit_more)_[A-Za-z0-9_-]+$"))

    # Текст из чата одобрения — только от тебя для редактирования
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_CHAT_ID),
        admin_edit
    ))

    # Всё остальное (фото, документы, стикеры и т.п.) в чате одобрения —
    # чтобы правка, отправленная не текстом, не терялась молча
    app.add_handler(MessageHandler(
        filters.Chat(ADMIN_CHAT_ID) & ~filters.TEXT & ~filters.COMMAND,
        admin_chat_unrecognized
    ))

    # Текст от партнёров — единый обработчик для описания и правок
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Chat(ADMIN_CHAT_ID),
        handle_partner_text
    ))

    app.add_error_handler(global_error_handler)

    logger.info("Binio Partner Bot запущен...")
    # Не удаляем сообщения, пришедшие во время короткого деплоя/перезапуска:
    # среди них могут быть подтверждения оплаты и ответы пользователей.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    # Python 3.14 больше не создаёт event loop автоматически для MainThread.
    # python-telegram-bot пока ожидает, что он уже есть перед run_polling().
    if platform.system().lower() == "windows":
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()



