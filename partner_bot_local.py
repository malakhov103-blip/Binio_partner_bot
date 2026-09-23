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
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand, ForceReply
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, PicklePersistence,
    BaseUpdateProcessor, ApplicationHandlerStop
)

# aiohttp нужен для проверки постов в канале и для health-страницы, которую
# опрашивает Railway. Платёжный код (Stripe/Telegram-оплата) вынесен в
# payments_legacy.py и здесь больше не используется.
try:
    import aiohttp
    from aiohttp import web
except Exception:
    aiohttp = None
    web = None

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
BOT_USERNAME = clean_env("BOT_USERNAME", "binio_partner_bot").lstrip("@")
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "partner_bot_data.pickle")
BOT_DATA_PATH = os.path.join(BOT_DATA_DIR, BOT_DATA_FILE)
BOT_DRAFT_TTL_DAYS = int_env("BOT_DRAFT_TTL_DAYS", 14, minimum=1, maximum=3650)
BOT_SUBMITTED_TTL_DAYS = int_env("BOT_SUBMITTED_TTL_DAYS", 90, minimum=1, maximum=3650)
BOT_TRANSIENT_TTL_DAYS = int_env("BOT_TRANSIENT_TTL_DAYS", 7, minimum=1, maximum=3650)
BOT_AUTOSAVE_INTERVAL_SECONDS = int_env(
    "BOT_AUTOSAVE_INTERVAL_SECONDS", 120, minimum=15, maximum=3600
)
# Мёртвые записи: снятые или пропавшие из канала объявления и старая история.
# Живые и архивные объявления партнёров не трогаются никогда.
BOT_DEAD_LISTING_TTL_DAYS = int_env("BOT_DEAD_LISTING_TTL_DAYS", 365, minimum=30, maximum=3650)
BOT_HISTORY_TTL_DAYS = int_env("BOT_HISTORY_TTL_DAYS", 730, minimum=30, maximum=3650)
# Порт, который слушает health-страница. Railway проверяет им живость сервиса;
# раньше этот же порт поднимал Stripe-вебхук, поэтому сервер здесь сохранён.
WEB_PORT = int_env("PORT", 8080, minimum=1, maximum=65535)
PARTNER_GEMINI_DAILY_LIMIT = int_env("PARTNER_GEMINI_DAILY_LIMIT", 100, minimum=0, maximum=100000)
REVOKED_PARTNER_IDS = {
    value.strip()
    for value in clean_env("REVOKED_PARTNER_IDS").split(",")
    if value.strip().isdigit()
}

