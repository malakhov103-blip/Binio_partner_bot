import asyncio
import html
import logging
import os
import platform
import re
import uuid
from datetime import datetime, timezone
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LabeledPrice, BotCommand
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, PicklePersistence, PreCheckoutQueryHandler
)

# ============================================================
# НАСТРОЙКИ
#
# Railway/GitHub версия: токены не хранятся в коде.
# Все секреты задаются через Railway Variables.

#



def clean_env(name, default=""):
    return os.getenv(name, default).strip()

PARTNER_BOT_TOKEN = clean_env("PARTNER_BOT_TOKEN")
GEMINI_API_KEY = clean_env("GEMINI_API_KEY")

ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "894394087"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004484453420"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@binio_praha")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
PUBLIC_LISTING_PRICE_CZK = int(os.getenv("PUBLIC_LISTING_PRICE_CZK", "25"))
PUBLIC_PAYMENT_CURRENCY = "CZK"
PUBLIC_PAYMENT_AMOUNT = PUBLIC_LISTING_PRICE_CZK * 100
PUBLIC_PAYMENT_TEST_MODE = os.getenv("PUBLIC_PAYMENT_TEST_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "partner_bot_data.pickle")
BOT_DATA_PATH = os.path.join(BOT_DATA_DIR, BOT_DATA_FILE)

EMPLOYEES = {
    "ivan": "https://t.me/malakhov_prague",
    "ivan2": "https://t.me/malakhov_prague",
    "irina": "https://t.me/binio_irina",
    "irina2": "https://t.me/binio_irina",
    "vera": "https://t.me/VeraGryshyna",
    "vera2": "https://t.me/VeraGryshyna",
}
EMPLOYEE_NAMES = {
    "ivan": "Иван",
    "ivan2": "Иван",
    "irina": "Ирина",
    "irina2": "Ирина",
    "vera": "Вера",
    "vera2": "Вера",
}
EMPLOYEE_CHOICE_KEYS = ("ivan", "irina", "vera")
DEFAULT_CONTACT = "https://t.me/malakhov_prague"
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("partner_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
TELEGRAM_CAPTION_LIMIT = 1024
LISTING_SOFT_LIMIT = 930

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

LISTING_TEMPLATE = """
Ты редактор объявлений о недвижимости для русскоязычной аудитории в Праге. Твоя задача — превратить сырой текст
от партнёра в живое, понятное и естественное объявление об аренде — такое, которое хочется дочитать до конца,
а не сухую техническую справку.

ТИП ОБЪЕКТА, ВЫБРАННЫЙ ПАРТНЁРОМ:
{property_type_rules}

Это правило важнее любых намёков в исходном тексте. Если партнёр выбрал «комната», заголовок не может выглядеть
как объявление о всей квартире. Если выбрал «дом», не называй объект квартирой. Если выбрал «коммерция» или
«нежилое помещение», не используй жилую лексику там, где она вводит в заблуждение.

ЯЗЫК:
- Если текст на чешском — переведи описание на грамотный русский
- Если текст на русском — подправь стиль и орфографию
- Адреса, названия улиц, районов, городов, станций метро и остановок — оставляй как есть (не переводи)
- Итоговое объявление на русском, кроме адресов и географических названий
- Тип объекта, служебные слова и обычные русские слова всегда пиши кириллицей: «Квартира», «Комната», «Дом», «Участок», «Коммерческое помещение». Запрещены транслит и английские/чешские замены вроде «Kvartira», «Komnata», «Apartman», «Room», «House», если это не часть официального названия.

ПРАВИЛА:
- Никогда не придумывай: цену, район, улицу, адрес, метраж, этаж, количество комнат, залог, комиссию, коммунальные платежи
- Если у партнёра указаны коммунальные платежи отдельно от аренды — обязательно включи их отдельной строкой в "Финансовые условия", не пропускай и не объединяй с арендой
- Можно добавить лёгкое описание ("уютная квартира", "светлая планировка") только если это логично следует из контекста
- Используй только те данные которые есть в тексте
- Если какого-то блока нет — просто пропусти его
- Хештеги строго в таком формате — не больше 3 штук:
  1. Тип объекта:
     — квартира: #1kk / #2kk / #2plus1 / #3kk / #3plus1 / #4kk / #4plus1 / #5kk
     — комната: #pokoj
     — дом: #dum
     — участок: #pozemek
     — коммерция или нежилое помещение: #komerce
     — другое: выбери самый близкий из списка выше или пропусти типовой хештег, если он не подходит
  2. Район: #Praha1 / #Praha2 / #Praha3 / #Praha4 / #Praha5 / #Praha6 / #Praha7 / #Praha8 / #Praha9 / #Praha10
  3. Всегда: #pronajem
- Вместо строки с контактом вставь ровно этот текст на отдельной строке, без изменений: [[CONTACT]]
  (это служебный плейсхолдер, его заменят автоматически — не переводи его и не меняй квадратные скобки)

ТОН И СТИЛЬ (важно):
Пиши по-русски так, как говорит живой человек, а не переводчик и не риелторский шаблон. Текст должен звучать
естественно для русскоязычного клиента: короткие фразы, нормальный порядок слов, без буквального перевода с чешского.
Пиши так, будто спокойно рассказываешь знакомому об объекте, который сам посмотрел: одним связным текстом,
а не перечислением фактов через точку. Плохой пример (так писать НЕ надо — звучит как чек-лист,
предложения не связаны друг с другом):
"Светлая и уютная квартира 1+kk (27 м²) в районе Motol, после недавней реконструкции. Она частично
меблирована и уже ждёт новых жильцов. Внутри найдёте современный кухонный блок со всей нужной техникой.
В шаговой доступности есть живописный парк."

Хороший пример (так надо — предложения перетекают одно в другое, есть живая интонация):
"После недавней реконструкции эта светлая 1+kk в Motol встречает частичной меблировкой и современной
кухней — заезжай и живи. Рядом разбит уютный парк, так что по вечерам будет куда прогуляться."

Разница: хороший вариант читается как единая мысль, а не список пунктов. Избегай канцелярита
("предлагается в аренду", "расположена", "оборудована", "объект располагает", "имеется возможность",
"данное помещение") и странных фраз, которые никто не говорит в обычной речи. Не пиши «квартира встречает
меблировкой», «локация предлагает», «пространство порадует» и подобные искусственные обороты. Лучше проще:
«внутри уже есть мебель», «рядом метро», «подойдёт для пары». Тёплые слова можно использовать только там,
где они звучат естественно.

ЧТО НЕЛЬЗЯ ВЫБРАСЫВАТЬ, даже сокращая текст:
- Дата заезда / когда квартира освобождается
- Для кого подходит (один человек / пара / семья), если это указано в исходном тексте
- Разрешены ли животные
- Меблирована ли квартира (полностью / частично / без мебели)
Эти детали короткие, но важны для клиента — они всегда должны попасть в описание, даже если ради этого придётся сократить менее важные подробности (например, длинный список бытовой техники или второстепенных удобств).

ОБЪЁМ:
- Описание квартиры — примерно 30-40 слов, включая обязательные детали выше. Ориентир, не жёсткий лимит — не в ущерб связности текста
- Не нужно перечислять всю технику и все удобства подряд — выбери то, что реально важно, и впиши естественно в текст, а не списком
- В разделе "Локация" — 2-3 варианта, как добраться (метро, трамвай, автобус — сколько есть у партнёра) плюс, если есть в тексте, кратко 1-2 значимых объекта рядом (магазины, парки, ТЦ) — единая картина "что рядом", без отдельного заголовка под это
- Если исходный текст партнёра длиннее — сокращай в первую очередь декоративные детали и перечисления, а не обязательные факты из списка выше
- Весь итоговый текст вместе с контактом и хештегами должен быть не длиннее 930 символов, чтобы текст и фото всегда публиковались единым сообщением

ФОРМАТИРОВАНИЕ:
- Никаких звёздочек-буллитов (* или •) — списки пиши через тире (—) или просто с новой строки
- Никаких эмодзи
- Заголовок объявления (самая первая строка) всегда строится по выбранному типу объекта выше.
  Не используй универсальный заголовок "[планировка], [метраж], [район]", если выбран не тип «квартира».
  Правила заголовка:
  — разделяй части запятыми, никогда не используй скобки для метража
  — единица измерения всегда "м²" через пробел от числа (не "36m2", не "36кв.м")
  — не пиши предлог "в" перед районом — просто указывай район напрямую после тире
  — если в районе два уровня (например Praha 5 и Smíchov) — соединяй их через " – " (тире с пробелами)
  — весь заголовок всегда жирным: <b>Заголовок</b>
- Жирный текст также для заголовков разделов — используй HTML тег: <b>Заголовок:</b>
- Например: <b>Локация:</b>, <b>Финансовые условия:</b>
- Остальной текст без выделений

ПРИМЕР СТРУКТУРЫ (ориентируйся на логику, не копируй дословно):
<b>[Заголовок: тип, метраж, район]</b>

[Живое описание — 30-40 слов, с обязательными деталями]

<b>Локация:</b>
— [остановка]: ~X мин
— [ещё один транспорт, если есть]: ~X мин
— Рядом: [магазины/парк/ТЦ — если упомянуты у партнёра]

<b>Финансовые условия:</b>
— Аренда: X Kč
— Залог: X Kč
— Комиссия: X Kč

[[CONTACT]]

#[тип] #[район] #pronajem

ТЕКСТ ОТ ПАРТНЁРА:
{text}

Верни только готовое объявление (плейсхолдер [[CONTACT]] оставь как есть, на своём месте — его заменят автоматически), без пояснений и комментариев.
"""

SHORTEN_TEMPLATE = """
Сожми готовое объявление ниже до {limit} символов или меньше.

Правила:
- Сохрани HTML-теги <b> только для заголовка и названий разделов
- Не меняй тип объекта в заголовке: квартира остаётся квартирой, комната — комнатой, дом — домом, участок — участком, коммерция — коммерцией
- Не удаляй цену, залог, комиссию, коммунальные платежи, дату заезда, условия по животным и меблировку, если они есть
- Не придумывай новые данные
- Обязательно сохрани строку контакта, если она есть
- Обязательно сохрани хештеги, если они есть
- Верни только готовое объявление, без комментариев

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
    if is_empty_or_placeholder(GEMINI_API_KEY, "PASTE_NEW_GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")

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
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Выбрать тип публикации"),
            BotCommand("partner", "Публикация для партнёра / риэлтора"),
            BotCommand("owner", "Разместить объявление как собственник"),
            BotCommand("mylistings", "Мои объявления партнёра"),
            BotCommand("employee", "Сменить сотрудника для контакта"),
            BotCommand("stats", "Статистика для администратора"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось обновить меню команд Telegram: {e}")


def get_state(context, user_id):
    return context.application.bot_data.get(f"state_{user_id}")


def set_state(context, user_id, state):
    context.application.bot_data[f"state_{user_id}"] = state


def is_admin(user_id):
    return user_id == ADMIN_TELEGRAM_ID


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
    context.application.bot_data[pending_key(listing_id)] = data


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
    context.application.bot_data[published_key(listing_id)] = data


def list_partner_published(context, partner_id):
    listings = []
    for key, value in context.application.bot_data.items():
        if not key.startswith("published_listing_") or not isinstance(value, dict):
            continue
        if value.get("partner_id") == partner_id:
            listings.append(value)
    status_order = {"active": 0, "rented": 1, "removed": 2}
    listings.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    listings.sort(key=lambda item: status_order.get(item.get("status"), 9))
    return listings


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
        r'^\s*(?:<b>)?(?:✅\s*)?СДАНО(?:</b>)?\s*\n+',
        '',
        text,
        flags=re.I,
    ).strip()


def listing_with_status(text, status):
    base = strip_listing_status(text)
    if status == "rented":
        return f"<b>✅ СДАНО</b>\n\n{base}"
    return base


def published_status_label(status):
    if status == "rented":
        return "🔴 Сдано"
    if status == "removed":
        return "⏸ Снято"
    return "🟢 Активно"


FINANCIAL_FIELDS = {
    "price": {
        "label": "цену",
        "line_label": "Аренда",
        "aliases": ["Аренда", "Цена"],
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
        "aliases": ["Комиссия"],
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


def replace_financial_line(text, field_key, value):
    field = get_financial_field(field_key)
    if not field:
        return text

    value = normalize_financial_value(value)
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


def remember_channel_message(item, message, has_photos):
    message_id = getattr(message, "message_id", None)
    if not message_id:
        return False
    chat_id = getattr(getattr(message, "chat", None), "id", CHANNEL_USERNAME)
    entry = {
        "chat_id": chat_id,
        "message_id": message_id,
        "has_photos": bool(has_photos),
    }
    messages = list(published_channel_messages(item))
    if not any(m.get("chat_id") == chat_id and m.get("message_id") == message_id for m in messages):
        messages.append(entry)
    item["channel_messages"] = messages
    item["channel_message_id"] = message_id
    item["channel_chat_id"] = chat_id
    item["channel_post_url"] = channel_post_url(CHANNEL_USERNAME, message_id)
    return True


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
                caption = fit_to_caption(caption)
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


def published_list_keyboard(listings):
    rows = []
    for item in listings[:20]:
        headline = listing_headline(item.get("listing", "Объявление"))
        if len(headline) > 34:
            headline = headline[:31].rstrip() + "..."
        rows.append([
            InlineKeyboardButton(
                f"{published_status_label(item.get('status'))} · {headline}",
                callback_data=f"pub_view_{item['listing_id']}",
            )
        ])
    return InlineKeyboardMarkup(rows)


def published_manage_keyboard(item):
    listing_id = item["listing_id"]
    rows = []
    if item.get("status") == "rented":
        rows.append([InlineKeyboardButton("🟢 Сделать активным", callback_data=f"pub_active_{listing_id}")])
    else:
        rows.append([InlineKeyboardButton("🔴 Отметить как сдано", callback_data=f"pub_rented_{listing_id}")])
        rows.append([
            InlineKeyboardButton(FINANCIAL_FIELDS["price"]["button"], callback_data=f"pub_money_price_{listing_id}"),
            InlineKeyboardButton(FINANCIAL_FIELDS["deposit"]["button"], callback_data=f"pub_money_deposit_{listing_id}"),
        ])
        rows.append([InlineKeyboardButton(FINANCIAL_FIELDS["commission"]["button"], callback_data=f"pub_money_commission_{listing_id}")])
    rows.append([InlineKeyboardButton("⬅️ К списку", callback_data="my_listings")])
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


def has_partner_access(context, user_id):
    partner_code = context.application.bot_data.get(f"partner_code_{user_id}")
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
        [InlineKeyboardButton(f"👤 {employee_display_name(key)}", callback_data=f"employee_{key}")]
        for key in EMPLOYEE_CHOICE_KEYS
    ])


async def ask_employee_choice(message, context, user_id, mode="start_partner"):
    context.application.bot_data[f"employee_choice_mode_{user_id}"] = mode
    set_state(context, user_id, "choosing_employee")

    if mode == "change_employee":
        text = (
            "Выберите сотрудника, чей контакт должен стоять в ваших следующих объявлениях.\n\n"
            "Это не меняет уже опубликованные объявления."
        )
    else:
        text = (
            "Партнёрский доступ активируется через сотрудника Binio.\n\n"
            "Выберите сотрудника, чей контакт должен быть указан в объявлениях."
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


def remove_contact_from_listing(text, contact_url):
    exact = make_contact_line(contact_url)
    text = text.replace(exact, "")
    text = re.sub(r'\n?\s*<b>Контакт:</b>\s*<a\s+href=["\'][^"\']+["\']>автор</a>\s*', '\n', text, flags=re.I)
    return text.strip()


def ensure_contact_line(text, contact_url):
    """Гарантирует одну каноническую строку контакта перед публикацией."""
    contact_line = make_contact_line(contact_url)
    if "[[CONTACT]]" in text:
        return text.replace("[[CONTACT]]", contact_line)
    if contact_line in text:
        return text

    if contact_url in text:
        cleaned = re.sub(
            r'\n?.*Контакт:.*' + re.escape(contact_url) + r'.*(?=\n|$)',
            '\n',
            text,
            flags=re.I,
        ).strip()
        if cleaned != text.strip():
            return cleaned.rstrip() + "\n\n" + contact_line
        return text

    text = re.sub(
        r'\n?\s*(?:<b>)?Контакт:(?:</b>)?.*(?=\n|$)',
        '\n',
        text,
        flags=re.I,
    ).strip()
    return text.rstrip() + "\n\n" + contact_line


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


def normalize_layout_hashtags(text):
    replacements = {
        r'#2(?:_|\+|-)?1\b': '#2plus1',
        r'#3(?:_|\+|-)?1\b': '#3plus1',
        r'#4(?:_|\+|-)?1\b': '#4plus1',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


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
    price = extract_financial_value(text, ["Аренда", "Цена"])
    deposit = extract_financial_value(text, ["Залог"])
    commission = extract_financial_value(text, ["Комиссия"])

    if price:
        parts.append(f"Цена: {price}")
    if deposit:
        parts.append(f"Залог: {deposit}")
    if commission:
        parts.append(f"Комиссия: {commission}")

    return "\n".join(parts)


def published_card_text(item):
    headline = listing_headline(item.get("listing", "Объявление")) or "Объявление"
    status = published_status_label(item.get("status"))
    financial_summary = listing_financial_summary(item.get("listing", ""))
    post_url = item.get("channel_post_url")

    text = f"{status}\n\n<b>{html.escape(headline)}</b>"
    if financial_summary:
        text += f"\n\n{html.escape(financial_summary)}"
    if post_url:
        text += f'\n\n<a href="{html.escape(post_url, quote=True)}">Открыть пост в канале</a>'
    return text


def listing_has_price(text):
    return bool(re.search(
        r'\d[\d\s.,]*(?:Kč|CZK|EUR|€|крон|korun)|'
        r'цена\s+по\s+(?:договор[её]нности|запросу)|'
        r'по\s+договор[её]нности|'
        r'уточняется|'
        r'info\s*(?:v|u)?\s*(?:rk|realit)',
        text,
        flags=re.I,
    ))


def headline_matches_property_type(headline, property_type_key):
    normalized = normalize_russian_headline(headline)
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

    if property_type_key not in PROPERTY_TYPES:
        issues.append("не выбран тип объекта")
    elif not headline_matches_property_type(headline, property_type_key):
        label = get_property_type(property_type_key)["button"]
        issues.append(f"заголовок не соответствует выбранному типу «{label}»: {headline or 'заголовок не найден'}")

    if make_contact_line(contact_url) not in listing and contact_url not in listing:
        issues.append("не найден контакт")
    if not listing_has_price(listing):
        issues.append("не найдена цена")

    return issues


def listing_fix_keyboard(listing_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Улучшить текст", callback_data=f"regen_{listing_id}")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}")],
    ])


def admin_fix_keyboard(listing_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Исправить", callback_data=f"edit_more_{listing_id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{listing_id}")],
    ])


def validation_message(issues):
    lines = "\n".join(f"— {issue}" for issue in issues)
    return (
        "Перед отправкой нужно поправить объявление:\n"
        f"{lines}\n\n"
        "Можно улучшить текст автоматически или исправить его вручную."
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


async def send_with_retry(coro_factory, retries=2, delay=2, label=""):
    """Пытается выполнить отправку в Telegram до `retries` раз — короткие сетевые
    сбои (Timed out и т.п.) не должны сразу проваливать всю операцию.
    coro_factory — функция без аргументов, возвращающая новую корутину на каждый вызов."""
    last_error = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            last_error = e
            logger.warning(f"{label} попытка {attempt + 1}/{retries} не удалась: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    raise last_error


async def shorten_listing_if_needed(text, limit=LISTING_SOFT_LIMIT):
    """Просит Gemini сжать объявление, если оно не помещается в подпись с фото."""
    if len(text) <= limit:
        return text

    if gemini_client is None:
        logger.warning("Gemini недоступен для сжатия объявления")
        return fit_to_caption(text)

    prompt = SHORTEN_TEMPLATE.format(limit=limit, text=text)
    import time

    last_error = None
    for attempt in range(2):
        attempt_start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model="gemini-2.5-flash",
                    contents=prompt,
                ),
                timeout=20,
            )
            shortened = response.text.strip()
            elapsed = time.monotonic() - attempt_start
            logger.info(
                f"Gemini сжатие попытка {attempt + 1}: успех за {elapsed:.1f}с, "
                f"{len(text)} → {len(shortened)} символов"
            )
            if len(shortened) <= TELEGRAM_CAPTION_LIMIT:
                return shortened
            return fit_to_caption(shortened)
        except Exception as e:
            elapsed = time.monotonic() - attempt_start
            logger.warning(f"Gemini сжатие попытка {attempt + 1}: ошибка за {elapsed:.1f}с — {e}")
            last_error = e
            if attempt == 0:
                await asyncio.sleep(1.5)

    logger.error(f"Не удалось сжать объявление через Gemini: {last_error}")
    return fit_to_caption(text)


async def prepare_listing_for_caption(text, contact_url):
    """Финальная подготовка объявления к подписи под фото.

    Здесь не меняется сценарий: мы только гарантируем контакт и размер подписи,
    чтобы Telegram принял фото вместе с текстом.
    """
    prepared = normalize_layout_hashtags(normalize_russian_headline(text))
    for _ in range(2):
        prepared = normalize_layout_hashtags(normalize_russian_headline(prepared))
        prepared = ensure_contact_line(prepared, contact_url)
        if len(prepared) <= LISTING_SOFT_LIMIT:
            return prepared
        prepared = await shorten_listing_if_needed(prepared)

    prepared = normalize_layout_hashtags(normalize_russian_headline(prepared))
    prepared = ensure_contact_line(prepared, contact_url)
    if len(prepared) <= TELEGRAM_CAPTION_LIMIT:
        return prepared

    contact_line = make_contact_line(contact_url)
    body = remove_contact_from_listing(prepared, contact_url)
    body_limit = max(100, TELEGRAM_CAPTION_LIMIT - len(contact_line) - 2)
    body = fit_to_caption(body, body_limit).rstrip()
    return f"{body}\n\n{contact_line}"


async def generate_formatted_listing(raw_text, property_type_key, contact_url, status_message=None):
    property_type = get_property_type(property_type_key)
    prompt = LISTING_TEMPLATE.format(
        text=raw_text,
        property_type_label=property_type["label"],
        property_type_rules=property_type["rules"],
    )

    import time

    formatted_listing = None
    last_error = None
    for attempt in range(3):
        attempt_start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model="gemini-2.5-flash",
                    contents=prompt,
                ),
                timeout=22,
            )
            formatted_listing = response.text.strip()
            elapsed = time.monotonic() - attempt_start
            logger.info(f"Gemini попытка {attempt + 1}: успех за {elapsed:.1f}с")
            break
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - attempt_start
            logger.warning(f"Gemini попытка {attempt + 1}: тайм-аут после {elapsed:.1f}с")
            last_error = TimeoutError("Gemini не ответил за 22 секунды")
            if attempt == 0 and status_message is not None:
                await status_message.reply_text("Обработка идёт дольше обычного, продолжаю ждать...")
            if attempt < 2:
                await asyncio.sleep(0.8)
                continue
        except Exception as e:
            elapsed = time.monotonic() - attempt_start
            logger.warning(f"Gemini попытка {attempt + 1}: ошибка за {elapsed:.1f}с — {e}")
            last_error = e
            if attempt < 2:
                await asyncio.sleep(0.8)
                continue

    if not formatted_listing:
        raise last_error or RuntimeError("Gemini не вернул текст")

    return await prepare_listing_for_caption(formatted_listing, contact_url)


async def send_text_with_fallback(
    bot,
    chat_id,
    text,
    reply_markup=None,
    disable_web_page_preview=True,
    label="text",
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
            )
        except Exception as e:
            logger.error(f"{label} HTML text error: {e}")

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
        return f"Предпросмотр объявления\n\n{headline}\n\nВыберите действие:"
    return "Предпросмотр объявления\n\nВыберите действие:"


async def send_listing_with_media(
    bot,
    chat_id,
    text,
    photos,
    reply_markup=None,
    caption_index=0,
    label="listing",
):
    """Отправляет объявление вместе с фото как подпись.

    Основной текст заранее сжимается через Gemini. Обрезка здесь — последняя
    страховка, чтобы Telegram не отклонил отправку.
    """
    photos = photos or []

    if not photos:
        return await send_text_with_fallback(
            bot,
            chat_id,
            text,
            reply_markup=reply_markup,
            label=f"{label} (только текст)",
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
            )

        safe_index = min(max(caption_index, 0), len(photos) - 1)
        media_group = build_media_group(photos, caption, caption_index=safe_index, parse_mode="HTML")
        sent_messages = await send_with_retry(
            lambda: bot.send_media_group(chat_id=chat_id, media=media_group),
            label=f"{label} (медиагруппа+подпись)",
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
            )
        return sent_messages[safe_index] if sent_messages else None
    except Exception as e:
        logger.error(f"{label} HTML media error: {e}")
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
            )

        safe_index = min(max(caption_index, 0), len(photos) - 1)
        media_group = build_media_group(photos, plain_caption, caption_index=safe_index, parse_mode=None)
        sent_messages = await send_with_retry(
            lambda: bot.send_media_group(chat_id=chat_id, media=media_group),
            label=f"{label} (plain медиагруппа+подпись)",
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
            )
        return sent_messages[safe_index] if sent_messages else None

    return None



async def send_role_choice(message):
    await message.reply_text(
        "Добро пожаловать в Binio.\n\n"
        "Выберите тип публикации:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Партнёр / риэлтор", callback_data="role_partner")],
            [InlineKeyboardButton("🏠 Собственник / разовое объявление", callback_data="role_public")],
        ])
    )


async def start_partner_flow(message, context, user, employee_key=None):
    user_id = user.id
    has_employee_link = employee_key and employee_key in EMPLOYEES

    if not has_employee_link and not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "choosing_role")
        await message.reply_text(
            "Партнёрская публикация доступна по персональной ссылке Binio.\n\n"
            f'Чтобы получить партнёрский доступ, напишите: <a href="{html.escape(DEFAULT_CONTACT, quote=True)}">администратору</a>.\n\n'
            "Если вы хотите разместить разовое объявление, выберите публикацию как собственник.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
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

    partner_code = context.application.bot_data.get(f"partner_code_{user_id}")

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
        f"Контакт в объявлениях: <a href=\"{contact_safe}\">{partner_name}</a>.\n\n"
        "Теперь можно создать объявление. Сначала отправьте фотографии объекта, а затем бот попросит выбрать тип недвижимости и прислать описание.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True


async def start_public_flow(message, context, user):
    user_id = user.id

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
        "Публикация для собственников и разовых объявлений.\n\n"
        "Стоимость размещения: "
        f"{PUBLIC_LISTING_PRICE_CZK} Kč.\n\n"
        "Сначала отправьте фотографии объекта. Затем бот попросит выбрать тип недвижимости и прислать описание.\n\n"
        "Перед оплатой вы увидите готовый предпросмотр и сможете исправить текст."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    context.application.bot_data.pop(f"employee_choice_mode_{user_id}", None)

    employee_key = context.args[0].strip().lower() if context.args else ""
    if employee_key in EMPLOYEES:
        await start_partner_flow(update.message, context, update.effective_user, employee_key)
        return

    set_state(context, user_id, "choosing_role")
    await send_role_choice(update.message)


async def public_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_public_flow(update.message, context, update.effective_user)


async def partner_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_partner_flow(update.message, context, update.effective_user)


async def employee_change_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) and not has_partner_access(context, user_id):
        set_state(context, user_id, "choosing_role")
        await update.message.reply_text(
            "Сменить сотрудника могут только партнёры, которые уже вошли по персональной ссылке.\n\n"
            f'Чтобы получить партнёрский доступ, напишите: <a href="{html.escape(DEFAULT_CONTACT, quote=True)}">администратору</a>.',
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
        await query.answer("Эта кнопка уже не активна. Напишите /start чтобы выбрать заново.", show_alert=True)
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
        await query.answer("Эта кнопка уже не активна. Используйте /employee или /partner.", show_alert=True)
        return

    await query.answer()

    employee_key = query.data.replace("employee_", "", 1)
    if employee_key not in EMPLOYEES:
        await query.message.reply_text("Не удалось выбрать сотрудника. Используйте /employee и попробуйте ещё раз.")
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
            "Если в старом сообщении визуально остался прежний контакт, при отправке на проверку бот всё равно использует новый."
            if current_preview_updated else ""
        )
        await query.message.reply_text(
            f"Сотрудник изменён: <a href=\"{contact_safe}\">{name_safe}</a>.\n\n"
            "В следующих объявлениях будет указан этот контакт."
            f"{extra_text}\n\n"
            "Чтобы создать новое объявление, используйте /partner.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    await start_partner_flow(query.message, context, user, employee_key)


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state == "choosing_role":
        await update.message.reply_text("Сначала выберите тип публикации кнопкой выше.")
        return

    if state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee.")
        return

    if state != "waiting_photos":
        await update.message.reply_text(
            "Пожалуйста, начните с команды /start."
        )
        return

    key = f"photos_{user_id}"
    if key not in context.application.bot_data:
        context.application.bot_data[key] = []

    MAX_PHOTOS = 10  # ограничение Telegram на кол-во фото в одной медиагруппе

    if len(context.application.bot_data[key]) >= MAX_PHOTOS:
        await update.message.reply_text(
            f"Уже загружено максимум: {MAX_PHOTOS} фото. "
            "Нажмите «Фото загружены», чтобы продолжить с уже загруженными."
        )
        return

    photo = update.message.photo[-1]
    context.application.bot_data[key].append(photo.file_id)

    count = len(context.application.bot_data[key])

    if count == 1:
        await update.message.reply_text(
            "Фото получено.\n\nДобавьте ещё фотографии или нажмите «Фото загружены».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")]
            ])
        )
    elif count == MAX_PHOTOS:
        await update.message.reply_text(
            f"Загружено {MAX_PHOTOS} фото — это максимум. "
            "Нажмите «Фото загружены», чтобы продолжить."
        )


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    # Если уже не в режиме ожидания фото — игнорируем старые кнопки.
    if state != "waiting_photos":
        await query.answer("Эта кнопка уже не активна. Напишите /start чтобы начать заново.", show_alert=True)
        return

    await query.answer()

    photos = context.application.bot_data.get(f"photos_{user_id}", [])
    if not photos:
        await query.message.reply_text(
            "Сначала загрузите хотя бы одно фото объекта."
        )
        return

    set_state(context, user_id, "waiting_type")
    await query.message.reply_text(
        "Фото приняты.\n\n"
        "Теперь выберите тип объекта:",
        reply_markup=property_type_keyboard()
    )


async def property_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    state = get_state(context, user_id)

    if state != "waiting_type":
        await query.answer("Эта кнопка уже не активна. Напишите /start чтобы начать заново.", show_alert=True)
        return

    type_key = query.data.replace("property_type_", "", 1)
    if type_key not in PROPERTY_TYPES:
        type_key = "other"
    property_type = get_property_type(type_key)
    context.application.bot_data[f"property_type_{user_id}"] = type_key
    set_state(context, user_id, "waiting_text")

    await query.answer()
    try:
        await query.edit_message_text(
            text=f"Выбрано: {property_type['button']}\n\n"
                 "Теперь отправьте описание объекта одним сообщением.\n\n"
                 "Чтобы объявление получилось точным, укажите:\n"
                 "— район или адрес\n"
                 "— метраж и планировку\n"
                 "— цену, коммунальные платежи, залог и комиссию\n"
                 "— дату заезда и важные условия"
        )
    except Exception:
        await query.message.reply_text(
            f"Выбрано: {property_type['button']}\n\n"
            "Теперь отправьте описание объекта одним сообщением.\n\n"
            "Чтобы объявление получилось точным, укажите:\n"
            "— район или адрес\n"
            "— метраж и планировку\n"
            "— цену, коммунальные платежи, залог и комиссию\n"
            "— дату заезда и важные условия"
        )


async def handle_wrong_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Партнёр прислал видео, документ или стикер вместо фото"""
    await update.message.reply_text(
        "Пожалуйста, отправляйте только фотографии объекта.\n"
        "Видео и документы не принимаются."
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
        await process_listing(update, context, update.message.text)
    elif state == "published_money_edit":
        await partner_apply_money_update(update, context, update.message.text)
    elif state == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается. Пожалуйста, подождите.")
    elif state == "submitted":
        await update.message.reply_text(
            "Объявление уже отправлено на проверку. Для нового объявления используйте /start."
        )
    elif state == "waiting_type":
        await update.message.reply_text(
            "Сначала выберите тип объекта кнопкой выше: квартира, комната, дом, участок, коммерция или другое."
        )
    elif state == "choosing_role":
        await update.message.reply_text("Сначала выберите тип публикации кнопкой выше.")
    elif state == "choosing_employee":
        await update.message.reply_text("Сначала выберите сотрудника кнопкой выше или используйте /employee.")
    elif state == "waiting_photos":
        await update.message.reply_text(
            "Сначала отправьте фотографии объекта и нажмите «Фото загружены»."
        )
    else:
        await update.message.reply_text(
            "Добро пожаловать в Binio.\n\nИспользуйте /start, чтобы начать."
        )


async def process_listing(update, context, text):
    """Обрабатывает текст через Gemini и показывает предпросмотр."""
    user_id = update.effective_user.id
    set_state(context, user_id, "processing")

    await update.message.reply_text("Обрабатываю объявление. Это может занять немного времени.")
    typing_task = asyncio.create_task(
        keep_chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING)
    )

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
        else:
            listing_id = new_listing_id()
            contact_url = context.application.bot_data.pop(
                f"session_contact_{user_id}",
                context.application.bot_data.get(f"contact_{user_id}", DEFAULT_CONTACT)
            )
            context.application.bot_data.pop(f"session_partner_code_{user_id}", None)
            photos = list(context.application.bot_data.get(f"photos_{user_id}", []))
            property_type_key = context.application.bot_data.get(f"property_type_{user_id}", "other")
            source = context.application.bot_data.get(f"flow_{user_id}", "partner")

        formatted_listing = await generate_formatted_listing(
            text,
            property_type_key,
            contact_url,
            status_message=update.message,
        )

        partner_label = format_partner_for_admin(update.effective_user)

        save_pending(context, listing_id, {
            'formatted_listing': formatted_listing,
            'photos': photos,
            'partner_id': user_id,
            'partner_label': partner_label,
            'contact_url': contact_url,
            'property_type': property_type_key,
            'source_text': text,
            'source': source,
            'paid': bool(existing_pending.get('paid')) if existing_pending else source == "partner",
            'submitted_to_admin': bool(existing_pending.get('submitted_to_admin')) if existing_pending else False,
        })

        context.application.bot_data[f"editing_listing_{user_id}"] = listing_id
        set_state(context, user_id, "done")
        if source == "public":
            await show_public_preview(update.message, context, formatted_listing, photos, listing_id)
        else:
            await show_partner_preview(update.message, context, formatted_listing, photos, listing_id)

    except Exception as e:
        logger.error(f"process_listing error: {e}")
        set_state(context, user_id, "waiting_text")
        await update.message.reply_text(
            "Сервис временно недоступен. Пожалуйста, отправьте текст ещё раз через несколько секунд."
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def show_partner_preview(message, context, listing, photos, listing_id):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить на проверку", callback_data=f"submit_{listing_id}")],
        [InlineKeyboardButton("🔁 Улучшить текст", callback_data=f"regen_{listing_id}")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}")]
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
    if PUBLIC_PAYMENT_TEST_MODE and not (pending and pending.get("paid")):
        rows.append([InlineKeyboardButton("🧪 Тест: пропустить оплату", callback_data=f"test_pay_public_{listing_id}")])
    rows.extend([
        [InlineKeyboardButton("🔁 Улучшить текст", callback_data=f"regen_{listing_id}")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data=f"partner_edit_{listing_id}")]
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
    if pending.get("paid"):
        await query.answer()
        await submit_paid_public_listing(context, listing_id, update.effective_user, query.message, test_mode=False)
        return
    if not PAYMENT_PROVIDER_TOKEN:
        await query.answer("Оплата ещё не подключена. Нужен provider token от BotFather.", show_alert=True)
        return

    formatted_listing = await prepare_listing_for_caption(
        pending['formatted_listing'],
        pending.get('contact_url', DEFAULT_CONTACT),
    )
    if formatted_listing != pending.get('formatted_listing'):
        pending['formatted_listing'] = formatted_listing
        save_pending(context, listing_id, pending)
    issues = validate_listing_ready(pending, formatted_listing)
    if issues:
        await query.answer()
        await query.message.reply_text(
            validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    await query.answer()
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title="Публикация объявления",
        description="Платное размещение объявления в канале Binio.",
        payload=public_payment_payload(listing_id),
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency=PUBLIC_PAYMENT_CURRENCY,
        prices=[LabeledPrice(label="Публикация объявления", amount=PUBLIC_PAYMENT_AMOUNT)],
        start_parameter=f"public-{listing_id}",
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
        await query.answer("Подождите, объявление ещё обрабатывается.", show_alert=True)
        return

    if pending.get('submitted_to_admin'):
        await query.answer("Объявление уже отправлено на проверку. Изменения закрыты.", show_alert=True)
        return

    source_text = pending.get('source_text')
    if not source_text:
        await query.answer()
        await query.message.reply_text(
            "Для этой старой заявки не сохранился исходный текст. "
            "Нажмите «Изменить текст» и отправьте описание заново.",
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    await query.answer()
    set_state(context, user_id, "processing")
    await query.message.reply_text("Готовлю более аккуратный вариант текста...")

    try:
        contact_url = pending.get('contact_url', context.application.bot_data.get(f"contact_{user_id}", DEFAULT_CONTACT))
        property_type_key = pending.get('property_type', context.application.bot_data.get(f"property_type_{user_id}", "other"))
        formatted_listing = await generate_formatted_listing(
            source_text,
            property_type_key,
            contact_url,
            status_message=query.message,
        )

        pending['formatted_listing'] = formatted_listing
        pending['contact_url'] = contact_url
        pending['property_type'] = property_type_key
        save_pending(context, listing_id, pending)
        set_state(context, user_id, "done")

        await query.message.reply_text("Готово. Проверьте новый вариант:")
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
        set_state(context, user_id, "done")
        await query.message.reply_text(
            "Не получилось улучшить текст автоматически. Попробуйте ещё раз или исправьте его вручную.",
            reply_markup=listing_fix_keyboard(listing_id),
        )


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
        await query.answer("Подождите, объявление ещё обрабатывается.", show_alert=True)
        return

    if pending.get('submitted_to_admin'):
        await query.answer("Объявление уже отправлено на проверку. Изменения закрыты.", show_alert=True)
        return

    await query.answer()
    context.application.bot_data[f"editing_listing_{update.effective_user.id}"] = listing_id
    set_state(context, update.effective_user.id, "partner_editing")

    try:
        await query.edit_message_text(text="Отправьте исправленный текст объявления одним сообщением.")
    except Exception:
        try:
            await query.edit_message_caption(caption="Отправьте исправленный текст объявления одним сообщением.")
        except Exception:
            await query.message.reply_text("Отправьте исправленный текст объявления одним сообщением.")

    plain_text = pending['formatted_listing']
    contact_url_saved = pending.get('contact_url', DEFAULT_CONTACT)
    plain_text = remove_contact_from_listing(plain_text, contact_url_saved)
    plain_text = strip_html_tags_keep_text(plain_text)
    await send_plain_text_chunks(
        context.bot,
        query.message.chat_id,
        f"📄 Текущий текст для редактирования:\n\n{plain_text}",
        label="partner_edit current_text",
    )


async def send_pending_to_admin(context, listing_id, pending, submitter_label, label="submit_to_admin"):
    formatted_listing = await prepare_listing_for_caption(
        pending['formatted_listing'],
        pending.get('contact_url', DEFAULT_CONTACT),
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
    await send_text_with_fallback(
        context.bot,
        ADMIN_CHAT_ID,
        f"📋 Новое объявление от {submitter_label}",
        label=f"{label} submitter_info",
    )
    await send_listing_with_media(
        context.bot,
        ADMIN_CHAT_ID,
        formatted_listing,
        pending['photos'],
        reply_markup=admin_keyboard,
        caption_index=len(pending['photos']) - 1,
        label=label,
    )
    return []


async def partner_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.split("_", 1)[1]
    pending = get_pending(context, listing_id)

    if not pending:
        await query.answer()
        await query.message.reply_text("Не удалось найти это объявление. Пожалуйста, начните заново через /start.")
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

    await query.answer()

    partner_label = pending.get('partner_label') or format_partner_for_admin(update.effective_user)
    try:
        issues = await send_pending_to_admin(context, listing_id, pending, partner_label, label="partner_submit")
    except Exception as e:
        logger.error(f"partner_submit send error: {e}")
        await query.message.reply_text(f"Не получилось отправить объявление на проверку: {e}")
        return
    if issues:
        await query.message.reply_text(
            validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    pending['submitted_to_admin'] = True
    save_pending(context, listing_id, pending)
    set_state(context, update.effective_user.id, "submitted")

    try:
        await query.edit_message_text(
            text="✅ Объявление отправлено на проверку!\nДля нового объявления напишите /start"
        )
    except Exception:
        try:
            await query.edit_message_caption(
                caption="✅ Объявление отправлено на проверку!\nДля нового объявления напишите /start"
            )
        except Exception:
            await query.message.reply_text(
                "✅ Объявление отправлено на проверку!\nДля нового объявления напишите /start"
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
        or query.currency != PUBLIC_PAYMENT_CURRENCY
        or query.total_amount != PUBLIC_PAYMENT_AMOUNT
    ):
        await query.answer(ok=False, error_message="Счёт устарел. Создайте публикацию заново через /owner.")
        return

    await query.answer(ok=True)


async def submit_paid_public_listing(context, listing_id, user, reply_message, test_mode=False):
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("source") != "public":
        logger.warning("submit_paid_public_listing: pending public listing not found")
        return False
    if pending.get("partner_id") != user.id:
        logger.warning("submit_paid_public_listing: user_id mismatch")
        return False
    if pending.get("submitted_to_admin"):
        await reply_message.reply_text("✅ Объявление уже отправлено на проверку.")
        return True

    pending["paid"] = True
    if test_mode:
        pending["payment_test_mode"] = True
        pending["payment_total_amount"] = 0
        pending["payment_currency"] = PUBLIC_PAYMENT_CURRENCY
    save_pending(context, listing_id, pending)

    public_label = "платного пользователя " + (pending.get("partner_label") or format_partner_for_admin(user))
    if test_mode:
        public_label = "тестовое платное объявление от " + (pending.get("partner_label") or format_partner_for_admin(user))

    try:
        issues = await send_pending_to_admin(context, listing_id, pending, public_label, label="public_paid_submit")
    except Exception as e:
        logger.error(f"submit_paid_public_listing error: {e}")
        await reply_message.reply_text(
            "✅ Оплата получена, но не получилось отправить объявление на проверку. Я сообщу администратору."
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Оплата получена, но объявление не отправилось на проверку: {e}"
            )
        except Exception:
            pass
        return False

    if issues:
        await reply_message.reply_text(
            "✅ Оплата получена, но объявление нужно поправить перед проверкой.\n\n"
            + validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return False

    pending["submitted_to_admin"] = True
    save_pending(context, listing_id, pending)
    set_state(context, user.id, "submitted")
    await reply_message.reply_text("✅ Оплата получена. Объявление отправлено на проверку.")
    return True


async def test_public_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    listing_id = query.data.replace("test_pay_public_", "", 1)

    if not PUBLIC_PAYMENT_TEST_MODE:
        await query.answer("Тестовый режим оплаты выключен.", show_alert=True)
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

    await query.answer()
    await submit_paid_public_listing(context, listing_id, update.effective_user, query.message, test_mode=False)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    listing_id = listing_id_from_payment_payload(payment.invoice_payload)
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("source") != "public":
        logger.warning("successful_payment: pending public listing not found")
        return
    if pending.get("partner_id") != update.effective_user.id:
        logger.warning("successful_payment: user_id mismatch")
        return
    if pending.get("submitted_to_admin"):
        await update.message.reply_text("✅ Оплата уже получена, объявление уже отправлено на проверку.")
        return

    if payment.currency != PUBLIC_PAYMENT_CURRENCY or payment.total_amount != PUBLIC_PAYMENT_AMOUNT:
        logger.error(
            f"successful_payment amount mismatch: {payment.currency} {payment.total_amount}, "
            f"expected {PUBLIC_PAYMENT_CURRENCY} {PUBLIC_PAYMENT_AMOUNT}"
        )
        return

    pending["paid"] = True
    pending["payment_total_amount"] = payment.total_amount
    pending["payment_currency"] = payment.currency
    pending["telegram_payment_charge_id"] = payment.telegram_payment_charge_id
    pending["provider_payment_charge_id"] = payment.provider_payment_charge_id
    save_pending(context, listing_id, pending)

    await submit_paid_public_listing(context, listing_id, update.effective_user, update.message, test_mode=False)


def clear_pending_for_user(context, user_id):
    removed = 0
    for listing_id, pending in list(list_unique_pending_items(context).items()):
        if pending.get("partner_id") == user_id:
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
    return removed


async def admin_clear_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    removed = clear_pending_for_user(context, user.id)
    await update.message.reply_text(
        "Очистка завершена.\n\n"
        f"Удалено ваших незавершённых заявок и тестовых предпросмотров: {removed}.\n\n"
        "Опубликованные объявления и заявки других пользователей не тронуты."
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

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
    published_active = sum(1 for item in published_items if item.get("status", "active") == "active")
    published_rented = sum(1 for item in published_items if item.get("status") == "rented")
    published_removed = sum(1 for item in published_items if item.get("status") == "removed")
    published_partner_ids = {
        item.get("partner_id")
        for item in published_items
        if item.get("partner_id") is not None
    }

    partner_ids = set()
    for key, value in context.application.bot_data.items():
        if key.startswith("partner_code_") and value in EMPLOYEES:
            partner_ids.add(key.replace("partner_code_", "", 1))
        elif key.startswith("contact_") and is_partner_contact_url(value):
            partner_ids.add(key.replace("contact_", "", 1))

    payment_status = "подключена" if PAYMENT_PROVIDER_TOKEN else "не подключена"
    test_mode_status = "включён" if PUBLIC_PAYMENT_TEST_MODE else "выключен"
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
        "<b>Опубликованные объявления партнёров:</b>\n"
        f"— всего объявлений: {published_total}\n"
        f"— активные: {published_active}\n"
        f"— сдано: {published_rented}\n"
        f"— снято: {published_removed}\n"
        f"— авторов объявлений: {len(published_partner_ids)}\n\n"
        "<b>Партнёры:</b>\n"
        f"— с привязкой к сотруднику: {len(partner_ids)}\n\n"
        "<b>Оплата:</b>\n"
        f"— Telegram-оплата: {payment_status}\n"
        f"— тестовый режим: {test_mode_status}\n\n"
        f"Обновлено: {updated_at}\n\n"
        "Если после тестов остались лишние черновики, используйте /clearpending."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def partner_my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    listings = list_partner_published(context, user_id)
    target = update.callback_query.message if update.callback_query else update.message

    if update.callback_query:
        await update.callback_query.answer()

    if not listings:
        text = (
            "У вас пока нет опубликованных объявлений.\n\n"
            "Когда объявление пройдёт проверку и появится в канале, оно будет доступно здесь."
        )
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text)
                return
            except Exception:
                pass
        await target.reply_text(text)
        return

    text = (
        "Мои объявления\n\n"
        "Выберите объявление, чтобы открыть управление.\n\n"
        "Внутри можно отметить объект как сданный или изменить цену, залог и комиссию."
    )
    keyboard = published_list_keyboard(listings)
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
        await update.message.reply_text("Объявление не найдено. Откройте /mylistings и попробуйте ещё раз.")
        return

    new_value = normalize_financial_value(new_value)
    if len(new_value) < 2:
        await update.message.reply_text(f"Напишите новое значение для поля «{field['label']}». Например: 20 000 Kč")
        return

    base_listing = replace_financial_line(item.get("listing", ""), field_key, new_value)
    visible_listing = listing_with_status(base_listing, item.get("status", "active"))

    try:
        await edit_published_channel_posts(context, item, visible_listing)
    except Exception as e:
        logger.error(f"partner_apply_money_update error: {e}")
        await update.message.reply_text("Не получилось обновить пост в канале. Попробуйте позже.")
        return

    item["listing"] = base_listing
    item["visible_listing"] = visible_listing
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_published(context, listing_id, item)

    context.application.bot_data.pop(f"published_money_listing_{user_id}", None)
    context.application.bot_data.pop(f"published_money_field_{user_id}", None)
    set_state(context, user_id, "done")
    await update.message.reply_text("Готово. Значение обновлено в объявлении.")


async def edit_channel_listing_status(context, item, status):
    visible_listing = listing_with_status(item.get("listing", ""), status)
    await edit_published_channel_posts(context, item, visible_listing)

    item["status"] = status
    item["visible_listing"] = visible_listing
    item["status_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_published(context, item["listing_id"], item)


async def partner_published_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data

    if data == "my_listings":
        await partner_my_listings(update, context)
        return

    if data.startswith("pub_view_"):
        listing_id = data.replace("pub_view_", "", 1)
        item = get_published(context, listing_id)
        if not item or item.get("partner_id") != user_id:
            await query.answer("Объявление не найдено.", show_alert=True)
            return

        await query.answer()
        text = published_card_text(item)
        try:
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(item),
            )
        except Exception:
            await query.message.reply_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(item),
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

        if item.get("status") == status:
            await query.answer("Статус уже такой.", show_alert=True)
            return

        await query.answer("Обновляю пост в канале...")
        try:
            await edit_channel_listing_status(context, item, status)
        except Exception as e:
            logger.error(f"partner_published_callback status update error: {e}")
            await query.message.reply_text(
                "⚠️ Не получилось изменить пост в канале. Возможно, Telegram не дал отредактировать старый пост."
            )
            return

        updated = get_published(context, listing_id)
        text = published_card_text(updated)
        try:
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(updated),
            )
        except Exception:
            await query.message.reply_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=published_manage_keyboard(updated),
            )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("У вас нет прав для этого действия.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        listing_id = data.split("_", 1)[1]
        pending = get_pending(context, listing_id)
        if not pending:
            await query.message.reply_text("⚠️ Объявление не найдено.")
            return

        listing = await prepare_listing_for_caption(
            pending['formatted_listing'],
            pending.get('contact_url', DEFAULT_CONTACT),
        )
        if listing != pending.get('formatted_listing'):
            pending['formatted_listing'] = listing
            save_pending(context, listing_id, pending)
        issues = validate_listing_ready(pending, listing)
        if issues:
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
            )
        except Exception as e:
            logger.error(f"approve publish send error: {e}")
            await query.message.reply_text(f"⚠️ Ошибка публикации: {e}")
            return

        channel_message_id = getattr(published_message, "message_id", None)
        channel_chat_id = getattr(getattr(published_message, "chat", None), "id", CHANNEL_USERNAME)
        partner_id = pending.get('partner_id')
        is_public_paid = pending.get("source") == "public"
        post_url = channel_post_url(CHANNEL_USERNAME, channel_message_id) if channel_message_id else None
        if channel_message_id and not is_public_paid:
            save_published(context, listing_id, {
                "listing_id": listing_id,
                "partner_id": partner_id,
                "partner_label": pending.get("partner_label"),
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
            })
        elif not channel_message_id:
            logger.warning(f"Опубликовано, но не удалось сохранить message_id для listing_id={listing_id}")

        try:
            await query.edit_message_text(text=f"✅ Опубликовано в {CHANNEL_USERNAME}")
        except Exception:
            await query.edit_message_caption(caption=f"✅ Опубликовано в {CHANNEL_USERNAME}")

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
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить платного пользователя {partner_id} о публикации: {e}")

        delete_pending(context, listing_id)
        if context.application.bot_data.get("admin_editing_listing_id") == listing_id:
            context.application.bot_data.pop("admin_editing_listing_id", None)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)

    elif data.startswith("edit_more_"):
        listing_id = data.split("_", 2)[2]
        pending = get_pending(context, listing_id)
        if pending:
            context.application.bot_data["admin_editing_listing_id"] = listing_id
            current_text = pending['formatted_listing']
            try:
                await query.edit_message_text(text="Отправьте исправленный текст объявления одним сообщением.")
            except Exception:
                await query.edit_message_caption(caption="Отправьте исправленный текст объявления одним сообщением.")
            await send_text_with_fallback(
                context.bot,
                ADMIN_CHAT_ID,
                current_text,
                label="admin edit_more current_text",
            )
        else:
            await query.message.reply_text("⚠️ Объявление не найдено.")

    elif data.startswith("reject_"):
        listing_id = data.split("_", 1)[1]
        pending = get_pending(context, listing_id)
        if not pending:
            await query.message.reply_text("⚠️ Объявление не найдено.")
            return
        try:
            await query.edit_message_text(text="❌ Объявление отклонено.")
        except Exception:
            await query.edit_message_caption(caption="❌ Объявление отклонено.")
        partner_id = pending.get('partner_id')
        if partner_id is not None:
            try:
                if pending.get("source") == "public" and pending.get("paid"):
                    reject_text = (
                        "❌ Ваше объявление отклонено администратором.\n\n"
                        "Если нужна помощь по оплате или публикации, свяжитесь с администратором."
                    )
                else:
                    reject_text = "❌ Ваше объявление отклонено администратором."
                await context.bot.send_message(chat_id=partner_id, text=reject_text)
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {partner_id} об отклонении: {e}")
        delete_pending(context, listing_id)
        if context.application.bot_data.get("admin_editing_listing_id") == listing_id:
            context.application.bot_data.pop("admin_editing_listing_id", None)
        if partner_id is not None and context.application.bot_data.get(f"editing_listing_{partner_id}") == listing_id:
            context.application.bot_data.pop(f"editing_listing_{partner_id}", None)


async def admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только админ может редактировать текст из чата одобрения."""
    if update.effective_user is not None and not is_admin(update.effective_user.id):
        logger.warning(
            f"admin_edit: сообщение в чате одобрения от user_id={update.effective_user.id} "
            f"проигнорировано — не совпадает с ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}"
        )
        return

    message = update.effective_message
    if not message or not message.text:
        return
    edited_text = message.text

    listing_id = context.application.bot_data.get("admin_editing_listing_id")
    pending = get_pending(context, listing_id) if listing_id else None

    # Если явно не отмечено, какое объявление редактируем, берём единственное ожидающее.
    if not pending:
        keys = list_pending_keys(context)
        if len(keys) == 1:
            listing_id = listing_id_from_pending_key(keys[0])
            pending = get_pending(context, listing_id)
        else:
            listing_id = None

    if not listing_id or not pending:
        await message.reply_text(
            "⚠️ Не понятно, какое объявление вы редактируете. "
            "Нажмите «✏️ Исправить» под нужным объявлением и повторите."
        )
        return

    edited_text = await prepare_listing_for_caption(
        edited_text,
        pending.get('contact_url', DEFAULT_CONTACT),
    )
    pending['formatted_listing'] = edited_text
    save_pending(context, listing_id, pending)
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
        "⚠️ Не получилось распознать это как текст для правки. "
        "Пожалуйста, отправьте исправленный текст обычным текстовым сообщением "
        "(без прикреплённых фото или файлов)."
    )


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит любые необработанные ошибки, чтобы бот не 'зависал' молча."""
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
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла техническая ошибка. Попробуйте ещё раз через /start."
            )
    except Exception:
        pass


def main():
    validate_config()
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    logger.info(f"Файл памяти бота: {BOT_DATA_PATH}")
    persistence = PicklePersistence(filepath=BOT_DATA_PATH)
    app = (
        Application.builder()
        .token(PARTNER_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .concurrent_updates(False)
        .persistence(persistence)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("partner", partner_publish_start))
    app.add_handler(CommandHandler("owner", public_publish_start))
    app.add_handler(CommandHandler("publish", public_publish_start))
    app.add_handler(CommandHandler("mylistings", partner_my_listings))
    app.add_handler(CommandHandler("employee", employee_change_start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("clearpending", admin_clear_pending))

    # Фото только от партнёров (не из чата одобрения)
    app.add_handler(MessageHandler(
        filters.PHOTO & ~filters.Chat(ADMIN_CHAT_ID),
        receive_photo
    ))

    # Защита от видео, документов, стикеров
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.Document.ALL | filters.Sticker.ALL) & ~filters.Chat(ADMIN_CHAT_ID),
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
        pattern=r"^(my_listings|pub_view_[A-Za-z0-9_-]+|pub_rented_[A-Za-z0-9_-]+|pub_active_[A-Za-z0-9_-]+|pub_money_(price|deposit|commission)_[A-Za-z0-9_-]+)$"
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
    app.add_handler(CallbackQueryHandler(partner_submit, pattern=r"^submit_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_regenerate, pattern=r"^regen_[A-Za-z0-9_-]+$"))
    app.add_handler(CallbackQueryHandler(partner_edit_request, pattern=r"^partner_edit_[A-Za-z0-9_-]+$"))

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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Python 3.14 больше не создаёт event loop автоматически для MainThread.
    # python-telegram-bot пока ожидает, что он уже есть перед run_polling().
    if platform.system().lower() == "windows":
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()

