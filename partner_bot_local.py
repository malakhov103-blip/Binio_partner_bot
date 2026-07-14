import asyncio
import html
import logging
import os
import pickle
import platform
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LabeledPrice, BotCommand
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, PicklePersistence, PreCheckoutQueryHandler
)

try:
    import stripe
except Exception:
    stripe = None

try:
    from aiohttp import web
except Exception:
    web = None

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
PAYMENT_PROVIDER_TOKEN = clean_env("PAYMENT_PROVIDER_TOKEN")
STRIPE_SECRET_KEY = clean_env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = clean_env("STRIPE_WEBHOOK_SECRET")
PUBLIC_BASE_URL = clean_env("PUBLIC_BASE_URL").rstrip("/")
BOT_USERNAME = clean_env("BOT_USERNAME", "binio_partner_bot").lstrip("@")
PUBLIC_LISTING_PRICE_CZK = int(os.getenv("PUBLIC_LISTING_PRICE_CZK", "20"))
PUBLIC_PAYMENT_CURRENCY = "CZK"
PUBLIC_PAYMENT_AMOUNT = PUBLIC_LISTING_PRICE_CZK * 100
PUBLIC_MONTHLY_LIMIT = int(os.getenv("PUBLIC_MONTHLY_LIMIT", "3"))
PUBLIC_INVOICE_TTL_HOURS = int(os.getenv("PUBLIC_INVOICE_TTL_HOURS", "2"))
PUBLIC_PAYMENT_TEST_MODE = os.getenv("PUBLIC_PAYMENT_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
BOT_DATA_DIR = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "partner_bot_data.pickle")
BOT_DATA_PATH = os.path.join(BOT_DATA_DIR, BOT_DATA_FILE)
BOT_DRAFT_TTL_DAYS = int(os.getenv("BOT_DRAFT_TTL_DAYS", "14"))
BOT_SUBMITTED_TTL_DAYS = int(os.getenv("BOT_SUBMITTED_TTL_DAYS", "90"))
BOT_TRANSIENT_TTL_DAYS = int(os.getenv("BOT_TRANSIENT_TTL_DAYS", "7"))

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
GEMINI_CONCURRENT_LIMIT = max(1, int(os.getenv("GEMINI_CONCURRENT_LIMIT", "3")))
GEMINI_SEMAPHORE = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
STRIPE_ENABLED = bool(stripe and web and STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and PUBLIC_BASE_URL)
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
Ты редактор объявлений Binio для русскоязычных клиентов в Праге. Перепиши сырой текст в красивое, грамотное и продающее объявление об аренде.

Тип объекта выбран партнёром:
{property_type_rules}
Это важнее исходного текста: комната не должна выглядеть как квартира, дом — как квартира, коммерция — как жильё.

Стиль всегда как в этом образце, но данные не копируй:
<b>Аренда 1+кк, 27 м² — Прага 5, Мотол</b>

Предлагается светлая студия площадью 27 м² в спокойном районе Праги 5. Жильё расположено на 2-м этаже дома без лифта, недавно отремонтировано, частично меблировано и готово к заселению.

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
- Пиши на русском; адреса, улицы, районы, станции и остановки оставляй как в исходнике.
- Не придумывай конкретные факты: цену, район, улицу, метраж, этаж, планировку, залог, комиссию, коммунальные платежи, транспорт, сроки заезда.
- Формулировки делай лучше исходных: плавно, понятно, уважительно, без буквального перевода.
- Описание объекта до транспорта обычно 30-40 слов; если фактов мало, всё равно пиши красиво, но без выдуманных параметров.
- Важное не выбрасывай: дата заезда, для кого подходит, животные, мебель, если это есть в исходнике.
- Технику и удобства не перечисляй длинно; выбери главное и впиши естественно.
- Не используй фразы: «квартира встречает», «локация предлагает», «пространство порадует», «данное помещение», «внутри найдёте».
- Общий текст с контактом и хештегами — до 930 символов.