EMPLOYEES = {
    "ivan": "https://t.me/malakhov_prague",
    "ivan2": "https://t.me/malakhov_prague",
    "vera": "https://t.me/VeraGryshyna",
    "vera2": "https://t.me/VeraGryshyna",
    "ekaterina2": "https://t.me/ekaterina_rossel",
    "binio_dp2": "https://t.me/Binio_DP",
    "darya2": "https://t.me/Binio_Darya",
}
EMPLOYEE_NAMES = {
    "ivan": "Иван",
    "ivan2": "Иван",
    "vera": "Вера",
    "vera2": "Вера",
    "ekaterina2": "Екатерина",
    "binio_dp2": "Диана",
    "darya2": "Дарья",
}
EMPLOYEE_CHOICE_KEYS = ("ivan2", "vera2", "ekaterina2", "binio_dp2", "darya2")
# Псевдосотрудник для объявлений, у которых контакт не удалось сопоставить
# ни с одной ссылкой. Нужен, чтобы такие записи были видны, а не пропадали.
UNASSIGNED_EMPLOYEE_KEY = "__unassigned__"
EMPLOYEE_CODE_ALIASES = {
    "ivan": "ivan2",
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
# Жизненный цикл объявления. Через LISTING_STALE_REMINDER_DAYS бот спрашивает
# автора, актуально ли ещё; если ответа нет до LISTING_STALE_ARCHIVE_DAYS,
# объявление уходит в архив, а пост в канале получает пометку «НЕАКТУАЛЬНО».
LISTING_STALE_REMINDER_DAYS = int_env("LISTING_STALE_REMINDER_DAYS", 150, minimum=7, maximum=3650)
LISTING_STALE_ARCHIVE_DAYS = int_env("LISTING_STALE_ARCHIVE_DAYS", 180, minimum=8, maximum=3650)
if LISTING_STALE_ARCHIVE_DAYS <= LISTING_STALE_REMINDER_DAYS:
    LISTING_STALE_ARCHIVE_DAYS = LISTING_STALE_REMINDER_DAYS + 30
# Сколько объявлений обрабатывать за один проход и как часто проверять.
LISTING_STALE_CHECK_INTERVAL_SECONDS = int_env(
    "LISTING_STALE_CHECK_INTERVAL_SECONDS", 6 * 60 * 60, minimum=600, maximum=7 * 24 * 60 * 60
)
LISTING_STALE_MAX_PER_RUN = int_env("LISTING_STALE_MAX_PER_RUN", 30, minimum=1, maximum=200)
# Потолок для ручной команды /sync_channel: один прогон берёт столько записей и
# сообщает, сколько осталось. Так сверка не превращается в получасовую задачу.
CHANNEL_FULL_SYNC_MAX_ITEMS = int_env(
    "CHANNEL_FULL_SYNC_MAX_ITEMS", 400, minimum=50, maximum=5000
)
CHANNEL_CHECK_VERSION = "telegram-preview-v3-conservative"

# Владельцы ссылок задаются отдельно: по публичной deep-link ссылке нельзя
# надёжно отличить сотрудника от партнёра, которого этот сотрудник привёл.
# Формат Railway Variable: ivan2:123456789,vera2:234567890
EMPLOYEE_STATS_OWNER_BY_ID = {}
# Нераспознанные записи копим здесь: logger ещё не создан, а молча терять
# строку нельзя — иначе объявления сотрудника попадут в графу «партнёры»,
# и об ошибке в настройке никто не узнает.
EMPLOYEE_TELEGRAM_IDS_PROBLEMS = []
for _employee_pair in clean_env("EMPLOYEE_TELEGRAM_IDS").split(","):
    if not _employee_pair.strip():
        continue
    _employee_code, _separator, _employee_id = _employee_pair.partition(":")
    _employee_code = _employee_code.strip()
    _employee_code = EMPLOYEE_CODE_ALIASES.get(_employee_code, _employee_code)
    _employee_id = _employee_id.strip()
    if not _separator:
        EMPLOYEE_TELEGRAM_IDS_PROBLEMS.append(
            f"{_employee_pair.strip()!r}: нет двоеточия, нужен формат код:telegram_id"
        )
    elif _employee_code not in EMPLOYEES:
        EMPLOYEE_TELEGRAM_IDS_PROBLEMS.append(
            f"{_employee_pair.strip()!r}: неизвестный код сотрудника {_employee_code!r}; "
            f"допустимые: {', '.join(sorted(EMPLOYEE_CHOICE_KEYS))}"
        )
    elif not _employee_id.isdigit():
        EMPLOYEE_TELEGRAM_IDS_PROBLEMS.append(
            f"{_employee_pair.strip()!r}: telegram id должен состоять только из цифр"
        )
    else:
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

for _problem in EMPLOYEE_TELEGRAM_IDS_PROBLEMS:
    logger.warning("EMPLOYEE_TELEGRAM_IDS: запись пропущена — %s", _problem)
if EMPLOYEE_STATS_OWNER_BY_ID:
    logger.info(
        "Сотрудники для статистики: %s",
        ", ".join(f"{code}={user_id}" for user_id, code in sorted(
            EMPLOYEE_STATS_OWNER_BY_ID.items(), key=lambda pair: pair[1]
        )),
    )
else:
    logger.warning(
        "EMPLOYEE_TELEGRAM_IDS не задана: объявления сотрудников будут считаться "
        "партнёрскими в разделе «По сотрудникам»"
    )


class AtomicPicklePersistence(PicklePersistence):
    """Совместимая с прежним .pickle память с атомарной записью и .bak-копией."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._atomic_write_lock = threading.RLock()

    @property
    def backup_path(self):
        return Path(str(self.filepath) + ".bak")

    @staticmethod
    def _link_or_copy(source, destination):
        """Делает резервную копию жёсткой ссылкой, если файловая система умеет.

        Ссылка создаётся мгновенно и не пишет данные повторно. Если том её не
        поддерживает, молча возвращаемся к обычному копированию — поведение
        при этом прежнее, просто медленнее.
        """
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            os.link(source, destination)
        except (OSError, AttributeError, NotImplementedError):
            shutil.copy2(source, destination)

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
                    # Жёсткая ссылка вместо копирования: прежняя версия файла
                    # остаётся доступна как .bak, но байты на диск не
                    # переписываются. При тысячах объявлений копирование было
                    # второй полной записью файла на каждое сохранение.
                    self._link_or_copy(original_path, backup_temp_path)
                    os.replace(backup_temp_path, backup_path)
                os.replace(temp_path, original_path)

                if not backup_path.exists():
                    self._link_or_copy(original_path, backup_temp_path)
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
CONTACT_PLACEHOLDER = "[[CONTACT]]"
CAPTION_SAFETY_MARGIN = 24
HUGE_SOURCE_CHARACTER_THRESHOLD = 2200
HUGE_SOURCE_WORD_THRESHOLD = 350
# Считается по словам описания без сумм и контактов: ниже этого порога фактов
# на развёрнутое вступление не хватает, и модель начинает добирать слова водой
# или пересказом заголовка.
THIN_SOURCE_WORD_THRESHOLD = 30
GEMINI_CONCURRENT_LIMIT = int_env("GEMINI_CONCURRENT_LIMIT", 8, minimum=1, maximum=16)
GEMINI_SEMAPHORE = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
MAX_CONCURRENT_UPDATES = int_env("MAX_CONCURRENT_UPDATES", 16, minimum=4, maximum=32)
# Та же модель и тот же полный промпт. Общий бюджет включает ожидание очереди и
# повтор только после ошибки API. Успешный ответ возвращается сразу и больше не
# запускает второй скрытый запрос к Gemini для сокращения. Минимум в 60 секунд
# не позволяет старому значению GEMINI_TIMEOUT_SECONDS=25 на Railway снова
# преждевременно оборвать нормальный ответ модели. Здесь намеренно не используем
# minimum=60: старое значение должно быть повышено, а не остановить запуск.
_CONFIGURED_GEMINI_TIMEOUT_SECONDS = int_env("GEMINI_TIMEOUT_SECONDS", 60, minimum=1, maximum=120)
GEMINI_TIMEOUT_SECONDS = max(60, _CONFIGURED_GEMINI_TIMEOUT_SECONDS)
if _CONFIGURED_GEMINI_TIMEOUT_SECONDS < GEMINI_TIMEOUT_SECONDS:
    logger.warning(
        "GEMINI_TIMEOUT_SECONDS=%s слишком мало; безопасный предел повышен до %s",
        _CONFIGURED_GEMINI_TIMEOUT_SECONDS,
        GEMINI_TIMEOUT_SECONDS,
    )
GEMINI_MAX_ATTEMPTS = int_env("GEMINI_MAX_ATTEMPTS", 3, minimum=1, maximum=3)
GEMINI_SLOW_NOTICE_SECONDS = int_env("GEMINI_SLOW_NOTICE_SECONDS", 8, minimum=5, maximum=10)
# Фоновые задачи сверки канала и runner health-сервера живут только в памяти
# процесса: PicklePersistence не умеет сериализовать объекты asyncio/aiohttp и
# не должна получать их через bot_data.
CHANNEL_SYNC_TASKS = {}
CHANNEL_FULL_SYNC_TASK = None
HEALTH_WEB_RUNNER = None
LISTING_STALE_TASK = None

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


MONEY_LINE_PATTERN = re.compile(
    r'(Kč|CZK|EUR|€|крон)|'
    r'^\s*(?:[—\-*•]\s*)?(?:Аренда|Арендная\s+плата|Стоимость|Цена|Залог|Депозит|Комиссия|'
    r'Коммунальн\w*|Услуги|Электричеств\w*|Газ|Вода|Отопление|Интернет|Парковк\w*|'
    r'Парковочное\s+место|Poplatky|Nájem|Kauce|Provize|Energie)\b',
    re.IGNORECASE,
)
SERVICE_LINE_PATTERN = re.compile(
    r'^\s*(?:#|@|https?://|www\.|\+?\d[\d\s()\-]{7,})|'
    r'^\s*(?:ID|Контакт\w*|Телефон|Тел\.?|Тг|TG|Telegram|WhatsApp|E-?mail|Почта|Сайт)\s*[:\-–]|'
    r'\b[\w.-]+\.(?:cz|com|ru|eu|net|org)\b',
    re.IGNORECASE,
)


def descriptive_word_count(raw_text):
    """Считает слова, из которых реально можно построить вступление.

    Суммы, контакты агентства, хештеги и ссылки в описание объекта не попадают:
    они живут в своих разделах или вырезаются. Если считать их наравне с
    текстом, объявление из одних цен выглядит объёмным, и модель получает
    команду написать длинное вступление там, где фактов на одну строку.
    """
    words = 0
    for line in str(raw_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if SERVICE_LINE_PATTERN.search(stripped) or MONEY_LINE_PATTERN.search(stripped):
            continue
        words += len(re.findall(r'\S+', stripped))
    return words


def listing_description_length_rules(raw_text):
    """Подбирает ориентир по объёму вводного описания под размер исходника.

    Считаем только факты, которые реально можно рассказать во вступлении:
    заголовок, финансовые условия и локация живут в своих разделах, поэтому
    объём вступления зависит от того, много ли осталось на него самого.
    """
    value = str(raw_text or "")
    word_count = len(re.findall(r'\S+', value))
    fact_words = descriptive_word_count(value)
    common_tail = (
        "Вступление не должно быть сухим перечислением: пиши связными предложениями живым "
        "русским языком, как рассказал бы человек, который этот объект видел. "
        "Это правило относится только к вводному описанию, а не к разделам "
        "«Локация», «Финансовые условия», условиям заселения, контакту и хештегам."
    )

    if (
        len(value) >= HUGE_SOURCE_CHARACTER_THRESHOLD
        or word_count >= HUGE_SOURCE_WORD_THRESHOLD
    ):
        return (
            "- Исходник очень большой или повторяющийся: во вступлении оставь 3-4 самых важных "
            "характеристики объекта и уложись примерно в 35-45 слов. Остальные уникальные факты не теряй — "
            "перенеси их в подходящие разделы и в короткие финальные условия. Удали повторы и рекламную воду. "
            + common_tail
        )

    if fact_words <= THIN_SOURCE_WORD_THRESHOLD:
        return (
            "- В исходнике мало описания объекта: почти весь объём занимают суммы, контакты и "
            "служебные строки, а они во вступление не идут. Сделай вступление из 1-2 живых предложений "
            "примерно на 15-25 слов. Ни в коем случае не добирай объём выдуманными характеристиками, "
            "общими словами о районе или пересказом заголовка: короткое вступление здесь — "
            "правильный результат, а не недоработка. "
            + common_tail
        )

    return (
        "- Исходник обычного объёма: ориентир для вступления — примерно 25-40 слов, 2-4 предложения. "
        "Точное число не важно, важно, чтобы каждое предложение несло свой факт. Связывай характеристики "
        "между собой, а не перечисляй их через запятую. Не добавляй выдуманные характеристики, "
        "неподтверждённые преимущества и пустые фразы ради объёма. Плохой, обрывочный или иностранный "
        "текст можно полностью переформулировать, сохраняя реальный масштаб и содержание исходника. "
        + common_tail
    )


LISTING_TEMPLATE = """
Ты профессиональный редактор объявлений Binio для русскоязычных клиентов в Праге. Преврати исходный текст в красивое, естественное и полностью готовое к публикации объявление об аренде.

Тип объекта выбран партнёром:
{property_type_rules}
Это важнее исходного текста: комната не должна выглядеть как квартира, дом — как квартира, коммерция — как жильё.

Ориентир по стилю и структуре (данные из примера не копируй; все факты в примере взяты из исходника — сам ничего не придумывай):
<b>Квартира 1+кк, 27 м², Прага 5, Motol</b>

Светлая квартира на 2-м этаже дома без лифта. Недавно сделан ремонт, из мебели уже есть основное — можно заезжать и жить, ничего не докупая. Окна выходят во двор, поэтому в комнате тихо.

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
- Заголовок уже называет тип объекта, планировку, метраж и район. НЕ повторяй их во вводном описании: читатель только что их прочитал, и пересказ выглядит как пустая строка. Вступление рассказывает то, что в заголовок не поместилось: состояние и ремонт, мебель и технику, этаж и лифт, террасу, балкон или вид из окон, дом и жилой комплекс, что входит в аренду, кому объект подходит, когда он свободен. Улицу и микрорайон называть можно — они уточняют заголовок, а не повторяют его.
- Поэтому не начинай вступление с «Предлагается», «Сдаётся», «Вашему вниманию», «Представляем», «В аренду предлагается» и подобных оборотов: за ними всегда следует пересказ заголовка. Начинай сразу с содержательного факта об объекте.
- Если после вычета заголовка, финансовых условий и локации фактов осталось мало — пиши коротко и честно. Два живых предложения лучше, чем абзац, растянутый общими словами. Но и в двух предложениях текст должен звучать как человеческая речь, а не как строка из таблицы.
- Формулировки делай лучше исходных: плавно, понятно, уважительно, без буквального перевода, канцелярита и рекламных преувеличений.
- Текст должен мягко вызывать интерес к объекту через подтверждённые преимущества. Не составляй сухую сводку фактов: связывай характеристики с понятной бытовой пользой, только когда такая связь очевидна из исходника. Делай это спокойно, без давления, восторженных оценок, рекламных штампов и неподтверждённых обещаний.
- В описании не повторяй подряд «квартира», «комната», «дом», «объект» или другое название недвижимости. После первого упоминания перестрой следующую фразу без повторного подлежащего; не заменяй каждый повтор словом «жильё».
- Не начинай два соседних предложения одинаково и не повторяй одну мысль разными словами.
- Предпочитай естественные слова «есть», «можно», «подходит», «сдаётся». Не используй канцелярские обороты «имеется», «данный объект», «осуществляется», «предоставляется возможность», «оборудована для проживания», «возможно оформление». Пиши, например, «Можно оформить прописку».
- Не называй помещение «уютным», «современным», «просторным», «идеальным» или «полностью оборудованным», если это прямо не подтверждается исходником. Не скрывай недостатки: отсутствие кухни, лифта или мебели сообщай спокойно и прямо.
- Не пиши «идеально подходит», «она имеет», «обладает продуманной планировкой» и похожие рекламные или переводные конструкции. Пиши естественно: «подходит семье», «есть терраса», «квартира частично меблирована». Не превращай разрешение на животных в эмоциональное «животные приветствуются»: передавай факт точно — «можно с домашними животными».
- Сроки и расписание передавай точно, не переосмысливай: «в течение недели», «по будням», «на следующей неделе» и конкретная дата — разные условия.
- Не делай сухую сводку. Короткий исходник можно умеренно расширить за счёт естественной связи подтверждённых фактов и понятной бытовой пользы, но не за счёт выдумок или слов ради длины. Подробный исходник упорядочивай и сокращай без потери уникальных фактов.
- Объём вводного описания выбирай по количеству оставшихся фактов, а не по заданному числу слов: на скудный исходник — 1-2 предложения, на обычный — 2-4, на очень подробный — 3-4 самых важных характеристики. Точный ориентир для этого исходника дан ниже отдельной строкой. Пустое предложение ради объёма — худшая ошибка, чем короткий текст. Второстепенные характеристики и условия — подвал, парковку, животных, интернет, дату заезда — при необходимости вынеси в короткие финальные фразы после основных разделов. Разделы с локацией, финансами и условиями в эти слова не входят.
{description_length_rules}
- Сохрани все важные факты: дату заезда, вместимость, мебель, технику, удобства, состояние, этаж, лифт, животных и другие условия, если они есть в исходнике.
- Обязательно сохрани статус объекта и юридические ограничения: например, что это ательер и что оформление trvalý/přechodný pobyt невозможно.
- Каждую отдельно указанную сумму или финансовое условие сохрани отдельной понятной строкой. Не объединяй коммунальные платежи, электричество и интернет в одни скобки и не теряй ни одну из этих сумм.
- Если точное время в пути не указано, пиши «несколько минут пешком». Не пиши неестественное «около нескольких минут». Форму «около X минут» используй только при наличии числа в исходнике.
- Раздел «Локация» добавляй только при наличии в исходнике адреса, транспорта или инфраструктуры. Не выдумывай пункты ради заполнения шаблона.
- В «Финансовых условиях» показывай только те платежи, которые реально указаны. Не оставляй пустые строки и шаблонные X.
- Не используй фразы: «квартира встречает», «локация предлагает», «пространство порадует», «данное помещение», «внутри найдёте».
- Ориентир естественной редактуры: вместо «Квартира оборудована для проживания, имеется мебель, возможно оформление прописки» пиши «Подходит для одного человека. Основная мебель уже есть. Можно оформить прописку». Вместо «Просмотры возможны по предварительной договорённости» пиши «Просмотр — по предварительной договорённости».
- Готовый ответ целиком — включая HTML-теги, маркер [[CONTACT]] и хештеги — должен быть не длиннее {model_character_limit} символов. Это точный технический предел, уже рассчитанный с запасом под настоящую контактную ссылку. Сохрани все уникальные факты и суммы, сокращая повторы и формулировки, а не содержание. Не делай текст заметно короче доступного объёма без необходимости.
- Перед ответом молча перечитай готовый текст и проверь по порядку: не пересказывает ли первое предложение заголовок; можно ли вычеркнуть любое предложение без потери факта (если да — вычеркни); нет ли повторов существительных, одинаковых начал предложений, канцелярита и машинных оборотов; на месте ли все суммы и условия; звучит ли текст как живая человеческая речь, а не как перечень характеристик. Не описывай эту проверку в ответе.

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
Хештеги: максимум 3. Второй хештег всегда административный район Праги в формате #Praha1...#Praha22, если он указан в тексте; не используй микрорайоны вроде #holesovice, #smichov, #vinohrady или #chodov. 2kk/2кк/2+kk/2+кк = #2kk; 3kk/3кк/3+kk/3+кк = #3kk; 4kk/4кк/4+kk/4+кк = #4kk. #2plus1/#3plus1/#4plus1 только для явных 2+1/3+1/4+1. Комната #pokoj, дом #dum, участок #pozemek, коммерция/нежилое #komerce.
Контакт: вставь ровно [[CONTACT]] отдельной строкой. Не добавляй Markdown-ссылку, имя, username или URL рядом с этим маркером.

ТЕКСТ ОТ ПАРТНЁРА:
{text}

Верни только готовое объявление, без пояснений.
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

    # Платёжные переменные (STRIPE_*, PAYMENT_PROVIDER_TOKEN, PUBLIC_BASE_URL)
    # больше не используются и не проверяются: оплата отключена, её код лежит
    # в payments_legacy.py. Лишние переменные в Railway запуску не мешают.

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
            # /partner делает то же самое и остаётся рабочей командой,
            # но в меню дублировать её незачем.
            BotCommand("start", "Новое объявление"),
            BotCommand("mylistings", "Мои объявления"),
            BotCommand("drafts", "Незавершённые объявления"),
            BotCommand("cancel", "Отменить текущий шаг"),
            BotCommand("employee", "Сменить сотрудника для контакта"),
            BotCommand("mystats", "Моя статистика"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось обновить меню команд Telegram: {e}")

    await start_health_server(app)
    start_listing_freshness_job(app)


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




def history_key(listing_id):
    return f"history_listing_{listing_id}"


def reject_notice_key(listing_id):
    """Куда отправить причину отклонения, если администратор решит её написать."""
    return f"reject_notice_{listing_id}"


def clear_reject_reason_session(bot_data):
    bot_data.pop("admin_reason_listing_id", None)
    bot_data.pop("admin_reason_prompt_message_id", None)






# История нужна только статистике: она считает объявления, а не показывает их.
# Тексты, фото и служебные флаги в неё не переносим — при тысячах публикаций
# это был бы второй полный экземпляр базы, который никто никогда не читает.
HISTORY_FIELDS = (
    "listing_id", "partner_id", "partner_label", "source", "employee_code",
    "contact_url", "property_type", "status", "history_status", "listing_status",
    "submitted_to_admin", "submitted_at", "published_at", "created_at",
    "rejection_reason", "cancellation_reason",
)


def save_listing_history(context, listing_id, pending, status, **extra):
    """Сохраняет короткую запись о завершённой заявке для статистики."""
    timestamp = now_iso()
    source = dict(pending or {})
    source.update(extra)
    history = {key: source[key] for key in HISTORY_FIELDS if key in source}
    history["listing_id"] = str(listing_id)
    history["history_status"] = status
    history["status"] = status
    history.setdefault("created_at", timestamp)
    history["updated_at"] = timestamp
    history[f"{status}_at"] = timestamp
    context.application.bot_data[history_key(listing_id)] = history
    return history




































class AppContext:
    def __init__(self, app):
        self.application = app
        self.bot = app.bot




def context_from_app(app):
    return AppContext(app)








async def persist_now(app):
    """Немедленно фиксирует критическое состояние, если приложение уже запущено."""
    updater = getattr(app, "update_persistence", None)
    if updater is not None:
        await updater()






























async def health_page(request):
    return web.Response(text="Binio Partner Bot is running")


async def start_health_server(app):
    """Поднимает health-страницу на PORT для проверки живости на Railway.

    Платёжных маршрутов здесь нет: остались только / и /health. Если сервис
    развёрнут воркером без внешнего порта, сервер просто никем не опрашивается
    и ничему не мешает. Сбой запуска не должен ронять бота.
    """
    global HEALTH_WEB_RUNNER
    if web is None:
        logger.warning("aiohttp недоступен: health-страница не запущена")
        return
    if HEALTH_WEB_RUNNER is not None:
        return
    try:
        web_app = web.Application()
        web_app.router.add_get("/", health_page)
        web_app.router.add_get("/health", health_page)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        HEALTH_WEB_RUNNER = runner
        logger.info(f"Health-страница запущена на порту {WEB_PORT}")
    except Exception as error:
        logger.error(f"Не удалось запустить health-страницу на порту {WEB_PORT}: {error}")


async def stop_background_tasks(app):
    """Корректно гасит фоновые сверки канала и health-сервер при остановке."""
    global CHANNEL_FULL_SYNC_TASK, HEALTH_WEB_RUNNER, LISTING_STALE_TASK
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

    freshness_task = LISTING_STALE_TASK
    if freshness_task is not None:
        LISTING_STALE_TASK = None
        freshness_task.cancel()
        await asyncio.gather(freshness_task, return_exceptions=True)

    runner = HEALTH_WEB_RUNNER
    if runner is not None:
        HEALTH_WEB_RUNNER = None
        try:
            await runner.cleanup()
        except Exception as error:
            logger.warning(f"Не удалось корректно остановить health-страницу: {error}")


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


def listings_search_query(context, user_id):
    return str(context.application.bot_data.get(f"listings_query_{user_id}") or "").strip()


def apply_listings_search(listings, query):
    """Отбирает объявления по подстроке: район, улица, планировка, цена.

    Ищем по видимому тексту без HTML, поэтому запрос «Vysočany» или «3+kk»
    находит то же, что человек видит в объявлении.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return listings
    found = []
    for item in listings:
        haystack = strip_html_tags_keep_text(item.get("listing", "")).lower()
        if needle in haystack:
            found.append(item)
    return found


def current_filtered_published_listings(context, user_id):
    all_listings = list_partner_published(context, user_id, include_hidden=True)
    filter_key = current_published_filter(context, user_id)
    listings = filter_published_listings(all_listings, filter_key)
    return apply_listings_search(listings, listings_search_query(context, user_id)), filter_key


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

        for busy_field in ("submit_in_progress", "admin_action_in_progress"):
            if pending.get(busy_field) and not pending_busy(pending, busy_field, ttl_seconds=300):
                pending.pop(busy_field, None)
                changed = True

        deleted = False
        # Старые оплаченные разовые заявки, оставшиеся в памяти от прежней
        # платной публикации, не удаляем автоматически: без текста и фото
        # разобрать такой платёж потом будет нечем.
        legacy_paid_public = bool(
            pending.get("source") == "public"
            and (pending.get("paid") or pending.get("payment_review_required"))
        )
        if legacy_paid_public:
            pass
        elif pending.get("submitted_to_admin"):
            if is_older_than(last_seen, submitted_ttl):
                # Короткая запись, как и в save_listing_history: тексты и фото
                # протухшей заявки статистике не нужны.
                history = {key: pending[key] for key in HISTORY_FIELDS if key in pending}
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
        "listings_query_",
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

    removed["dead"] = cleanup_dead_listings(bot_data)
    return removed


def cleanup_dead_listings(bot_data):
    """Удаляет давно мёртвые записи, чтобы память не росла бесконечно.

    Трогаем только то, что уже никому не показывается и не может ожить:
    объявления, снятые или пропавшие из канала очень давно, и совсем старую
    историю. Активные, сданные и просто убранные в архив записи остаются —
    они видны партнёру в «Моих объявлениях», и удалять их нельзя.
    """
    dead_ttl = BOT_DEAD_LISTING_TTL_DAYS * 24 * 60 * 60
    history_ttl = BOT_HISTORY_TTL_DAYS * 24 * 60 * 60
    removed = 0

    for key in list(bot_data.keys()):
        value = bot_data.get(key)
        if not isinstance(value, dict):
            continue

        if key.startswith("published_listing_"):
            is_dead = value.get("status") == "removed" or value.get("channel_missing")
            if not is_dead:
                continue
            last_seen = (
                value.get("hidden_at")
                or value.get("channel_checked_at")
                or value.get("updated_at")
                or value.get("published_at")
            )
            if is_older_than(last_seen, dead_ttl):
                bot_data.pop(key, None)
                removed += 1

        elif key.startswith("history_listing_"):
            last_seen = value.get("updated_at") or value.get("created_at")
            if is_older_than(last_seen, history_ttl):
                bot_data.pop(key, None)
                removed += 1

        elif key.startswith("reject_notice_"):
            # Предложение написать причину живёт неделю: позже оно уже неуместно.
            if is_older_than(value.get("created_at"), 7 * 24 * 60 * 60):
                bot_data.pop(key, None)
                removed += 1

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
        f"временные шаги: {removed.get('transient', 0)}, "
        f"мёртвые записи: {removed.get('dead', 0)}"
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
    # Данные отключённой оплаты. Автоматически не удаляются: это журнал
    # прошлых платежей. Полный код оплаты лежит в payments_legacy.py.
    if (
        key.startswith("payment_record_")
        or key.startswith("deferred_stripe_status_")
        or key == "processed_stripe_events"
    ):
        return "Архив прошлых платежей"
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
    """Убирает служебную пометку в начале поста, какой бы она ни была.

    Пометки не должны накапливаться: объявление может побывать сданным,
    вернуться в активные, потом устареть.
    """
    return re.sub(
        r'^\s*(?:<b>)?(?:(?:✅|🔴|⚠️)\s*)?(?:СДАНО|НЕАКТУАЛЬНО)(?:</b>)?\s*\n+',
        '',
        text,
        flags=re.I,
    ).strip()


def listing_with_status(text, status, stale=False):
    base = strip_listing_status(text)
    if status == "rented":
        return f"<b>🔴 СДАНО</b>\n\n{base}"
    if stale:
        return f"<b>⚠️ НЕАКТУАЛЬНО</b>\n\n{base}"
    return base


def published_status_label(status, channel_missing=False, archived=False, stale=False):
    if channel_missing:
        base = "⚫ Нет в канале"
    elif status == "rented":
        base = "🔴 Сдано"
    elif status == "removed":
        base = "⚪ Снято"
    elif stale:
        base = "⚠️ Неактуально"
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
        # Проверяем сначала те, что дольше всех не проверялись, и берём за один
        # прогон ограниченную порцию. Иначе при тысячах объявлений сверка идёт
        # десятки минут, а при перезапуске начинается заново с нуля.
        total_published = len(items)
        due = [item for item in items if channel_check_is_due(item)]
        due.sort(key=lambda item: item.get("channel_checked_at") or "")
        skipped_fresh = total_published - len(due)
        remaining_after = max(0, len(due) - CHANNEL_FULL_SYNC_MAX_ITEMS)
        batch = due[:CHANNEL_FULL_SYNC_MAX_ITEMS]

        total_missing = 0
        total_unknown = 0
        checked = 0
        for start in range(0, len(batch), PUBLISHED_LISTINGS_PAGE_SIZE):
            chunk = batch[start:start + PUBLISHED_LISTINGS_PAGE_SIZE]
            missing, unknown = await verify_published_items(context, chunk, archive_missing=True)
            total_missing += missing
            total_unknown += unknown
            checked += len(chunk)
            # Периодически фиксируем прогресс: после перезапуска проверенные
            # записи уже не придётся обходить заново.
            if checked % (PUBLISHED_LISTINGS_PAGE_SIZE * 10) == 0:
                await persist_now(app)
        await persist_now(app)

        lines = [
            "✅ Синхронизация канала завершена.",
            "",
            f"Проверено за этот прогон: {checked}",
            f"Перемещено в архив: {total_missing}",
        ]
        if total_unknown:
            lines.append(f"Временно не удалось проверить: {total_unknown}")
        if skipped_fresh:
            lines.append(f"Пропущено как недавно проверенные: {skipped_fresh}")
        if remaining_after:
            lines.append("")
            lines.append(
                f"Осталось проверить: {remaining_after}. "
                "Запустите /sync_channel ещё раз — бот продолжит с самых старых."
            )
        await app.bot.send_message(chat_id=admin_chat_id, text="\n".join(lines))
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


def listing_freshness_age_seconds(item):
    """Сколько объявление считается актуальным без подтверждения автора.

    Отсчёт идёт от последнего подтверждения, а если его не было — от
    публикации. Правки цены не продлевают срок: человек мог поправить сумму,
    не проверяя, сдан ли объект.
    """
    started = item.get("freshness_confirmed_at") or item.get("published_at")
    parsed = parse_iso(started)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def listing_is_watchable(item):
    """Следим только за живыми объявлениями в канале."""
    return bool(
        item.get("published_at")
        and item.get("status", "active") == "active"
        and not item.get("channel_missing")
        and not item.get("hidden_from_list")
    )


def stale_reminder_keyboard(listing_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ещё актуально", callback_data=f"fresh_keep_{listing_id}")],
        [
            InlineKeyboardButton("🔴 Сдано", callback_data=f"fresh_rented_{listing_id}"),
            InlineKeyboardButton("🗄 Уже неактуально", callback_data=f"fresh_archive_{listing_id}"),
        ],
    ])


async def mark_listing_stale(context, item):
    """Уводит забытое объявление в архив и помечает пост в канале."""
    listing_id = item["listing_id"]
    visible = listing_with_status(item.get("listing", ""), item.get("status", "active"), stale=True)
    try:
        await edit_published_channel_posts(context, item, visible)
    except Exception as error:
        # Пост мог быть удалён вручную или стать нередактируемым. Архивировать
        # запись всё равно нужно, иначе бот будет возвращаться к ней бесконечно.
        logger.info("Не удалось пометить пост как неактуальный (%s): %s", listing_id, error)

    item["stale"] = True
    item["stale_at"] = now_iso()
    item["hidden_from_list"] = True
    item["hidden_at"] = now_iso()
    item["hidden_reason"] = "stale"
    save_published(context, listing_id, item)


async def listing_freshness_job(app):
    """Раз в несколько часов спрашивает про залежавшиеся объявления."""
    while getattr(app, "running", True) is False:
        await asyncio.sleep(0.25)
    reminder_ttl = LISTING_STALE_REMINDER_DAYS * 24 * 60 * 60
    archive_ttl = LISTING_STALE_ARCHIVE_DAYS * 24 * 60 * 60
    while True:
        try:
            context = context_from_app(app)
            items = [
                value for key, value in list(app.bot_data.items())
                if key.startswith("published_listing_")
                and isinstance(value, dict)
                and listing_is_watchable(value)
            ]
            handled = 0
            for item in items:
                if handled >= LISTING_STALE_MAX_PER_RUN:
                    break
                age = listing_freshness_age_seconds(item)
                if age is None:
                    continue

                if age >= archive_ttl and item.get("stale_reminded_at"):
                    await mark_listing_stale(context, item)
                    handled += 1
                    partner_id = item.get("partner_id")
                    if partner_id is not None:
                        try:
                            await app.bot.send_message(
                                chat_id=partner_id,
                                text=(
                                    "🗄 Объявление убрано в архив: полгода без подтверждения.\n\n"
                                    f"{listing_headline(item.get('listing', '')) or 'Объявление'}\n\n"
                                    "В канале пост помечен как неактуальный. "
                                    "Если объект всё ещё сдаётся, откройте /mylistings → Архив "
                                    "и верните его в список."
                                ),
                            )
                        except Exception as error:
                            logger.info("Не удалось сообщить об архивации: %s", error)
                    await asyncio.sleep(0.5)

                elif age >= reminder_ttl and not item.get("stale_reminded_at"):
                    partner_id = item.get("partner_id")
                    if partner_id is None:
                        continue
                    headline = listing_headline(item.get("listing", "")) or "Объявление"
                    months = int(age // (30 * 24 * 60 * 60))
                    try:
                        await app.bot.send_message(
                            chat_id=partner_id,
                            text=(
                                f"Объявление висит в канале {months} мес. и всё ещё активно:\n\n"
                                f"{html.escape(headline)}\n\n"
                                "Оно ещё сдаётся? Если не ответить, через месяц объявление "
                                "уйдёт в архив, а пост в канале будет помечен как неактуальный."
                            ),
                            parse_mode="HTML",
                            reply_markup=stale_reminder_keyboard(item["listing_id"]),
                            disable_web_page_preview=True,
                        )
                    except Exception as error:
                        logger.info("Не удалось отправить напоминание: %s", error)
                        # Помечаем всё равно, иначе бот будет пытаться каждый
                        # проход и упрётся в лимиты Telegram.
                    item["stale_reminded_at"] = now_iso()
                    save_published(context, item["listing_id"], item)
                    handled += 1
                    await asyncio.sleep(0.5)

            if handled:
                await persist_now(app)
                logger.info("Проверка актуальности: обработано объявлений %s", handled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Проверка актуальности объявлений не удалась")
        await asyncio.sleep(LISTING_STALE_CHECK_INTERVAL_SECONDS)


def start_listing_freshness_job(app):
    global LISTING_STALE_TASK
    if LISTING_STALE_TASK is None or LISTING_STALE_TASK.done():
        LISTING_STALE_TASK = asyncio.create_task(
            listing_freshness_job(app), name="listing-freshness")


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


def published_list_keyboard(listings, page=0, filter_key="all", counts=None,
                            total_in_filter=None, query=""):
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
            stale=item.get('stale'),
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

    # Поиск появляется, когда листать становится долго. При десятке объявлений
    # он только занимает место на экране.
    if query:
        rows.append([
            InlineKeyboardButton("🔍 Искать другое", callback_data="my_listings_search"),
            InlineKeyboardButton("✖️ Сбросить", callback_data="my_listings_search_clear"),
        ])
    elif (total_in_filter if total_in_filter is not None else len(listings)) > (
        PUBLISHED_LISTINGS_PAGE_SIZE * 2
    ):
        rows.append([InlineKeyboardButton(
            "🔍 Найти объявление", callback_data="my_listings_search")])

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






def make_contact_line(contact_url):
    safe_url = html.escape(contact_url, quote=True)
    return f'<b>Контакт:</b> <a href="{safe_url}">автор</a>'


def listing_generation_character_limit(contact_url):
    """Резервирует место под настоящую ссылку ещё до единственного запроса Gemini."""
    contact_growth = max(
        0,
        len(make_contact_line(contact_url)) - len(CONTACT_PLACEHOLDER),
    )
    return max(
        600,
        TELEGRAM_CAPTION_LIMIT - contact_growth - CAPTION_SAFETY_MARGIN,
    )


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
    match = re.search(
        r'\b(?:Praha|Prahy|Praze|Prague|Прага|Праге)\s*(2[0-2]|1[0-9]|[1-9])\b',
        text,
        flags=re.I,
    )
    if not match:
        return None
    return f"#Praha{match.group(1)}"


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
        stale=item.get("stale"),
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

    if not allow_gemini_shortening:
        # Ручной или администраторский текст нельзя обрезать без согласия.
        # Вызывающая ветка покажет точное превышение через validate_listing_ready.
        return prepared

    # Автоматически созданный текст больше не отправляется в Gemini второй раз.
    # Основной промпт уже получает точный лимит; это только мгновенная страховка,
    # если модель всё же нарушила его.
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
        model_character_limit=listing_generation_character_limit(contact_url),
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
            "✨ Улучшаю текст объявления…\n\n"
            "Обычно это занимает 15–25 секунд. При высокой нагрузке Gemini может отвечать дольше.",
        )
        await asyncio.sleep(GEMINI_SLOW_NOTICE_SECONDS)
        await update_processing_status(
            status_message,
            "🔎 Проверяю форматирование и важные детали…",
        )
        await asyncio.sleep(GEMINI_SLOW_NOTICE_SECONDS)
        await update_processing_status(
            status_message,
            "⏳ Gemini отвечает дольше обычного, но обработка продолжается.\n\n"
            "Повторно отправлять текст не нужно.",
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

            queue_wait_seconds = 0.0
            api_elapsed_seconds = 0.0

            async def call_gemini():
                nonlocal queue_wait_seconds, api_elapsed_seconds
                # Ожидание свободного места в семафоре тоже входит в общий
                # настраиваемый бюджет, поэтому очередь не зависает незаметно.
                queue_started = time.monotonic()
                async with GEMINI_SEMAPHORE:
                    queue_wait_seconds = time.monotonic() - queue_started
                    api_started = time.monotonic()
                    try:
                        return await gemini_client.aio.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=prompt,
                            config=GEMINI_GENERATION_CONFIG,
                        )
                    finally:
                        api_elapsed_seconds = time.monotonic() - api_started

            try:
                response = await asyncio.wait_for(call_gemini(), timeout=remaining)
                formatted_listing = sanitize_gemini_listing_output(response.text)
                elapsed = time.monotonic() - attempt_start
                usage = getattr(response, "usage_metadata", None)
                usage_parts = []
                for label, field_name in (
                    ("вход", "prompt_token_count"),
                    ("анализ", "thoughts_token_count"),
                    ("ответ", "candidates_token_count"),
                ):
                    value = getattr(usage, field_name, None) if usage is not None else None
                    if value is not None:
                        usage_parts.append(f"{label} {value}")
                usage_text = f"; токены: {', '.join(usage_parts)}" if usage_parts else ""
                logger.info(
                    f"Gemini попытка {attempt + 1}: успех за {elapsed:.1f}с "
                    f"(API {api_elapsed_seconds:.1f}с, очередь {queue_wait_seconds:.1f}с"
                    f"{usage_text})"
                )
                break
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - attempt_start
                logger.warning(f"Gemini попытка {attempt + 1}: тайм-аут после {elapsed:.1f}с")
                last_error = TimeoutError(f"Gemini не ответил за {GEMINI_TIMEOUT_SECONDS} секунд")
                # Не запускаем второй запрос поверх первого после полного
                # тайм-аута: отменённый запрос не должен продолжать расходовать
                # квоту параллельно с новым.
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
                    # попытки по-прежнему входят в общий настраиваемый бюджет.
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
    if len(editable_listing) > TELEGRAM_CAPTION_LIMIT:
        logger.warning(
            "Gemini превысил рассчитанный лимит: "
            f"{len(editable_listing)} символов; применена мгновенная локальная страховка"
        )
    publication_listing = await prepare_listing_for_caption(
        editable_listing,
        contact_url,
        allow_gemini_shortening=True,
    )

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





async def start_partner_flow(message, context, user, employee_key=None):
    user_id = user.id
    employee_key = canonical_employee_code(employee_key) if employee_key else None
    has_employee_link = employee_key and employee_key in EMPLOYEES

    if not is_admin(user_id) and str(user_id) in REVOKED_PARTNER_IDS:
        set_state(context, user_id, "idle")
        await message.reply_text(
            "Партнёрский доступ для этого аккаунта отключён.\n\n"
            "Если это произошло по ошибке, напишите администратору Binio.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
            ]),
        )
        return False

    if not has_employee_link and not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "idle")
        await message.reply_text(
            "Публикация в канале Binio доступна только партнёрам.\n\n"
            "Партнёрский доступ активируется по персональной ссылке сотрудника Binio. "
            "Если вы уже договорились о сотрудничестве, откройте ссылку, которую прислал сотрудник.\n\n"
            "Чтобы получить такую ссылку, напишите администратору",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
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
        f"В объявлениях будет указан контакт сотрудника Binio: "
        f"<a href=\"{contact_safe}\">{partner_name}</a> — клиенты из канала будут писать ей, "
        "она ведёт переговоры и сделку. Сменить сотрудника можно командой /employee.\n\n"
        "Чтобы создать объявление, отправьте фотографии объекта. После этого бот попросит "
        "выбрать тип недвижимости и прислать описание",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True




async def discontinued_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отвечает на команды отключённой разовой публикации.

    Команды /owner, /publish, /terms и /support остались у людей в истории и в
    меню Telegram. Без обработчика они молчат, поэтому объясняем изменение.
    """
    await update.effective_message.reply_text(
        "Разовая платная публикация больше не работает.\n\n"
        "Объявления в канале Binio теперь размещают только партнёры по персональной "
        "ссылке сотрудника. Чтобы получить такую ссылку, напишите администратору",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
        ]),
    )


