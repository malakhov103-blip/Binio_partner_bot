import logging
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ============================================================
# НАСТРОЙКИ — ключи хранятся в переменных окружения Railway
# ============================================================
import os
PARTNER_BOT_TOKEN = os.environ["PARTNER_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@binio_praha")
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Состояния разговора
WAITING_PHOTOS, WAITING_TEXT = range(2)

LISTING_TEMPLATE = """
Ты помощник по недвижимости. Отформатируй объявление об аренде квартиры по следующему шаблону.

ПРАВИЛА:
- Никогда не придумывай: цену, район, улицу, адрес, метраж, этаж, количество комнат, залог, комиссию
- Можно добавить приятное описание ("уютная квартира", "светлая планировка") если это логично следует из контекста
- Используй только те данные которые есть в тексте
- Если какого-то блока нет — просто пропусти его
- Хештеги генерируй только самые нужные: тип квартиры, район, #pronajem

ШАБЛОН:
Pronájem bytu [тип и метраж] – [город, район]

[Описание квартиры]

[Транспортная доступность если есть]

[Инфраструктура если есть]

Finanční podmínky:
[Финансовые условия]

Kontakt: [автор](https://t.me/binio_praha)

#[тип] #[район] #pronajem

ТЕКСТ ОТ ПАРТНЁРА:
{text}

Верни только готовое объявление, без пояснений.
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['photos'] = []
    await update.message.reply_text(
        "Добро пожаловать в Binio! 🏠\n\n"
        "Пожалуйста, прикрепите фотографии квартиры.\n"
        "Когда все фото загружены — нажмите кнопку «Готово».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")]
        ])
    )
    return WAITING_PHOTOS


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'photos' not in context.user_data:
        context.user_data['photos'] = []

    photo = update.message.photo[-1]
    context.user_data['photos'].append(photo.file_id)

    await update.message.reply_text(
        f"📸 Фото получено ({len(context.user_data['photos'])} шт.). "
        "Добавьте ещё или нажмите «Фото загружены».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")]
        ])
    )
    return WAITING_PHOTOS


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not context.user_data.get('photos'):
        await query.message.reply_text(
            "⚠️ Пожалуйста, сначала загрузите хотя бы одно фото квартиры."
        )
        return WAITING_PHOTOS

    await query.message.reply_text(
        "Отлично! Теперь, пожалуйста, напишите описание квартиры.\n\n"
        "Укажите всё что знаете: район, цену, метраж, условия аренды и любые детали."
    )
    return WAITING_TEXT


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['listing_text'] = text

    await update.message.reply_text("⏳ Обрабатываю объявление, пожалуйста подождите...")

    try:
        prompt = LISTING_TEMPLATE.format(text=text)
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        formatted_listing = response.text

        context.user_data['formatted_listing'] = formatted_listing
        partner_username = update.effective_user.username or update.effective_user.first_name

        # Отправляем админу на одобрение
        admin_message = (
            f"📋 Новое объявление от партнёра @{partner_username}\n\n"
            f"{'─' * 30}\n\n"
            f"{formatted_listing}\n\n"
            f"{'─' * 30}\n\n"
            f"✏️ Чтобы опубликовать как есть — нажмите ДА\n"
            f"❌ Чтобы отклонить — нажмите НЕТ\n"
            f"📝 Чтобы отредактировать — отправьте исправленный текст объявления"
        )

        # Сохраняем фото и текст для отправки
        photos = context.user_data['photos']
        context.user_data['partner_id'] = update.effective_user.id
        context.user_data['partner_username'] = partner_username

        # Отправляем фото
        if len(photos) == 1:
            await context.bot.send_photo(
                chat_id=ADMIN_TELEGRAM_ID,
                photo=photos[0]
            )
        else:
            media_group = []
            from telegram import InputMediaPhoto
            for photo_id in photos:
                media_group.append(InputMediaPhoto(media=photo_id))
            await context.bot.send_media_group(
                chat_id=ADMIN_TELEGRAM_ID,
                media=media_group
            )

        # Отправляем текст с кнопками
        sent_message = await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=admin_message,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ ДА — опубликовать", callback_data=f"approve_{update.effective_user.id}"),
                    InlineKeyboardButton("❌ НЕТ — отклонить", callback_data=f"reject_{update.effective_user.id}")
                ]
            ])
        )

        # Сохраняем message_id для редактирования
        context.application.bot_data[f"pending_{update.effective_user.id}"] = {
            'formatted_listing': formatted_listing,
            'photos': photos,
            'partner_id': update.effective_user.id,
            'partner_username': partner_username,
            'message_id': sent_message.message_id
        }

        await update.message.reply_text(
            "✅ Ваше объявление успешно принято и отправлено на проверку!\n\n"
            "Для добавления нового объявления напишите /start"
        )

    except Exception as e:
        logger.error(f"Error processing listing: {e}")
        await update.message.reply_text(
            "⚠️ Произошла ошибка при обработке. Пожалуйста, попробуйте ещё раз через /start"
        )

    context.user_data.clear()
    return ConversationHandler.END


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    data = query.data

    if data.startswith("approve_"):
        partner_id = int(data.split("_")[1])
        pending = context.application.bot_data.get(f"pending_{partner_id}")

        if pending:
            listing = pending['formatted_listing']
            photos = pending['photos']

            # Публикуем в канал
            from telegram import InputMediaPhoto
            if len(photos) == 1:
                await context.bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=photos[0],
                    caption=listing
                )
            else:
                media_group = []
                for i, photo_id in enumerate(photos):
                    if i == 0:
                        media_group.append(InputMediaPhoto(media=photo_id, caption=listing))
                    else:
                        media_group.append(InputMediaPhoto(media=photo_id))
                await context.bot.send_media_group(
                    chat_id=CHANNEL_USERNAME,
                    media=media_group
                )

            await query.edit_message_text(
                text=f"✅ Объявление опубликовано в {CHANNEL_USERNAME}\n\n{listing}"
            )
            del context.application.bot_data[f"pending_{partner_id}"]

    elif data.startswith("reject_"):
        partner_id = int(data.split("_")[1])
        await query.edit_message_text(text="❌ Объявление отклонено.")
        if f"pending_{partner_id}" in context.application.bot_data:
            del context.application.bot_data[f"pending_{partner_id}"]


async def admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ редактирует текст и отправляет обратно — публикуем"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    edited_text = update.message.text

    # Ищем любое ожидающее объявление
    pending_key = None
    for key in context.application.bot_data:
        if key.startswith("pending_"):
            pending_key = key
            break

    if pending_key:
        pending = context.application.bot_data[pending_key]
        photos = pending['photos']

        from telegram import InputMediaPhoto
        if len(photos) == 1:
            await context.bot.send_photo(
                chat_id=CHANNEL_USERNAME,
                photo=photos[0],
                caption=edited_text
            )
        else:
            media_group = []
            for i, photo_id in enumerate(photos):
                if i == 0:
                    media_group.append(InputMediaPhoto(media=photo_id, caption=edited_text))
                else:
                    media_group.append(InputMediaPhoto(media=photo_id))
            await context.bot.send_media_group(
                chat_id=CHANNEL_USERNAME,
                media=media_group
            )

        await update.message.reply_text(f"✅ Отредактированное объявление опубликовано в {CHANNEL_USERNAME}")
        del context.application.bot_data[pending_key]
    else:
        await update.message.reply_text("⚠️ Нет ожидающих объявлений.")


def main():
    app = Application.builder().token(PARTNER_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                CallbackQueryHandler(photos_done, pattern="^photos_done$")
            ],
            WAITING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)
            ]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve|reject)_"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_TELEGRAM_ID),
        admin_edit
    ))

    logger.info("Binio Partner Bot запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