Формат:
<b>[Заголовок]</b>

[Описание объекта]

<b>Локация:</b>
— [транспорт]: около X минут пешком
— Рядом: [важная инфраструктура, если есть]

<b>Финансовые условия:</b>
— Арендная плата: X Kč
— Коммунальные платежи: X Kč
— Залог: X Kč
— Комиссия агентства: X Kč

[[CONTACT]]

#[тип] #[район] #pronajem

Заголовок: жирный через <b>. Разделы тоже жирные. Без эмодзи и звёздочек.
Хештеги: максимум 3. Второй хештег всегда район Праги в формате #Praha1...#Praha10, если он указан в тексте; не используй микрорайоны вроде #holesovice, #smichov, #vinohrady. 2kk/2кк/2+kk/2+кк = #2kk; 3kk/3кк/3+kk/3+кк = #3kk; 4kk/4кк/4+kk/4+кк = #4kk. #2plus1/#3plus1/#4plus1 только для явных 2+1/3+1/4+1. Комната #pokoj, дом #dum, участок #pozemek, коммерция/нежилое #komerce.
Контакт: вставь ровно [[CONTACT]] отдельной строкой.

ТЕКСТ ОТ ПАРТНЁРА:
{text}

Верни только готовое объявление, без пояснений.
"""

SHORTEN_TEMPLATE = """
Сожми объявление до {limit} символов.
Сохрани: HTML <b>, тип объекта, цену, коммунальные платежи, залог, комиссию, дату заезда, мебель, животных, контакт и хештеги.
Не придумывай данные. Стиль оставь плавным и продающим, не сухим списком.

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
    removed = cleanup_bot_memory(app.bot_data)
    if sum(removed.values()):
        logger.info(f"Очистка памяти при запуске: {cleanup_summary_text(removed)}")

    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Выбрать тип публикации"),
            BotCommand("partner", "Публикация для партнёра / риэлтора"),
            BotCommand("owner", "Разместить объявление как собственник"),
            BotCommand("mylistings", "Мои объявления"),
            BotCommand("employee", "Сменить сотрудника для контакта"),
            BotCommand("terms", "Условия оплаты и публикации"),
            BotCommand("support", "Поддержка по оплате и публикации"),
            BotCommand("stats", "Статистика для администратора"),
            BotCommand("memory", "Память и очистка"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось обновить меню команд Telegram: {e}")

    await start_stripe_webhook_server(app)


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
    seen_listing_ids = set()
    total = 0

    for listing_id, pending in list_unique_pending_items(context).items():
        if exclude_listing_id and listing_id == exclude_listing_id:
            continue
        if pending.get("partner_id") != user_id or pending.get("source") != "public":
            continue
        if not (pending.get("paid") or pending.get("submitted_to_admin") or public_invoice_active(pending)):
            continue
        item_month = month_key(
            pending.get("payment_paid_at")
            or pending.get("submitted_at")
            or pending.get("invoice_created_at")
            or pending.get("updated_at")
            or pending.get("created_at")
        )
        if item_month == target_month:
            seen_listing_ids.add(listing_id)
            total += 1

    for key, item in context.application.bot_data.items():
        if not key.startswith("published_listing_") or not isinstance(item, dict):
            continue
        listing_id = item.get("listing_id") or key.replace("published_listing_", "", 1)
        if exclude_listing_id and listing_id == exclude_listing_id:
            continue
        if listing_id in seen_listing_ids:
            continue
        if item.get("partner_id") != user_id or item.get("source") != "public":
            continue
        item_month = month_key(
            item.get("payment_paid_at")
            or item.get("submitted_at")
            or item.get("published_at")
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


def bot_start_url(start_param="start"):
    return f"https://t.me/{BOT_USERNAME}?start={start_param}"


def stripe_success_url():
    return f"{PUBLIC_BASE_URL}/stripe-success"


def stripe_cancel_url(listing_id):
    return f"{PUBLIC_BASE_URL}/stripe-cancel?listing_id={quote(str(listing_id), safe='')}"


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


async def create_stripe_checkout_session(listing_id, pending):
    if not STRIPE_ENABLED:
        raise RuntimeError("Stripe не подключён")

    expires_at = int(time.time() + PUBLIC_INVOICE_TTL_HOURS * 60 * 60)
    return await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": PUBLIC_PAYMENT_CURRENCY.lower(),
                "product_data": {
                    "name": "Публикация объявления Binio",
                },
                "unit_amount": PUBLIC_PAYMENT_AMOUNT,
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
        },
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

    await query.message.reply_text(
        "Счёт создан\n\n"
        "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты Stripe. "
        "После успешной оплаты бот автоматически отправит объявление на проверку",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💳 Оплатить {PUBLIC_LISTING_PRICE_CZK} Kč", url=checkout_url)
        ]])
    )