async def discontinued_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Гасит старые кнопки оплаты и выбора роли из прежних сообщений.

    Без этого Telegram крутит «часики» на кнопке до истечения callback-запроса.
    """
    query = update.callback_query
    await query.answer(
        "Эта кнопка больше не действует: платная разовая публикация отключена.",
        show_alert=True,
    )
    try:
        await query.message.reply_text(
            "Разовая платная публикация отключена.\n\n"
            "Объявления размещают только партнёры по персональной ссылке сотрудника Binio",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Написать администратору", url=DEFAULT_CONTACT)],
            ]),
        )
    except Exception as error:
        logger.info(f"Не удалось пояснить отключённую кнопку: {error}")


async def legacy_role_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старая кнопка «Партнёр / риэлтор» из прежних сообщений."""
    query = update.callback_query
    user = update.effective_user
    if get_state(context, user.id) == "processing":
        await query.answer(
            "Объявление ещё обрабатывается. Повторно отправлять текст не нужно.",
            show_alert=True,
        )
        return
    await query.answer()
    await start_partner_flow(query.message, context, user)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if get_state(context, user_id) == "processing":
        await update.message.reply_text(
            "Объявление ещё обрабатывается\n\n"
            "При высокой нагрузке Gemini может отвечать дольше. Повторно отправлять текст не нужно."
        )
        return

    context.application.bot_data.pop(f"employee_choice_mode_{user_id}", None)

    employee_key = context.args[0].strip().lower() if context.args else ""
    if employee_key in EMPLOYEES:
        await start_partner_flow(update.message, context, update.effective_user, employee_key)
        return

    # Без персональной ссылки сотрудника сценарий открывается только тем, у кого
    # партнёрский доступ уже сохранён; остальным start_partner_flow объяснит,
    # как его получить.
    await start_partner_flow(update.message, context, update.effective_user)








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
    if not is_admin(update.effective_user.id) and not has_partner_access(context, update.effective_user.id):
        await query.answer(
            "Работа с объявлениями доступна только партнёрам. Обратитесь к администратору Binio.",
            show_alert=True,
        )
        return
    context.application.bot_data[f"editing_listing_{update.effective_user.id}"] = listing_id
    set_state(context, update.effective_user.id, "done")
    await query.answer()
    await show_partner_preview(
        query.message,
        context,
        pending.get("formatted_listing", ""),
        pending.get("photos", []),
        listing_id,
    )


async def cancel_current_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    for prefix in ("photos_", "property_type_", "session_contact_", "session_partner_code_",
                   "flow_", "editing_listing_", "listings_query_"):
        context.application.bot_data.pop(f"{prefix}{user_id}", None)
    set_state(context, user_id, "idle")
    await update.effective_message.reply_text(
        "Текущий шаг отменён. Сохранённые объявления не удалены: /drafts\n\nНачать заново: /start"
    )


async def partner_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_state(context, update.effective_user.id) == "processing":
        await update.message.reply_text(
            "Объявление ещё обрабатывается\n\n"
            "При высокой нагрузке Gemini может отвечать дольше. Повторно отправлять текст не нужно."
        )
        return
    await start_partner_flow(update.message, context, update.effective_user)


async def employee_change_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if get_state(context, user_id) == "processing":
        await update.message.reply_text(
            "Объявление ещё обрабатывается\n\n"
            "При высокой нагрузке Gemini может отвечать дольше. Повторно отправлять текст не нужно."
        )
        return
    if not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "idle")
        await update.message.reply_text(
            "Сменить сотрудника могут только партнёры, которые уже вошли по персональной ссылке.\n\n"
            f'Чтобы получить партнёрский доступ, напишите <a href="{html.escape(DEFAULT_CONTACT, quote=True)}">администратору</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await ask_employee_choice(update.message, context, user_id, mode="change_employee")




async def employee_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id

    if get_state(context, user_id) != "choosing_employee":
        await query.answer("Эта кнопка уже устарела. Используйте /employee или /start.", show_alert=True)
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
            "Чтобы создать новое объявление, используйте /start",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await start_partner_flow(query.message, context, user, employee_key)


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee")
        return

    if state == "done":
        # Объявление уже готово, фото зафиксированы в предпросмотре. Советовать
        # здесь /start нельзя: он начнёт новое объявление с пустым списком фото,
        # и партнёр решит, что потерял работу.
        await update.message.reply_text(
            "Это объявление уже собрано, набор фотографий для него зафиксирован.\n\n"
            "Если нужны другие фото, создайте новое объявление командой /start — "
            "текущее никуда не денется и останется в /drafts"
        )
        return

    if state == "submitted":
        await update.message.reply_text(
            "Объявление уже отправлено на проверку, добавить к нему фото нельзя.\n\n"
            "Новое объявление: /start"
        )
        return

    if state != "waiting_photos":
        await update.message.reply_text(
            "Сейчас бот не ждёт фотографии.\n\n"
            "Начать новое объявление: /start\n"
            "Незавершённые объявления: /drafts"
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
        await query.answer(
            "Эта кнопка устарела. /start — новое объявление, /drafts — незавершённые.",
            show_alert=True,
        )
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
        await query.answer(
            "Эта кнопка устарела. /start — новое объявление, /drafts — незавершённые.",
            show_alert=True,
        )
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
    elif state == "searching_listings":
        await partner_apply_listings_search(update, context, update.message.text)
    elif state == "processing":
        await update.message.reply_text(
            "Объявление ещё обрабатывается\n\n"
            "При высокой нагрузке Gemini может отвечать дольше. Повторно отправлять текст не нужно."
        )
    elif state == "done":
        # Партнёр смотрит на готовый предпросмотр и пишет правку словами.
        # Раньше он получал «используйте /start», а эта команда начинает
        # новое объявление — то есть совет бота уничтожал его работу.
        await update.message.reply_text(
            "Объявление уже готово и ждёт вашего решения в сообщении выше.\n\n"
            "Чтобы поправить текст, нажмите под предпросмотром «✏️ Изменить текст» — "
            "бот пришлёт текущий вариант, и вы отправите исправленный.\n"
            "«✨ Улучшить текст» перепишет его заново.\n"
            "«✅ Отправить на проверку» — когда всё устраивает.\n\n"
            "Если предпросмотр потерялся в переписке, откройте его через /drafts"
        )
    elif state == "submitted":
        await update.message.reply_text(
            "Объявление уже отправлено на проверку, изменить его нельзя.\n\n"
            "Новое объявление: /start\n"
            "Опубликованные: /mylistings"
        )
    elif state == "waiting_type":
        await update.message.reply_text(
            "Сначала выберите тип объекта кнопкой выше: квартира, комната, дом, участок, коммерция или другое"
        )
    elif state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee")
    elif state == "waiting_photos":
        await update.message.reply_text(
            "Сначала отправьте фотографии объекта. Когда всё будет готово, нажмите «Фото загружены»"
        )
    else:
        await update.message.reply_text(
            "Бот принимает описание объекта только на своём шаге, а не обычным сообщением.\n\n"
            "Новое объявление: /start\n"
            "Незавершённые: /drafts\n"
            "Опубликованные: /mylistings"
        )


async def process_listing(update, context, text):
    """Обрабатывает текст через Gemini и показывает предпросмотр."""
    user_id = update.effective_user.id
    set_state(context, user_id, "processing")

    processing_message = await update.message.reply_text(
        "⏳ Обрабатываю объявление…\n\n"
        "Обычно это занимает 15–25 секунд. При высокой нагрузке Gemini может отвечать до минуты."
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
            # Новые объявления всегда партнёрские. Не читаем flow_<user_id>:
            # у пользователей, заставших прежний разовый сценарий, там мог
            # остаться "public", и объявление выпало бы из статистики сотрудника.
            source = "partner"
            employee_code = canonical_employee_code(
                session_partner_code or context.application.bot_data.get(f"partner_code_{user_id}")
            )
            if source == "partner" and employee_code not in EMPLOYEES and contact_url != DEFAULT_CONTACT:
                employee_code = employee_key_by_contact(contact_url)

        if source != "partner" or employee_code not in EMPLOYEES:
            employee_code = None

        quota_allowed = consume_partner_gemini_request(context, user_id)
        if not quota_allowed:
            set_state(context, user_id, "waiting_text")
            await update_processing_status(
                processing_message,
                "Дневной лимит автоматического улучшения текста исчерпан.\n\n"
                "Попробуйте снова завтра или напишите администратору Binio.",
            )
            return
        gemini_quota_prefix = "partner_gemini_usage_"

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
            'submitted_to_admin': bool(existing_pending.get('submitted_to_admin')) if existing_pending else False,
        })

        context.application.bot_data[f"editing_listing_{user_id}"] = listing_id
        set_state(context, user_id, "done")
        await update_processing_status(processing_message, "✅ Готово — показываю предпросмотр")
        await show_partner_preview(update.message, context, formatted_listing, photos, listing_id)

    except Exception as e:
        logger.error(f"process_listing error: {e}")
        if gemini_quota_prefix:
            refund_daily_gemini_request(context, user_id, gemini_quota_prefix)
        set_state(context, user_id, "waiting_text")
        if isinstance(e, TimeoutError):
            error_text = (
                f"Gemini не ответил за {GEMINI_TIMEOUT_SECONDS} секунд.\n\n"
                "Запрос остановлен, чтобы бот не завис навсегда. "
                "Пожалуйста, отправьте текст ещё раз."
            )
        else:
            error_text = (
                "Сервис временно недоступен\n\n"
                "Пожалуйста, отправьте текст ещё раз через несколько секунд"
            )
        await update_processing_status(processing_message, error_text)
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
        await query.answer(
            "Объявление ещё обрабатывается. Gemini может отвечать дольше; повторно отправлять текст не нужно.",
            show_alert=True,
        )
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

    quota_allowed = consume_partner_gemini_request(context, user_id)
    if not quota_allowed:
        await query.answer(
            "Дневной лимит автоматического улучшения исчерпан. Попробуйте снова завтра.",
            show_alert=True,
        )
        return
    gemini_quota_prefix = "partner_gemini_usage_"

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
                "Объявление уже было отправлено на проверку, пока готовился новый вариант. "
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
        await query.answer(
            "Объявление ещё обрабатывается. Gemini может отвечать дольше; повторно отправлять текст не нужно.",
            show_alert=True,
        )
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

    # Публикация доступна только партнёрам. Проверяем это и здесь, а не только
    # на входе в сценарий: у пользователя мог остаться старый черновик, а
    # партнёрский доступ за это время могли отозвать.
    if not is_admin(update.effective_user.id) and not has_partner_access(context, update.effective_user.id):
        await query.answer(
            "Отправка на проверку доступна только партнёрам Binio. "
            "Партнёрский доступ активируется по персональной ссылке сотрудника.",
            show_alert=True,
        )
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
            text="✅ Объявление отправлено на проверку\n\n"
                "Администратор скоро посмотрит его и опубликует в канале. "
                "Как только объявление выйдет, бот пришлёт уведомление.\n\n"
                "Новое объявление: /start"
        )
    except Exception:
        try:
            await query.edit_message_caption(
                caption="✅ Объявление отправлено на проверку\n\n"
                "Администратор скоро посмотрит его и опубликует в канале. "
                "Как только объявление выйдет, бот пришлёт уведомление.\n\n"
                "Новое объявление: /start"
            )
        except Exception:
            await query.message.reply_text(
                "✅ Объявление отправлено на проверку\n\n"
                "Администратор скоро посмотрит его и опубликует в канале. "
                "Как только объявление выйдет, бот пришлёт уведомление.\n\n"
                "Новое объявление: /start"
            )