async def handle_stripe_checkout_completed(app, session):
    metadata = session.get("metadata") or {}
    listing_id = metadata.get("listing_id") or session.get("client_reference_id")
    user_id_raw = metadata.get("user_id")

    if not listing_id or not user_id_raw:
        logger.warning("Stripe webhook: нет listing_id/user_id в metadata")
        return

    try:
        user_id = int(user_id_raw)
    except Exception:
        logger.warning(f"Stripe webhook: неправильный user_id={user_id_raw}")
        return

    pending = get_pending_from_app(app, listing_id)
    if not pending or pending.get("source") != "public":
        logger.warning(f"Stripe webhook: заявка не найдена listing_id={listing_id}")
        return
    if pending.get("partner_id") != user_id:
        logger.warning(f"Stripe webhook: user_id mismatch listing_id={listing_id}")
        return
    if pending.get("submitted_to_admin"):
        logger.info(f"Stripe webhook: заявка уже отправлена listing_id={listing_id}")
        return

    if session.get("payment_status") != "paid":
        logger.info(f"Stripe webhook: payment_status={session.get('payment_status')}")
        return

    amount_total = session.get("amount_total")
    currency = str(session.get("currency", "")).upper()
    if amount_total != PUBLIC_PAYMENT_AMOUNT or currency != PUBLIC_PAYMENT_CURRENCY:
        logger.error(
            f"Stripe webhook amount mismatch: {currency} {amount_total}, "
            f"expected {PUBLIC_PAYMENT_CURRENCY} {PUBLIC_PAYMENT_AMOUNT}"
        )
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Stripe оплата не совпала с ожидаемой суммой для listing_id={listing_id}"
        )
        return

    if not public_invoice_active(pending):
        logger.warning(f"Stripe webhook: счёт устарел listing_id={listing_id}")
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Stripe прислал оплату по устаревшему счёту listing_id={listing_id}"
        )
        return

    if public_limit_reached_for_app(app, user_id, exclude_listing_id=listing_id):
        logger.warning(f"Stripe webhook: лимит превышен listing_id={listing_id}")
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Stripe оплата пришла сверх месячного лимита listing_id={listing_id}"
        )
        return

    pending["paid"] = True
    pending.setdefault("payment_paid_at", now_iso())
    pending["payment_provider"] = "stripe"
    pending["payment_total_amount"] = amount_total
    pending["payment_currency"] = currency
    pending["stripe_session_id"] = session.get("id")
    pending["stripe_payment_intent"] = session.get("payment_intent")
    save_pending_to_app(app, listing_id, pending)

    reply_target = BotReplyTarget(app.bot, user_id)
    await submit_paid_public_listing(
        context_from_app(app),
        listing_id,
        PaidUser(user_id),
        reply_target,
        test_mode=False,
    )


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

    if event.get("type") == "checkout.session.completed":
        await handle_stripe_checkout_completed(request.app["telegram_app"], event["data"]["object"])

    return web.Response(text="ok")