def clear_pending_for_user(context, user_id):
    removed = 0
    preserved = 0
    for listing_id, pending in list(list_unique_pending_items(context).items()):
        if pending.get("partner_id") == user_id:
            # Старые оплаченные разовые заявки трогать нельзя: платёж по ним
            # мог остаться неразобранным. Обычные партнёрские черновики
            # удаляются, если они ещё не ушли на проверку.
            legacy_paid_public = pending.get("source") == "public" and pending.get("paid")
            if legacy_paid_public or pending.get("submitted_to_admin"):
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
        f"Удалено ваших черновиков: {removed}.\n"
        f"Сохранено уже отправленных на проверку: {preserved}.\n\n"
        "Опубликованные объявления и заявки других пользователей не тронуты"
    )


def count_listing_data(bot_data):
    """Сколько записей об объявлениях сейчас в памяти бота."""
    counts = {"pending": 0, "published": 0, "history": 0, "payments": 0}
    for key in bot_data:
        if key.startswith("pending_listing_") or (
            key.startswith("pending_") and not key.startswith("pending_listing_")
        ):
            counts["pending"] += 1
        elif key.startswith("published_listing_"):
            counts["published"] += 1
        elif key.startswith("history_listing_"):
            counts["history"] += 1
        elif key.startswith("payment_record_") or key.startswith("deferred_stripe_status_"):
            counts["payments"] += 1
    return counts


def reset_listing_data(bot_data):
    """Удаляет все записи об объявлениях и незавершённые шаги пользователей.

    Привязки партнёров к сотрудникам (partner_code_/contact_) сохраняются:
    иначе всем пришлось бы заново заходить по персональной ссылке. Архив
    прошлых платежей тоже не трогаем — на статистику он давно не влияет.
    """
    removed = {"pending": 0, "published": 0, "history": 0, "states": 0}
    state_prefixes = (
        "state_", "photos_", "property_type_", "flow_", "editing_listing_",
        "published_money_listing_", "published_money_field_", "published_filter_",
        "listings_query_",
        "session_contact_", "session_partner_code_", "employee_choice_mode_",
    )
    for key in list(bot_data):
        if key.startswith("pending_listing_") or (
            key.startswith("pending_") and not key.startswith("pending_listing_")
        ):
            bot_data.pop(key, None)
            removed["pending"] += 1
        elif key.startswith("published_listing_"):
            bot_data.pop(key, None)
            removed["published"] += 1
        elif key.startswith("history_listing_"):
            bot_data.pop(key, None)
            removed["history"] += 1
        elif key.startswith(state_prefixes):
            # Иначе человек остался бы в шаге, который ссылается на удалённое
            # объявление, и бот отвечал бы ему невпопад.
            bot_data.pop(key, None)
            removed["states"] += 1
    for key in ("admin_editing_listing_id", "admin_editing_prompt_message_id",
                "admin_editing_user_id", "admin_reason_listing_id",
                "admin_reason_prompt_message_id"):
        bot_data.pop(key, None)
    for key in [k for k in bot_data if k.startswith("reject_notice_")]:
        bot_data.pop(key, None)
    return removed


async def admin_reset_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает, что будет удалено, и просит подтверждение."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Эта команда доступна только администратору")
        return

    counts = count_listing_data(context.application.bot_data)
    total = counts["pending"] + counts["published"] + counts["history"]
    if not total:
        await update.effective_message.reply_text(
            "Удалять нечего: записей об объявлениях в памяти бота нет."
        )
        return

    payments_note = (
        f"\n\nАрхив прошлых платежей ({counts['payments']} записей) остаётся — "
        "на статистику он не влияет."
        if counts["payments"] else ""
    )
    await update.effective_message.reply_text(
        "<b>Полный сброс статистики</b>\n\n"
        "Будет удалено без возможности восстановления:\n"
        f"— заявок и черновиков: {counts['pending']}\n"
        f"— записей об опубликованных: {counts['published']}\n"
        f"— записей в истории: {counts['history']}\n\n"
        "Привязки партнёров к сотрудникам сохранятся — заходить по ссылке заново не нужно.\n\n"
        "⚠️ Посты в канале бот удалить не может. Если тестовые публикации "
        f"ещё висят в {html.escape(CHANNEL_USERNAME)}, удалите их в Telegram руками — "
        "иначе они останутся без карточки в боте."
        f"{payments_note}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить всё", callback_data="reset_data_confirm")],
            [InlineKeyboardButton("Отмена", callback_data="reset_data_cancel")],
        ]),
    )