async def stripe_success_page(request):
    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Оплата Binio</title></head>"
            "<body style='font-family:Arial,sans-serif;padding:32px;line-height:1.45'>"
            "<h2>Оплата прошла</h2>"
            "<p>Вернитесь в Telegram. Бот автоматически отправит объявление на проверку.</p>"
            f"<p><a href='{bot_start_url()}' style='font-size:18px'>Открыть бота</a></p>"
            "</body></html>"
        )
    )


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
            f"<p><a href='{bot_start_url()}' style='font-size:18px'>Открыть бота</a></p>"
            "</body></html>"
        )
    )


async def start_stripe_webhook_server(app):
    if not STRIPE_ENABLED:
        if STRIPE_SECRET_KEY or STRIPE_WEBHOOK_SECRET or PUBLIC_BASE_URL:
            logger.warning(
                "Stripe настроен не полностью. Нужны STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PUBLIC_BASE_URL."
            )
        return

    port = int(os.getenv("PORT", "8080"))
    web_app = web.Application()
    web_app["telegram_app"] = app
    web_app.router.add_post("/stripe-webhook", stripe_webhook)
    web_app.router.add_get("/stripe-success", stripe_success_page)
    web_app.router.add_get("/stripe-cancel", stripe_cancel_page)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    app.bot_data["stripe_web_runner"] = runner
    logger.info(f"Stripe webhook server запущен на порту {port}")


async def stop_stripe_webhook_server(app):
    runner = app.bot_data.get("stripe_web_runner")
    if runner:
        await runner.cleanup()


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
        if pending.get("submitted_to_admin"):
            if is_older_than(last_seen, submitted_ttl):
                bot_data.pop(pending_key(listing_id), None)
                bot_data.pop(f"pending_{listing_id}", None)
                removed["submitted"] += 1
                deleted = True
        elif pending.get("paid"):
            # Оплаченные, но не отправленные из-за правок/ошибок, держим как важные заявки.
            if is_older_than(last_seen, submitted_ttl):
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
        "session_contact_",
        "session_partner_code_",
        "employee_choice_mode_",
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
        bot_data.pop("admin_editing_listing_id", None)
        removed["transient"] += 1

    return removed


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


def published_status_label(status):
    if status == "rented":
        return "🔴 Сдано"
    if status == "removed":
        return "⚪ Снято"
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
        rows.append([InlineKeyboardButton("↩️ Вернуть в активные", callback_data=f"pub_active_{listing_id}")])
    else:
        rows.append([InlineKeyboardButton("🔴 Отметить как сдано", callback_data=f"pub_rented_{listing_id}")])
        rows.append([
            InlineKeyboardButton(FINANCIAL_FIELDS["price"]["button"], callback_data=f"pub_money_price_{listing_id}"),
            InlineKeyboardButton(FINANCIAL_FIELDS["deposit"]["button"], callback_data=f"pub_money_deposit_{listing_id}"),
        ])
        rows.append([InlineKeyboardButton(FINANCIAL_FIELDS["commission"]["button"], callback_data=f"pub_money_commission_{listing_id}")])
    rows.append([InlineKeyboardButton("📋 К списку", callback_data="my_listings")])
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
        issues.append("выберите тип недвижимости")
    elif not headline_matches_property_type(headline, property_type_key):
        label = get_property_type(property_type_key)["button"]
        issues.append(f"заголовок не похож на выбранный тип «{label}»")

    if make_contact_line(contact_url) not in listing and contact_url not in listing:
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


async def send_with_retry(coro_factory, retries=2, delay=0.8, label=""):
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
            async with GEMINI_SEMAPHORE:
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
    prepared = convert_markdown_bold_to_html(text)
    prepared = normalize_listing_hashtags(normalize_russian_headline(prepared))
    for _ in range(2):
        prepared = normalize_listing_hashtags(normalize_russian_headline(prepared))
        prepared = ensure_contact_line(prepared, contact_url)
        if len(prepared) <= LISTING_SOFT_LIMIT:
            return prepared
        prepared = await shorten_listing_if_needed(prepared)

    prepared = normalize_listing_hashtags(normalize_russian_headline(prepared))
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
            async with GEMINI_SEMAPHORE:
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
        "Выберите, как вы хотите опубликовать объявление:",
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
            f'Чтобы получить доступ, напишите <a href="{html.escape(DEFAULT_CONTACT, quote=True)}">администратору</a>.\n\n'
            "Если вы хотите разместить одно объявление без партнёрского доступа, выберите публикацию как собственник",
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
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 10-20 секунд")
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
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 10-20 секунд")
        return
    await start_public_flow(update.message, context, update.effective_user)