async def admin_reset_data_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("У вас нет прав для этого действия", show_alert=True)
        return

    if query.data == "reset_data_cancel":
        await query.answer("Отменено")
        await update_admin_action_message(query, "Сброс отменён. Ничего не удалено.")
        return

    removed = reset_listing_data(context.application.bot_data)
    await persist_now(context.application)
    await query.answer("Удалено")
    logger.warning(
        "Администратор выполнил полный сброс данных объявлений: %s", removed
    )
    await update_admin_action_message(
        query,
        "✅ Память очищена.\n\n"
        f"Удалено заявок и черновиков: {removed['pending']}\n"
        f"Удалено записей об опубликованных: {removed['published']}\n"
        f"Удалено записей в истории: {removed['history']}\n"
        f"Сброшено незавершённых шагов: {removed['states']}\n\n"
        "Статистика начинается с нуля.",
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
    await persist_now(context.application)
    await update.effective_message.reply_text(
        f"Карточка {listing_id} повторно доставлена в чат проверки."
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


def stats_employee_code(item, context=None):
    """Определяет сотрудника, чей контакт видит клиент в этом объявлении.

    Лид уходит тому, чей контакт стоит в посте, поэтому контакт и есть главный
    признак. Проверяем по очереди: сохранённый код сотрудника, затем контактную
    ссылку в самом объявлении, затем привязку автора к сотруднику.

    Контакт по умолчанию здесь тоже засчитывается: он совпадает со ссылкой
    Ивана, и клиент из такого поста звонит именно ему. Прежняя версия его
    пропускала, и все старые объявления с этим контактом исчезали из разбивки.
    """
    code = canonical_employee_code(item.get("employee_code"))
    if code in EMPLOYEES:
        return code

    contact_url = item.get("contact_url")
    if contact_url:
        code = canonical_employee_code(employee_key_by_contact(contact_url))
        if code in EMPLOYEES:
            return code

    # Старые записи могли не сохранить ни код, ни контакт сотрудника.
    # Тогда берём привязку автора: по чьей ссылке он вообще работает.
    if context is not None:
        partner_id = item.get("partner_id")
        if partner_id is not None:
            code = canonical_employee_code(
                context.application.bot_data.get(f"partner_code_{partner_id}")
            )
            if code in EMPLOYEES:
                return code
            code = canonical_employee_code(
                employee_key_by_contact(
                    context.application.bot_data.get(f"contact_{partner_id}", "")
                )
            )
            if code in EMPLOYEES:
                return code
    return None




def stats_all_items(context):
    """Единый список объявлений без двойного счёта pending/history/published."""
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

    return list(items.values())


def stats_user_items(context, user_id):
    return [item for item in stats_all_items(context) if item.get("partner_id") == user_id]


def stats_employee_rows(context):
    """Срез по сотрудникам Binio: чей контакт стоит в объявлениях.

    Клиент из канала звонит тому сотруднику, чей контакт указан в посте,
    поэтому объявление засчитывается сотруднику независимо от того, кто его
    завёл — сам сотрудник или привлечённый им партнёр. Эти два источника
    считаются и по отдельности: own_* — объявления самого сотрудника,
    partners — сколько разных партнёров публикуется по его ссылке.

    Привязанные партнёры без единого объявления тоже попадают в partners:
    иначе сотрудник не видит, что человек подключился, но не работает.
    """
    rows = {
        key: {
            "partners": set(),
            "listings": set(),
            "published": 0,
            "active": 0,
            "own_listings": 0,
            "own_published": 0,
        }
        for key in EMPLOYEE_CHOICE_KEYS
    }

    # Telegram id сотрудников: их собственные объявления не должны попадать
    # в счётчик «привлечённых партнёров».
    employee_user_ids = {str(user_id) for user_id in EMPLOYEE_STATS_OWNER_BY_ID}
    employee_user_ids.add(str(ADMIN_TELEGRAM_ID))

    # Объявления, по которым сотрудника определить не удалось. Их нельзя
    # просто пропускать: иначе сумма по сотрудникам молча не сходится с общим
    # числом объявлений, и статистике перестаёшь верить.
    rows[UNASSIGNED_EMPLOYEE_KEY] = {
        "partners": set(), "listings": set(), "published": 0, "active": 0,
        "own_listings": 0, "own_published": 0,
    }

    for item in stats_all_items(context):
        code = stats_employee_code(item, context) or UNASSIGNED_EMPLOYEE_KEY
        if code not in rows:
            continue
        row = rows[code]
        partner_id = item.get("partner_id")
        is_own = str(partner_id) in employee_user_ids

        listing_id = item.get("listing_id")
        if listing_id:
            row["listings"].add(listing_id)
        if not is_own and partner_id is not None:
            # Ключи bot_data хранят id строкой, а в заявке лежит число:
            # приводим к одному виду, иначе один партнёр посчитается дважды.
            row["partners"].add(str(partner_id))
        if is_own:
            row["own_listings"] += 1

        if item.get("published_at"):
            row["published"] += 1
            if is_own:
                row["own_published"] += 1
            if (
                item.get("status", "active") == "active"
                and not item.get("channel_missing")
                and not item.get("stale")
            ):
                row["active"] += 1

    # Партнёр мог получить ссылку, но ещё ничего не опубликовать.
    prefix = "partner_code_"
    for key, value in context.application.bot_data.items():
        if not key.startswith(prefix):
            continue
        code = canonical_employee_code(value)
        if code not in rows:
            continue
        user_id = key[len(prefix):]
        if user_id and user_id not in employee_user_ids:
            rows[code]["partners"].add(user_id)

    return rows


def stats_percent(numerator, denominator):
    if not denominator:
        return "—"
    return f"{(100 * numerator / denominator):.0f}%"


def stats_counters(items):
    """Единый набор счётчиков для личной и общей статистики."""
    return {
        "total": len(items),
        "submitted": sum(
            1 for item in items
            if item.get("submitted_to_admin") or item.get("published_at")
        ),
        "published": sum(1 for item in items if item.get("published_at")),
        "active": sum(
            1 for item in items
            if item.get("published_at")
            and item.get("status", "active") == "active"
            and not item.get("channel_missing")
            and not item.get("stale")
        ),
        "rented": sum(1 for item in items if item.get("status") == "rented"),
        "rejected": sum(
            1 for item in items
            if item.get("history_status") == "rejected"
            or item.get("listing_status") == "rejected"
        ),
    }


def stats_partner_ids(context):
    """Пользователи, у которых сохранён партнёрский доступ."""
    partner_ids = set()
    for key, value in context.application.bot_data.items():
        if key.startswith("partner_code_") and canonical_employee_code(value) in EMPLOYEES:
            partner_ids.add(key.replace("partner_code_", "", 1))
        elif key.startswith("contact_") and value in set(EMPLOYEES.values()) and value != DEFAULT_CONTACT:
            partner_ids.add(key.replace("contact_", "", 1))
    return partner_ids


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Личная статистика публикующего: только его собственные объявления.

    Партнёру нужны его публикации, а не чужие показатели, поэтому здесь нет
    ничего про сотрудников и привлечённых партнёров — этот срез живёт в /stats
    у администратора.
    """
    user = update.effective_user
    if not user:
        return
    if is_admin(user.id):
        # Для администратора /mystats — привычное имя общей статистики.
        await admin_stats(update, context)
        return

    items = stats_user_items(context, user.id)
    counters = stats_counters(items)
    in_progress = counters["total"] - counters["submitted"] - counters["rejected"]

    employee_code = canonical_employee_code(
        context.application.bot_data.get(f"partner_code_{user.id}")
    )
    if employee_code not in EMPLOYEES:
        employee_code = employee_key_by_contact(
            context.application.bot_data.get(f"contact_{user.id}", "")
        )

    lines = ["<b>Моя статистика</b>", ""]
    lines.append(f"Всего создано: {counters['total']}")
    if in_progress > 0:
        lines.append(f"Черновики: {in_progress}")
    lines.append(f"Отправлено на проверку: {counters['submitted']}")
    lines.append(f"Опубликовано: {counters['published']}")
    lines.append(f"Сейчас в канале: {counters['active']}")
    if counters["rented"]:
        lines.append(f"Отмечено «сдано»: {counters['rented']}")
    if counters["rejected"]:
        lines.append(f"Отклонено: {counters['rejected']}")

    # Заявки, по которым решение ещё не принято, в конверсию не входят:
    # ожидание в очереди — это не отказ.
    decided = counters["published"] + counters["rejected"]
    awaiting = counters["submitted"] - decided
    if awaiting > 0:
        lines.append(f"Ждут решения: {awaiting}")
    # Процент от одной-двух заявок ничего не значит и только пугает новичка.
    if decided >= 3:
        lines.append("")
        lines.append(
            f"Одобрено: {stats_percent(counters['published'], decided)} "
            f"из {decided} рассмотренных"
        )

    if employee_code in EMPLOYEES:
        lines.append("")
        lines.append(
            f"Контакт для клиентов в ваших объявлениях: "
            f"{html.escape(employee_display_name(employee_code))}. Сменить: /employee"
        )

    lines.append("")
    lines.append("Сами объявления: /mylistings")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Короткая общая сводка. Полная версия — /stats_full."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору")
        return

    all_items = stats_all_items(context)
    counters = stats_counters(all_items)
    pending_items = list(list_unique_pending_items(context).values())
    pending_submitted = sum(1 for item in pending_items if item.get("submitted_to_admin"))
    pending_drafts = len(pending_items) - pending_submitted

    employee_rows = stats_employee_rows(context)
    # Сотрудники с наибольшим числом активных объявлений — сверху.
    ranked = sorted(
        (key for key in EMPLOYEE_CHOICE_KEYS
         if employee_rows[key]["listings"] or employee_rows[key]["partners"]),
        key=lambda key: (employee_rows[key]["active"], employee_rows[key]["published"]),
        reverse=True,
    )
    employee_lines = []
    for employee_key in ranked:
        row = employee_rows[employee_key]
        partner_listings = len(row["listings"]) - row["own_listings"]
        employee_lines.append(
            f"— {html.escape(employee_display_name(employee_key))}: "
            f"в канале {row['active']}, "
            f"опубликовано {row['published']} "
            f"(свои {row['own_published']}), "
            f"партнёров {len(row['partners'])} → {partner_listings} объявл."
        )
    unassigned = employee_rows[UNASSIGNED_EMPLOYEE_KEY]
    if unassigned["listings"]:
        employee_lines.append(
            f"— Контакт не определён: {len(unassigned['listings'])} объявл., "
            f"в канале {unassigned['active']}"
        )
    if not employee_lines:
        employee_lines.append("— пока нет данных")

    lines = [
        "<b>Статистика Binio</b>",
        "",
        f"Ждут вашего решения: {pending_submitted}",
        f"Черновики у партнёров: {pending_drafts}",
        "",
        f"Сейчас в канале: {counters['active']}",
        f"Опубликовано всего: {counters['published']}",
        f"Сдано: {counters['rented']}",
        f"Отклонено: {counters['rejected']}",
        f"Объявлений заведено: {counters['total']}",
        f"Партнёров с доступом: {len(stats_partner_ids(context))}",
        "",
        "<b>По сотрудникам — чей контакт в объявлении</b>",
        *employee_lines,
        "",
        f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "Подробности: /stats_full",
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def admin_stats_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подробная статистика и техническое состояние памяти."""
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

    all_items = stats_all_items(context)
    counters = stats_counters(all_items)
    employee_rows = stats_employee_rows(context)
    employee_lines = []
    for employee_key in EMPLOYEE_CHOICE_KEYS:
        row = employee_rows[employee_key]
        partner_listings = len(row["listings"]) - row["own_listings"]
        employee_lines.append(
            f"— {html.escape(employee_display_name(employee_key))}: "
            f"сейчас в канале {row['active']}, опубликовано {row['published']}\n"
            f"   свои: {row['own_listings']} заведено / {row['own_published']} опубликовано\n"
            f"   партнёры: {len(row['partners'])} чел. / "
            f"{partner_listings} заведено / {row['published'] - row['own_published']} опубликовано"
        )
    unassigned_row = employee_rows[UNASSIGNED_EMPLOYEE_KEY]
    if unassigned_row["listings"]:
        employee_lines.append(
            f"— Контакт не определён: {len(unassigned_row['listings'])} заведено / "
            f"{unassigned_row['published']} опубликовано / "
            f"{unassigned_row['active']} в канале\n"
            "   это старые записи без сохранённого сотрудника; "
            "новые объявления всегда привязываются"
        )

    # Видно сразу, чей telegram id бот принял, а чей нет: без него объявления
    # сотрудника попадают в графу «партнёры».
    employee_setup_lines = []
    recognized = {code: user_id for user_id, code in EMPLOYEE_STATS_OWNER_BY_ID.items()}
    for employee_key in EMPLOYEE_CHOICE_KEYS:
        name = html.escape(employee_display_name(employee_key))
        if employee_key in recognized:
            employee_setup_lines.append(f"— {name}: id {recognized[employee_key]} ✓")
        elif employee_key == employee_key_by_contact(DEFAULT_CONTACT):
            employee_setup_lines.append(f"— {name}: администратор, id не нужен ✓")
        else:
            employee_setup_lines.append(
                f"— {name}: id не задан, её объявления считаются партнёрскими"
            )
    for problem in EMPLOYEE_TELEGRAM_IDS_PROBLEMS:
        employee_setup_lines.append(f"⚠️ {html.escape(problem)}")

    storage_status = (
        "Volume подключён"
        if BOT_DATA_PATH.startswith("/data") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        else "локальная папка сервиса"
    )

    text = (
        "<b>Подробная статистика Binio</b>\n\n"
        "<b>Заявки в работе:</b>\n"
        f"— сохранено в памяти бота: {pending_total}\n"
        f"— отправлены на проверку: {pending_submitted}\n"
        f"— черновики и предпросмотры: {pending_drafts}\n\n"
        "<b>По всем сохранённым объявлениям:</b>\n"
        f"— уникальных объявлений: {counters['total']}\n"
        f"— дошли до проверки: {counters['submitted']}\n"
        f"— опубликовано: {counters['published']}\n"
        f"— отклонено: {counters['rejected']}\n"
        f"— ждут вашего решения: {pending_submitted}\n"
        # Считаем только по решённым заявкам: пока объявление лежит в очереди,
        # это не отказ, и записывать его в неудачу нельзя.
        f"— одобрено из решённых: "
        f"{stats_percent(counters['published'], counters['published'] + counters['rejected'])}\n\n"
        "<b>Опубликованные объявления:</b>\n"
        f"— всего записей: {published_total}\n"
        f"— активные: {published_active}\n"
        f"— сдано: {published_rented}\n"
        f"— снято: {published_removed}\n"
        f"— отсутствуют в канале: {published_missing}\n"
        f"— авторов объявлений: {len(published_partner_ids)}\n\n"
        "<b>Партнёры:</b>\n"
        f"— с привязкой к сотруднику: {len(stats_partner_ids(context))}\n\n"
        "<b>По сотрудникам:</b>\n"
        + "\n".join(employee_lines) +
        "\n\n<b>Настройка EMPLOYEE_TELEGRAM_IDS:</b>\n"
        + "\n".join(employee_setup_lines) +
        "\n\n<b>Хранение данных:</b>\n"
        f"— файл памяти: {html.escape(BOT_DATA_PATH)}\n"
        f"— режим: {storage_status}\n\n"
        "<b>Автоочистка памяти:</b>\n"
        f"— черновики удаляются через: {BOT_DRAFT_TTL_DAYS} дней\n"
        f"— заявки на проверке хранятся: {BOT_SUBMITTED_TTL_DAYS} дней\n"
        f"— временные шаги пользователя: {BOT_TRANSIENT_TTL_DAYS} дней\n"
        f"— сейчас очищено: {cleanup_summary_text(removed)}\n\n"
        f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Статистика строится по сохранённым данным; очищенные записи в неё не входят.\n"
        "Лишние черновики: /clearpending"
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
    in_filter = filter_published_listings(all_listings, filter_key)
    query = listings_search_query(context, user_id)
    listings = apply_listings_search(in_filter, query)
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
        if query:
            text = (
                f"По запросу «{query}» ничего не найдено "
                f"в разделе «{PUBLISHED_LISTING_FILTERS[filter_key]}».\n\n"
                "Попробуйте другое слово — например район, улицу или планировку."
            )
        elif all_listings:
            text = (
                f"Мои объявления · {PUBLISHED_LISTING_FILTERS[filter_key]}\n\n"
                "В этом разделе пока нет объявлений. Выберите другой фильтр или создайте новую публикацию."
            )
        else:
            text = (
                "У вас пока нет опубликованных объявлений.\n\n"
                "Когда объявление пройдёт проверку и появится в канале, оно будет доступно здесь."
            )
        keyboard = published_list_keyboard(
            [], 0, filter_key, counts, total_in_filter=len(in_filter), query=query)
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
                return
            except Exception:
                pass
        await target.reply_text(text, reply_markup=keyboard)
        return

    notice_prefix = f"{notice}\n\n" if notice else ""
    found_line = (
        f"Найдено по запросу «{query}»: {len(listings)} из {len(in_filter)}\n"
        if query else f"В разделе: {len(listings)}\n"
    )
    text = notice_prefix + (
        f"Мои объявления · {PUBLISHED_LISTING_FILTERS[filter_key]}\n"
        + found_line
        + f"Основные: {counts['all']} · Архив: {counts['archive']}\n"
        f"Страница {page + 1} из {total_pages}\n\n"
        "Выберите объявление, чтобы открыть управление.\n\n"
        "Внутри можно отметить объект как сданный или изменить цену, залог и комиссию.\n"
        + (
            "В архиве карточки скрыты только из основного списка; посты, память и статистика сохраняются."
            if filter_key == "archive"
            else "Проверка наличия постов в канале выполняется автоматически."
        )
    )
    keyboard = published_list_keyboard(
        listings, page, filter_key, counts, total_in_filter=len(in_filter), query=query)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
            return
        except Exception:
            pass
    await target.reply_text(text, reply_markup=keyboard)


async def partner_apply_listings_search(update, context, raw_query):
    """Запоминает поисковый запрос и показывает отфильтрованный список."""
    user_id = update.effective_user.id
    query = re.sub(r"\s+", " ", str(raw_query or "").strip())[:60]
    if not query:
        await update.message.reply_text(
            "Пустой запрос. Отправьте слово из объявления или нажмите /cancel"
        )
        return
    context.application.bot_data[f"listings_query_{user_id}"] = query
    set_state(context, user_id, "done")
    await partner_my_listings(
        update, context, page=0,
        filter_key=current_published_filter(context, user_id),
    )


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

    if data == "my_listings_search":
        set_state(context, user_id, "searching_listings")
        await query.answer()
        await query.message.reply_text(
            "Что искать среди ваших объявлений?\n\n"
            "Отправьте слово из объявления: район, улицу, планировку или сумму.\n"
            "Например: Vysočany, 3+kk, 25 000\n\n"
            "Отменить поиск: /cancel"
        )
        return

    if data == "my_listings_search_clear":
        context.application.bot_data.pop(f"listings_query_{user_id}", None)
        await query.answer("Поиск сброшен")
        await partner_my_listings(
            update, context, page=0, answer_query=False,
            filter_key=current_published_filter(context, user_id),
        )
        return

    if data == "my_listings_new":
        if get_state(context, user_id) == "processing":
            await query.answer("Текущее объявление ещё обрабатывается. Дождитесь результата.", show_alert=True)
            return
        await query.answer()
        await start_partner_flow(query.message, context, update.effective_user)
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
            was_stale = bool(item.get("stale"))
            item.pop("hidden_from_list", None)
            item.pop("hidden_at", None)
            item.pop("hidden_reason", None)
            notice = "Объявление возвращено в основной список. Канал, статистика и память сохранены."
            answer = "Возвращено в список"
            if was_stale:
                # Возврат из архива — это и есть подтверждение актуальности:
                # снимаем пометку в канале и заново отсчитываем полгода.
                item.pop("stale", None)
                item.pop("stale_at", None)
                item.pop("stale_reminded_at", None)
                item["freshness_confirmed_at"] = now_iso()
                try:
                    await edit_published_channel_posts(
                        context, item,
                        listing_with_status(item.get("listing", ""), item.get("status", "active")),
                    )
                    notice = (
                        "Объявление снова в списке, пометка «неактуально» в канале снята. "
                        "Следующее напоминание — примерно через полгода."
                    )
                except Exception as error:
                    logger.info("Не удалось снять пометку в канале: %s", error)
                    notice = (
                        "Объявление снова в списке, но пометку в посте снять не удалось — "
                        "поправьте пост в канале вручную."
                    )
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
            f"Новое значение: {field['line_label'].lower()}.\n\n"
            "Отправьте сообщением, например: 20 000 Kč\n"
            "Отменить: /cancel"
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


async def listing_freshness_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ партнёра на напоминание о залежавшемся объявлении."""
    query = update.callback_query
    user_id = update.effective_user.id
    action, _, listing_id = query.data.replace("fresh_", "", 1).partition("_")
    item = get_published(context, listing_id)

    if not item or item.get("partner_id") != user_id:
        await query.answer("Объявление не найдено.", show_alert=True)
        return

    if action == "keep":
        item["freshness_confirmed_at"] = now_iso()
        item.pop("stale_reminded_at", None)
        item.pop("stale", None)
        item.pop("stale_at", None)
        if item.get("hidden_reason") == "stale":
            item.pop("hidden_from_list", None)
            item.pop("hidden_at", None)
            item.pop("hidden_reason", None)
        # Если пост уже успели пометить, снимаем пометку.
        try:
            await edit_published_channel_posts(
                context, item,
                listing_with_status(item.get("listing", ""), item.get("status", "active")),
            )
        except Exception as error:
            logger.info("Не удалось обновить пост после подтверждения: %s", error)
        save_published(context, listing_id, item)
        await persist_now(context.application)
        await query.answer("Отмечено как актуальное")
        await update_admin_action_message(
            query,
            "✅ Спасибо, объявление остаётся активным.\n\n"
            "Следующий раз спрошу примерно через полгода.",
        )
        return

    if action == "rented":
        if item.get("channel_missing") or item.get("status") == "removed":
            await query.answer("Пост отсутствует в канале.", show_alert=True)
            return
        await query.answer("Обновляю пост")
        try:
            await edit_channel_listing_status(context, item, "rented")
        except Exception as error:
            logger.error("fresh_rented error: %s", error)
            await query.message.reply_text("Не получилось обновить пост в канале. Попробуйте позже.")
            return
        updated = get_published(context, listing_id)
        updated.pop("stale_reminded_at", None)
        updated["freshness_confirmed_at"] = now_iso()
        save_published(context, listing_id, updated)
        await persist_now(context.application)
        await update_admin_action_message(query, "🔴 Отмечено как сдано. В посте появилась пометка.")
        return

    if action == "archive":
        await query.answer("Убираю в архив")
        await mark_listing_stale(context, item)
        await persist_now(context.application)
        await update_admin_action_message(
            query,
            "🗄 Объявление убрано в архив, в канале пост помечен как неактуальный.\n\n"
            "Вернуть можно через /mylistings → Архив.",
        )


async def admin_reject_reason_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает ввод причины отклонения. Шаг необязательный."""
    query = update.callback_query
    if not is_admin_edit_sender(update):
        await query.answer("У вас нет прав для этого действия", show_alert=True)
        return

    listing_id = query.data.replace("reject_reason_", "", 1)
    notice = context.application.bot_data.get(reject_notice_key(listing_id))
    if not isinstance(notice, dict):
        await query.answer(
            "Это отклонение уже слишком старое — напишите партнёру напрямую.",
            show_alert=True,
        )
        return

    bot_data = context.application.bot_data
    # Режим правки объявления и режим причины не должны пересекаться.
    clear_admin_edit_session(bot_data)
    bot_data["admin_reason_listing_id"] = listing_id
    await query.answer()

    reply_markup = admin_edit_reply_markup(getattr(query.message, "chat", None))
    if reply_markup is None:
        prompt = (
            "Отправьте следующим сообщением текст причины — бот перешлёт его партнёру.\n\n"
            "Чтобы передумать: /cancel_reason"
        )
    else:
        prompt = (
            "Напишите причину в поле ответа — бот перешлёт её партнёру.\n\n"
            "Важно: сообщение должно быть ответом именно на эту подсказку.\n"
            "Чтобы передумать: /cancel_reason"
        )
    prompt_message = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID, text=prompt, reply_markup=reply_markup)
    prompt_message_id = getattr(prompt_message, "message_id", None)
    if reply_markup is not None and prompt_message_id is not None:
        bot_data["admin_reason_prompt_message_id"] = prompt_message_id


async def admin_cancel_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_edit_sender(update):
        await update.effective_message.reply_text("Эта команда доступна только администратору")
        return
    clear_reject_reason_session(context.application.bot_data)
    await update.effective_message.reply_text(
        "Хорошо, причину не отправляю. Партнёр уже получил уведомление об отклонении."
    )


async def send_reject_reason(update, context, reason_text):
    """Пересылает партнёру пояснение администратора."""
    bot_data = context.application.bot_data
    listing_id = bot_data.get("admin_reason_listing_id")
    notice = bot_data.get(reject_notice_key(listing_id)) if listing_id else None
    message = update.effective_message

    if not isinstance(notice, dict):
        clear_reject_reason_session(bot_data)
        await message.reply_text(
            "Не понимаю, к какому объявлению относится причина. "
            "Нажмите «Написать причину» под нужным отклонением ещё раз."
        )
        return

    reason = str(reason_text or "").strip()
    if len(reason) > 1500:
        await message.reply_text(
            f"Слишком длинно: {len(reason)} символов при лимите 1500. Сократите и отправьте снова."
        )
        return

    partner_id = notice.get("partner_id")
    headline = notice.get("headline") or "Объявление"
    try:
        await context.bot.send_message(
            chat_id=partner_id,
            text=(
                "❌ Администратор пояснил, почему объявление отклонено:\n\n"
                f"<i>{html.escape(headline)}</i>\n\n"
                f"{html.escape(reason)}\n\n"
                "Новое объявление с учётом замечаний: /start"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as error:
        logger.warning("Не удалось отправить причину отклонения: %s", error)
        await message.reply_text(
            f"Не получилось доставить причину партнёру: {error}\n\n"
            "Возможно, он заблокировал бота — напишите ему напрямую."
        )
        clear_reject_reason_session(bot_data)
        return

    clear_reject_reason_session(bot_data)
    bot_data.pop(reject_notice_key(listing_id), None)
    await persist_now(context.application)
    await message.reply_text("✅ Причина отправлена партнёру.")


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
        post_url = channel_post_url(CHANNEL_USERNAME, channel_message_id) if channel_message_id else None
        if channel_message_id:
            published_data = {
                "listing_id": listing_id,
                "partner_id": partner_id,
                "partner_label": pending.get("partner_label"),
                "source": pending.get("source", "partner"),
                "employee_code": pending.get("employee_code"),
                "submitted_at": pending.get("submitted_at"),
                # visible_listing не храним: текст с пометкой «СДАНО»
                # собирается из listing и статуса функцией listing_with_status.
                "listing": strip_listing_status(listing),
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

        delete_pending(context, listing_id)
        clear_admin_edit_session(context.application.bot_data, listing_id)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)
        # Пост уже существует во внешнем мире — сразу фиксируем локальную запись
        # до косметического обновления кнопки и уведомления пользователя.
        await persist_now(context.application)

        await update_admin_action_message(query, f"✅ Опубликовано в {CHANNEL_USERNAME}")

        if partner_id is not None and channel_message_id:
            try:
                text = "✅ Ваше объявление опубликовано. Теперь оно доступно в разделе «Мои объявления»."
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
                logger.warning(f"Не удалось уведомить партнёра {partner_id} о публикации: {e}")

    elif data.startswith("edit_more_"):
        listing_id = data.split("_", 2)[2]
        pending = get_pending(context, listing_id)
        if pending:
            if pending.get("publish_state") in {"unknown", "sending"}:
                await query.message.reply_text(
                    "⚠️ Результат публикации пока неизвестен. Редактирование остановлено, чтобы текст в боте "
                    "не разошёлся с возможным постом в канале. Сначала проверьте последние публикации."
                )
                return
            if pending_busy(pending, "admin_action_in_progress"):
                await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
                return
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
        if pending.get("publish_state") in {"unknown", "sending"}:
            await query.message.reply_text(
                "⚠️ Результат публикации пока неизвестен. Отклонение остановлено, чтобы не удалить заявку, "
                "если пост уже появился в канале. Сначала проверьте последние публикации."
            )
            return
        if pending_busy(pending, "admin_action_in_progress"):
            await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
            return
        mark_pending_busy(context, listing_id, pending, "admin_action_in_progress", "reject")
        partner_id = pending.get('partner_id')
        save_listing_history(
            context,
            listing_id,
            pending,
            "rejected",
            rejection_reason="admin_rejected",
        )
        delete_pending(context, listing_id)
        clear_admin_edit_session(context.application.bot_data, listing_id)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)
        headline = listing_headline(pending.get("formatted_listing", "")) or "Объявление"
        if partner_id is not None:
            # Запоминаем, кому писать, если администратор решит пояснить причину:
            # сама заявка к этому моменту уже удалена.
            context.application.bot_data[reject_notice_key(listing_id)] = {
                "partner_id": partner_id,
                "headline": headline[:120],
                "created_at": now_iso(),
            }
        await persist_now(context.application)
        await update_admin_action_message(query, "❌ Объявление отклонено.")
        if partner_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=partner_id,
                    text="❌ Ваше объявление отклонено администратором.",
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {partner_id} об отклонении: {e}")
            # Причина — дело добровольное: кнопка появляется, но нажимать её
            # не обязательно, отклонение уже состоялось.
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"Отклонено: {html.escape(headline)}\n\nПояснить партнёру причину?",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✍️ Написать причину",
                            callback_data=f"reject_reason_{listing_id}",
                        )
                    ]]),
                )
            except Exception as e:
                logger.info(f"Не удалось предложить написать причину: {e}")


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

    bot_data = context.application.bot_data
    # Режим причины отклонения перехватывает текст раньше правки объявления.
    if bot_data.get("admin_reason_listing_id"):
        prompt_id = bot_data.get("admin_reason_prompt_message_id")
        reply_to = getattr(getattr(message, "reply_to_message", None), "message_id", None)
        if prompt_id is not None and reply_to != prompt_id:
            await message.reply_text(
                "⚠️ Текст не привязан к вводу причины.\n\n"
                "Отправьте его через поле ответа под подсказкой. "
                "Передумали — /cancel_reason"
            )
            return
        await send_reject_reason(update, context, message.text)
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
                "⚠️ Произошла техническая ошибка\n\n"
                "Начатое объявление сохранено — откройте его через /drafts.\n"
                "Новое объявление: /start"
            )
    except Exception:
        pass


def main():
    validate_config()
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    logger.info(f"Файл памяти бота: {BOT_DATA_PATH}")
    # Фоновое автосохранение переписывает файл целиком, поэтому при большой базе
    # частый интервал — основная нагрузка на диск. Критические моменты (отправка
    # на проверку, публикация в канал) сохраняются немедленно через persist_now,
    # так что редкий фоновый интервал ничего важного не теряет.
    persistence = AtomicPicklePersistence(
        filepath=BOT_DATA_PATH,
        update_interval=BOT_AUTOSAVE_INTERVAL_SECONDS,
    )
    app = (
        Application.builder()
        .token(PARTNER_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .post_shutdown(stop_background_tasks)
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
    app.add_handler(CommandHandler("mylistings", partner_my_listings))
    app.add_handler(CommandHandler("drafts", list_drafts))
    app.add_handler(CommandHandler("cancel", cancel_current_step))
    app.add_handler(CommandHandler("employee", employee_change_start))
    app.add_handler(CommandHandler("mystats", my_stats))
    # Команды отключённой разовой публикации остались у людей в истории.
    for discontinued in ("owner", "publish", "terms", "support"):
        app.add_handler(CommandHandler(discontinued, discontinued_command))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("stats_full", admin_stats_full))
    app.add_handler(CommandHandler("memory", admin_memory))
    app.add_handler(CommandHandler("clearpending", admin_clear_pending))
    app.add_handler(CommandHandler("reset_data", admin_reset_data))
    app.add_handler(CommandHandler("cancel_reason", admin_cancel_reason))
    app.add_handler(CommandHandler("retry_review", admin_retry_review))
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

    app.add_handler(CallbackQueryHandler(employee_selected, pattern=r"^employee_[A-Za-z0-9_]+$"))

    # Личный кабинет партнёра: опубликованные объявления и статус "сдано"
    app.add_handler(CallbackQueryHandler(
        partner_published_callback,
    pattern=r"^(my_listings(?:_page_(?:[0-9]+|(?:all|active|rented|archive)_[0-9]+)|_check_[0-9]+|_filter_(?:all|active|rented|archive)|_new|_search|_search_clear)?|pub_view_[A-Za-z0-9_-]+|pub_hide_[A-Za-z0-9_-]+|pub_archive_info_[A-Za-z0-9_-]+|pub_archive_[A-Za-z0-9_-]+|pub_unarchive_[A-Za-z0-9_-]+|pub_rented_[A-Za-z0-9_-]+|pub_active_[A-Za-z0-9_-]+|pub_money_(price|deposit|commission)_[A-Za-z0-9_-]+)$"
    ))

    # Старые кнопки из прежних сообщений. Регистрируются до partner_submit,
    # чтобы submit_paid_public_* не был разобран как обычная отправка.
    app.add_handler(CallbackQueryHandler(
        discontinued_button,
        pattern=r"^(pay_public_[A-Za-z0-9_-]+|test_pay_public_[A-Za-z0-9_-]+"
                r"|submit_paid_public_[A-Za-z0-9_-]+|role_public)$",
    ))
    # Старая кнопка «Партнёр / риэлтор» просто открывает партнёрский сценарий.
    app.add_handler(CallbackQueryHandler(legacy_role_partner, pattern=r"^role_partner$"))

    # Выбор типа объекта перед описанием
    app.add_handler(CallbackQueryHandler(property_type_selected, pattern=r"^property_type_[A-Za-z0-9_]+$"))

    # Кнопки партнёра
    app.add_handler(CallbackQueryHandler(partner_submit, pattern=r"^submit_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_regenerate, pattern=r"^regen_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_edit_request, pattern=r"^partner_edit_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(resume_draft, pattern=r"^draft_resume_[A-Za-z0-9_-]+$"))

    # Кнопки админа
    app.add_handler(CallbackQueryHandler(
        listing_freshness_callback,
        pattern=r"^fresh_(keep|rented|archive)_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(
        admin_reset_data_callback, pattern=r"^reset_data_(confirm|cancel)$"))
    app.add_handler(CallbackQueryHandler(
        admin_reject_reason_start, pattern=r"^reject_reason_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(
        admin_callback, pattern=r"^(approve|reject|edit_more)_[A-Za-z0-9_-]+$"))

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