async def payment_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_safe = html.escape(DEFAULT_CONTACT, quote=True)
    await update.message.reply_text(
        "<b>Условия разовой публикации</b>\n\n"
        f"Стоимость размещения объявления: <b>{PUBLIC_LISTING_PRICE_CZK} Kč</b>.\n\n"
        "После предпросмотра бот создаёт защищённую страницу оплаты. "
        "После успешной оплаты объявление автоматически отправляется администратору на проверку. "
        "В канале оно появляется только после одобрения.\n\n"
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


async def partner_publish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_state(context, update.effective_user.id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 10-20 секунд")
        return
    await start_partner_flow(update.message, context, update.effective_user)


async def employee_change_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if get_state(context, user_id) == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 10-20 секунд")
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
        await process_listing(update, context, update.message.text)
    elif state == "published_money_edit":
        await partner_apply_money_update(update, context, update.message.text)
    elif state == "processing":
        await update.message.reply_text("Объявление ещё обрабатывается\n\nОбычно это занимает 10-20 секунд")
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

    await update.message.reply_text("Обрабатываю объявление\n\nОбычно это занимает 10-20 секунд")
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
            "Сервис временно недоступен\n\nПожалуйста, отправьте текст ещё раз через несколько секунд"
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
        await query.answer()
        await query.message.reply_text(
            "Ссылка на оплату уже создана\n\n"
            "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты Stripe",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"💳 Оплатить {PUBLIC_LISTING_PRICE_CZK} Kč", url=pending["stripe_checkout_url"])
            ]])
        )
        return
    pending["invoice_created_at"] = now_iso()
    mark_pending_busy(context, listing_id, pending, "invoice_in_progress")

    formatted_listing = await prepare_listing_for_caption(
        pending['formatted_listing'],
        pending.get('contact_url', DEFAULT_CONTACT),
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
            currency=PUBLIC_PAYMENT_CURRENCY,
            prices=[LabeledPrice(label="Публикация объявления", amount=PUBLIC_PAYMENT_AMOUNT)],
            start_parameter=f"public-{listing_id}",
        )
    except Exception as e:
        logger.error(f"send_public_invoice error: {e}")
        pending.pop("invoice_created_at", None)
        clear_pending_busy(context, listing_id, pending, "invoice_in_progress")
        await query.message.reply_text("Не получилось отправить счёт на оплату\n\nПопробуйте ещё раз позже")
        return
    clear_pending_busy(context, listing_id, pending, "invoice_in_progress")


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
        await query.answer("Объявление ещё обрабатывается. Обычно это занимает 10-20 секунд.", show_alert=True)
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

    await query.answer()
    set_state(context, user_id, "processing")
    await query.message.reply_text("Готовлю более аккуратный вариант текста")
    typing_task = asyncio.create_task(
        keep_chat_action(context.bot, query.message.chat_id, ChatAction.TYPING)
    )

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

        await query.message.reply_text("Готово\n\nПроверьте новый вариант")
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
        await query.answer("Объявление ещё обрабатывается. Обычно это занимает 10-20 секунд.", show_alert=True)
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

    plain_text = pending['formatted_listing']
    contact_url_saved = pending.get('contact_url', DEFAULT_CONTACT)
    plain_text = remove_contact_from_listing(plain_text, contact_url_saved)
    plain_text = strip_html_tags_keep_text(plain_text)
    await send_plain_text_chunks(
        context.bot,
        query.message.chat_id,
        f"Текущий текст для редактирования:\n\n{plain_text}",
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

    mark_pending_busy(context, listing_id, pending, "submit_in_progress")
    await query.answer()

    partner_label = pending.get('partner_label') or format_partner_for_admin(update.effective_user)
    try:
        issues = await send_pending_to_admin(context, listing_id, pending, partner_label, label="partner_submit")
    except Exception as e:
        logger.error(f"partner_submit send error: {e}")
        clear_pending_busy(context, listing_id, pending, "submit_in_progress")
        await query.message.reply_text(f"Не получилось отправить объявление на проверку\n\n{e}")
        return
    if issues:
        clear_pending_busy(context, listing_id, pending, "submit_in_progress")
        await query.message.reply_text(
            validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return

    pending['submitted_to_admin'] = True
    pending.pop('submit_in_progress', None)
    save_pending(context, listing_id, pending)
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
        or query.currency != PUBLIC_PAYMENT_CURRENCY
        or query.total_amount != PUBLIC_PAYMENT_AMOUNT
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


async def submit_paid_public_listing(context, listing_id, user, reply_message, test_mode=False):
    pending = get_pending(context, listing_id) if listing_id else None

    if not pending or pending.get("source") != "public":
        logger.warning("submit_paid_public_listing: pending public listing not found")
        return False
    if pending.get("partner_id") != user.id:
        logger.warning("submit_paid_public_listing: user_id mismatch")
        return False
    if pending.get("submitted_to_admin"):
        await reply_message.reply_text("✅ Объявление уже отправлено на проверку")
        return True
    if pending_busy(pending, "submit_in_progress"):
        await reply_message.reply_text("Объявление уже отправляется на проверку")
        return True

    pending["paid"] = True
    pending.setdefault("payment_paid_at", now_iso())
    mark_pending_busy(context, listing_id, pending, "submit_in_progress")
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
        clear_pending_busy(context, listing_id, pending, "submit_in_progress")
        await reply_message.reply_text(
            "✅ Оплата получена, но объявление не получилось отправить на проверку\n\nАдминистратор получит уведомление"
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
        clear_pending_busy(context, listing_id, pending, "submit_in_progress")
        await reply_message.reply_text(
            "✅ Оплата получена, но перед проверкой объявление нужно поправить\n\n"
            + validation_message(issues),
            reply_markup=listing_fix_keyboard(listing_id),
        )
        return False

    pending["submitted_to_admin"] = True
    pending.setdefault("submitted_at", now_iso())
    pending.pop("submit_in_progress", None)
    save_pending(context, listing_id, pending)
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
    pending.setdefault("payment_paid_at", now_iso())
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
        await update.message.reply_text("Эта команда доступна только администратору")
        return

    removed = clear_pending_for_user(context, user.id)
    await update.message.reply_text(
        "Очистка завершена.\n\n"
        f"Удалено ваших незавершённых заявок и тестовых предпросмотров: {removed}.\n\n"
        "Опубликованные объявления и заявки других пользователей не тронуты"
    )


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

    stripe_status = "подключён" if STRIPE_ENABLED else "не подключён"
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
        "<b>Опубликованные объявления партнёров:</b>\n"
        f"— всего объявлений: {published_total}\n"
        f"— активные: {published_active}\n"
        f"— сдано: {published_rented}\n"
        f"— снято: {published_removed}\n"
        f"— авторов объявлений: {len(published_partner_ids)}\n\n"
        "<b>Партнёры:</b>\n"
        f"— с привязкой к сотруднику: {len(partner_ids)}\n\n"
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


async def partner_my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    listings = list_partner_published(context, user_id)
    target = update.callback_query.message if update.callback_query else update.message

    if update.callback_query:
        await update.callback_query.answer()

    if not listings:
        text = (
            "У вас пока нет опубликованных объявлений.\n\n"
            "Когда объявление пройдёт проверку и появится в канале, оно будет доступно здесь"
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
        "Внутри можно отметить объект как сданный или изменить цену, залог и комиссию"
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
        await update.message.reply_text("Объявление не найдено\n\nОткройте /mylistings и попробуйте ещё раз")
        return

    new_value = normalize_financial_value(new_value)
    if len(new_value) < 2:
        await update.message.reply_text(f"Напишите новое значение для поля «{field['label']}»\n\nНапример: 20 000 Kč")
        return

    base_listing = replace_financial_line(item.get("listing", ""), field_key, new_value)
    visible_listing = listing_with_status(base_listing, item.get("status", "active"))

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
        if pending_busy(pending, "admin_action_in_progress"):
            await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
            return
        mark_pending_busy(context, listing_id, pending, "admin_action_in_progress", "approve")

        listing = await prepare_listing_for_caption(
            pending['formatted_listing'],
            pending.get('contact_url', DEFAULT_CONTACT),
        )
        if listing != pending.get('formatted_listing'):
            pending['formatted_listing'] = listing
            save_pending(context, listing_id, pending)
        issues = validate_listing_ready(pending, listing)
        if issues:
            clear_pending_busy(context, listing_id, pending, "admin_action_in_progress")
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
            clear_pending_busy(context, listing_id, pending, "admin_action_in_progress")
            await query.message.reply_text(f"⚠️ Ошибка публикации: {e}")
            return

        channel_message_id = getattr(published_message, "message_id", None)
        channel_chat_id = getattr(getattr(published_message, "chat", None), "id", CHANNEL_USERNAME)
        partner_id = pending.get('partner_id')
        is_public_paid = pending.get("source") == "public"
        post_url = channel_post_url(CHANNEL_USERNAME, channel_message_id) if channel_message_id else None
        if channel_message_id:
            save_published(context, listing_id, {
                "listing_id": listing_id,
                "partner_id": partner_id,
                "partner_label": pending.get("partner_label"),
                "source": pending.get("source", "partner"),
                "paid": bool(pending.get("paid")),
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
            })
        elif not channel_message_id:
            logger.warning(f"Опубликовано, но не удалось сохранить message_id для listing_id={listing_id}")

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
            await update_admin_action_message(query, "Отправьте исправленный текст объявления одним сообщением\n\nПосле этого бот покажет новый предпросмотр")
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
        if pending_busy(pending, "admin_action_in_progress"):
            await query.message.reply_text("Это объявление уже обрабатывается. Подождите несколько секунд.")
            return
        mark_pending_busy(context, listing_id, pending, "admin_action_in_progress", "reject")
        await update_admin_action_message(query, "❌ Объявление отклонено.")
        partner_id = pending.get('partner_id')
        if partner_id is not None:
            try:
                if pending.get("source") == "public" and pending.get("paid"):
                    reject_text = (
                        "❌ Ваше объявление отклонено администратором.\n\n"
                        "Если нужна помощь по оплате или публикации, свяжитесь с администратором"
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
            "⚠️ Непонятно, какое объявление вы редактируете\n\n"
            "Нажмите «Исправить» под нужным объявлением и повторите"
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
        "⚠️ Не получилось распознать это как текст для правки\n\n"
        "Пожалуйста, отправьте исправленный текст обычным текстовым сообщением "
        "без прикреплённых фото или файлов"
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
                "⚠️ Произошла техническая ошибка\n\nПопробуйте ещё раз через /start"
            )
    except Exception:
        pass


def main():
    validate_config()
    os.makedirs(BOT_DATA_DIR, exist_ok=True)
    logger.info(f"Файл памяти бота: {BOT_DATA_PATH}")
    persistence = PicklePersistence(filepath=BOT_DATA_PATH, update_interval=15)
    app = (
        Application.builder()
        .token(PARTNER_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .post_shutdown(stop_stripe_webhook_server)
        .concurrent_updates(8)
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
    app.add_handler(CommandHandler("terms", payment_terms))
    app.add_handler(CommandHandler("support", payment_support))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("memory", admin_memory))
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



