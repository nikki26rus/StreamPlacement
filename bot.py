import html
import logging
import os
import sqlite3
import sys
import time
from urllib.parse import urlparse

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    COMBINE_DELAY_SECONDS,
    DB_PATH,
    FAST_POLL_INTERVAL_SECONDS,
    SLOW_SCRAPE_POLL_INTERVAL_SECONDS,
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
    YOUTUBE_POLL_INTERVAL_SECONDS,
)
from constants import (
    ADMIN_STATUSES,
    BUTTON_STYLES,
    DEFAULT_BUTTON_EMOJIS,
    MENU_ADD,
    MENU_APPEARANCE,
    MENU_CANCEL,
    MENU_CHECK,
    MENU_HELP,
    MENU_SUBSCRIPTIONS,
    PLATFORM_NAMES,
    SCRAPE_PLATFORMS,
    SLOW_SCRAPE_PLATFORMS,
)
from notifications import (
    delayed_notification,
    fetch_live_stream,
    format_live_notification,
    notification_keyboard,
    send_or_edit_notification,
)
from providers import (
    StreamProviders,
    parse_discord_url,
    parse_kick_url,
    parse_public_platform_url,
    parse_twitch_url,
    public_channel_url,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


from database import Database


async def resolve_channel(
    providers: StreamProviders, platform: str, url: str
) -> tuple[str, str, str]:
    if platform == "twitch":
        return parse_twitch_url(url)
    if platform == "youtube":
        return await providers.youtube_channel_id(url)
    if platform == "kick":
        return await providers.kick_channel(parse_kick_url(url))
    if platform in {"vk", "rutube", "instagram", "tiktok"}:
        return await providers.public_channel(
            platform, parse_public_platform_url(platform, url)
        )
    raise ValueError(f"Неизвестная платформа: {platform}")


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_ADD, MENU_SUBSCRIPTIONS],
            [MENU_CHECK, MENU_APPEARANCE],
            [MENU_HELP, MENU_CANCEL],
        ],
        resize_keyboard=True,
    )


def main_inline_keyboard() -> InlineKeyboardMarkup:
    """Очищает inline-клавиатуру: основные разделы находятся в Telegram Menu."""
    return InlineKeyboardMarkup([])


async def render_ui(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показывает единственный актуальный экран навигации внизу переписки."""
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(text, reply_markup=reply_markup)
        return
    previous_message_id = context.user_data.get("ui_message_id")
    query = update.callback_query
    if query and query.message and not previous_message_id:
        previous_message_id = query.message.message_id
    if previous_message_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=previous_message_id,
            )
        except BadRequest:
            pass
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    context.user_data["ui_message_id"] = message.message_id


def clear_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "wizard",
        "pending_subscription",
        "pending_template",
        "pending_description",
        "template_edit_chat_id",
        "description_edit_chat_id",
        "pending_discord_url",
        "emoji_chat_id",
        "emoji_platform",
        "custom_button_chat_id",
        "custom_button_label",
        "custom_button_url",
        "custom_button_index",
        "custom_button_emoji_index",
        "button_rename_kind",
        "button_rename_id",
        "platform_group_chat_id",
        "platform_group_platform",
        "platform_group_subscription_id",
        "awaiting_preview",
        "pending_preview_file_id",
    ):
        context.user_data.pop(key, None)


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Выбери действие:"
) -> None:
    clear_wizard(context)
    await render_ui(
        update,
        context,
        f"{text}\n\nОсновные действия доступны в кнопке Menu рядом с полем ввода.",
        main_inline_keyboard(),
    )


def help_text() -> str:
    return (
        "Я сообщаю о начале Twitch и YouTube-эфиров.\n\n"
        "1. Добавь меня администратором в нужный канал или группу. "
        "Ничего писать там не нужно.\n"
        "2. В личке нажми «Добавить канал» и следуй подсказкам.\n"
        "3. В «Оформлении» можно изменить заголовок уведомления и установить "
        "свою картинку.\n\n"
        "Кнопка «Проверить эфиры» запускает проверку сразу. "
        "Команды /add, /list, /remove, /check, /template и /preview "
        "остались доступны как запасной вариант.\n\n"
        "Первый опрос только запоминает текущий статус: уже идущий эфир "
        "не вызовет уведомление. Следующий новый эфир — вызовет."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Главные функции находятся на кнопках под полем ввода.",
        reply_markup=main_menu(),
    )
    await show_main_menu(
        update,
        context,
        "Добро пожаловать. Я сообщаю о начале Twitch и YouTube-эфиров.",
    )


async def track_connected_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Запоминает чат, когда пользователя добавляет в него самого бота."""
    change = update.my_chat_member
    if not change or change.new_chat_member.status not in ADMIN_STATUSES:
        return

    chat = change.chat
    actor = change.from_user
    database: Database = context.application.bot_data["database"]
    database.connect_chat(chat.id, chat.title or str(chat.id), actor.id)
    logger.info(
        "Бот подключён к чату chat_id=%s пользователем user_id=%s",
        chat.id,
        actor.id,
    )


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type != "supergroup" or not chat.is_forum:
        await message.reply_text(
            "Эта команда работает только внутри темы Telegram-форума."
        )
        return
    if not message.message_thread_id:
        await message.reply_text("Открой нужную тему форума и повтори /topic там.")
        return

    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
    if member.status not in ADMIN_STATUSES:
        await message.reply_text("Выбрать тему может только администратор форума.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.is_configured(chat.id):
        database.connect_chat(
            chat.id, chat.title or str(chat.id), update.effective_user.id
        )
    database.set_notification_thread(chat.id, message.message_thread_id)
    await message.reply_text(
        "Готово. Уведомления для этого форума теперь будут приходить в эту тему."
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if chat.type != "private":
        await message.reply_text(
            "Каналы добавляются в личке с ботом."
        )
        return

    database: Database = context.application.bot_data["database"]
    providers: StreamProviders = context.application.bot_data["providers"]

    if len(context.args) != 2:
        await message.reply_text(
            "Формат: /add twitch|youtube|kick|vk|rutube|instagram|tiktok <ссылка>"
        )
        return

    platform, url = context.args[0].lower(), context.args[1]
    platform = {"twich": "twitch"}.get(platform, platform)
    try:
        channel_key, channel_name, channel_url = await resolve_channel(
            providers, platform, url
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        logger.warning("Не удалось добавить канал: %s", error)
        await message.reply_text(f"Не удалось добавить канал: {error}")
        return

    chats = database.list_user_chats(user.id)
    if not chats:
        await message.reply_text(
            "Добавь бота администратором в нужный канал или группу, "
            "затем подожди несколько секунд и повтори /add."
        )
        return

    context.user_data["pending_subscription"] = {
        "platform": platform,
        "channel_key": channel_key,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"target_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await message.reply_text(
        f"Канал «{channel_name}» добавляется. Выбери чат для уведомлений:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def begin_subscription_from_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, url: str
) -> None:
    message = update.effective_message
    database: Database = context.application.bot_data["database"]
    providers: StreamProviders = context.application.bot_data["providers"]
    try:
        channel_key, channel_name, channel_url = await resolve_channel(
            providers, platform, url
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        await render_ui(
            update,
            context,
            f"Не удалось добавить канал: {error}\nПришли корректную ссылку или нажми «Отмена».",
        )
        return

    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(
            update,
            context,
            "Сначала добавь бота администратором в нужный канал или группу, "
            "затем повтори добавление.",
        )
        return

    context.user_data["pending_subscription"] = {
        "platform": platform,
        "channel_key": channel_key,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }
    context.user_data["wizard"] = "add_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"target_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update,
        context,
        f"Канал «{channel_name}» найден. Куда отправлять уведомления?",
        InlineKeyboardMarkup(keyboard),
    )


async def show_subscriptions_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await render_ui(update, context, "Подписок пока нет.", main_inline_keyboard())
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"{subscription['platform'].title()} · {subscription['channel_name']}",
                callback_data=f"subscription:{subscription['id']}",
            )
        ]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton("В меню", callback_data="menu:home")])
    await render_ui(
        update,
        context,
        "Выбери подписку, чтобы посмотреть её или удалить:",
        InlineKeyboardMarkup(keyboard),
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Список доступен в личке с ботом.")
        return

    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await update.effective_message.reply_text(
            "Подписок пока нет. Добавь канал: /add twitch <ссылка>"
        )
        return

    lines = ["Отслеживаемые каналы:"]
    for subscription in subscriptions:
        status = "в эфире" if subscription["active_stream_id"] else "офлайн"
        lines.append(
            f"#{subscription['id']} · {html.escape(subscription['chat_title'])} · "
            f"{subscription['platform']} · "
            f"<a href=\"{html.escape(subscription['channel_url'], quote=True)}\">"
            f"{html.escape(subscription['channel_name'])}</a> · {status}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def subscriptions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Раздел доступен в личке с ботом.")
        return
    await show_subscriptions_menu(update, context)


async def appearance_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Раздел доступен в личке с ботом.")
        return
    await show_appearance_menu(update, context)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Удаление подписок доступно в личке с ботом.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Формат: /remove <номер из /list>")
        return

    database: Database = context.application.bot_data["database"]
    removed = database.remove_user_subscription(
        update.effective_user.id, int(context.args[0])
    )
    await update.effective_message.reply_text(
        "Подписка удалена." if removed else "Подписка с таким номером не найдена."
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Проверка доступна в личке с ботом.")
        return
    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await render_ui(update, context, "У тебя нет доступных подписок.", main_inline_keyboard())
        return

    await render_ui(update, context, "Проверяю каналы…")
    results = await check_streams(
        context.application,
        only_subscription_ids={subscription["id"] for subscription in subscriptions},
    )
    await render_ui(
        update,
        context,
        "Результат проверки:\n" + "\n".join(results),
        main_inline_keyboard(),
    )


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Список чатов доступен в личке с ботом.")
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text(
            "Нет подключённых чатов. Добавь бота администратором в канал "
            "или группу, затем подожди несколько секунд."
        )
        return
    await update.effective_message.reply_text(
        "Подключённые группы:\n" + "\n".join(f"• {chat['title']}" for chat in chats)
    )


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Настройка текста уведомления доступна в личке с ботом."
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /template <текст>\n\n"
            "Это заголовок уведомления. Можно использовать {count} — "
            "число начавшихся эфиров, {time} — время начала, а также "
            "{titleYT}, {titleTwich}, {titleKick} и {categoryYT}, "
            "{categoryTwich}, {categoryKick}.\n"
            "Пример: /template 🔴 {titleTwich} начал эфир в {time}"
        )
        return

    template = " ".join(context.args).strip()
    if len(template) > 300:
        await update.effective_message.reply_text(
            "Текст слишком длинный: максимум 300 символов."
        )
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text("Нет доступных каналов или групп.")
        return

    context.user_data["pending_template"] = template
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"template_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await update.effective_message.reply_text(
        "Выбери канал или группу, для которых изменить заголовок уведомления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Свою картинку нужно настроить в личке с ботом."
        )
        return
    context.user_data["awaiting_preview"] = True
    await update.effective_message.reply_text(
        "Отправь мне картинку как фото. Затем выбери канал или группу, "
        "в уведомлениях которых её использовать."
    )


async def receive_preview_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        return
    if not context.user_data.get("awaiting_preview"):
        await update.effective_message.reply_text(
            "Чтобы установить эту картинку, сначала отправь команду /preview."
        )
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text("Нет доступных каналов или групп.")
        return

    context.user_data["pending_preview_file_id"] = update.effective_message.photo[-1].file_id
    context.user_data.pop("awaiting_preview", None)
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"preview_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await update.effective_message.reply_text(
        "Выбери канал или группу для этой картинки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_appearance_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await render_ui(
        update,
        context,
        "Оформление уведомлений:",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Заголовок", callback_data="appearance:template"
                    ),
                    InlineKeyboardButton(
                        "✏️ Описание", callback_data="appearance:description"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Своя картинка", callback_data="appearance:preview"
                    ),
                    InlineKeyboardButton(
                        "🗑 Удалить картинку", callback_data="appearance:clear_preview"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Источник превью", callback_data="appearance:preview_source"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🌫 Блюр превью", callback_data="appearance:blur"
                    ),
                    InlineKeyboardButton(
                        "🔗 Кнопки уведомления",
                        callback_data="appearance:buttons",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "👁 Пример", callback_data="appearance:example"
                    ),
                    InlineKeyboardButton(
                        "ℹ️ Настройки", callback_data="appearance:status"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "♻️ Сбросить настройки",
                        callback_data="appearance:reset",
                    )
                ],
                [InlineKeyboardButton("В меню", callback_data="menu:home")],
            ]
        ),
    )


async def choose_template_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, template: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_template"] = template
    context.user_data["wizard"] = "template_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"template_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, для которых изменить заголовок:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_template_edit_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"], callback_data=f"template_chat:{chat_row['chat_id']}"
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, где изменить заголовок:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_description_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, description: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_description"] = description
    context.user_data["wizard"] = "description_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"description_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, для которых изменить описание:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_description_edit_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"description_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, где изменить описание:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_example_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"example_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери настройки какого канала или группы показать:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_discord_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_discord_url"] = url
    context.user_data["wizard"] = "discord_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"discord_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу для Discord-кнопки:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_preview_clear_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"clear_preview_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, где удалить свою картинку:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_preview_source_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"preview_source_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу для настройки источника превью:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_emoji_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"emoji_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, для которых изменить эмодзи:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_custom_button_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"custom_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу для кастомных кнопок:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_button_settings_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat["title"], callback_data=f"button_settings_chat:{chat['chat_id']}"
            )
        ]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери канал или группу для настройки кнопок:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_button_color_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"color_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу для цвета кнопок:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_individual_button_color_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [
            InlineKeyboardButton(
                chat["title"], callback_data=f"individual_colors_chat:{chat['chat_id']}"
            )
        ]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери канал или группу для настройки цвета каждой кнопки:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_blur_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"blur_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await render_ui(
        update, context,
        "Выбери канал или группу, где включить или выключить блюр:",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_reset_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat["title"], callback_data=f"reset_chat:{chat['chat_id']}"
            )
        ]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери канал или группу. Подписки на стримеров сохранатся.",
        InlineKeyboardMarkup(keyboard),
    )


async def choose_platform_group_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat["title"], callback_data=f"platform_groups_chat:{chat['chat_id']}"
            )
        ]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери канал или группу для распределения кнопок привязанных каналов:",
        InlineKeyboardMarkup(keyboard),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    database: Database = context.application.bot_data["database"]

    if data == "menu:home":
        await show_main_menu(update, context)
        return
    if data == "menu:help":
        await render_ui(
            update,
            context,
            help_text(),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("В меню", callback_data="menu:home")]]
            ),
        )
        return
    if data == "menu:add":
        clear_wizard(context)
        context.user_data["wizard"] = "add_platform"
        await query.edit_message_text(
            "Выбери платформу:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Twitch", callback_data="add:twitch"),
                        InlineKeyboardButton("YouTube", callback_data="add:youtube"),
                    ],
                    [
                        InlineKeyboardButton("Kick", callback_data="add:kick"),
                        InlineKeyboardButton("VK", callback_data="add:vk"),
                    ],
                    [
                        InlineKeyboardButton("Rutube", callback_data="add:rutube"),
                        InlineKeyboardButton("Instagram", callback_data="add:instagram"),
                    ],
                    [InlineKeyboardButton("TikTok", callback_data="add:tiktok")],
                    [InlineKeyboardButton("Отмена", callback_data="menu:home")],
                ]
            ),
        )
        return
    if data.startswith("add:"):
        platform = data.removeprefix("add:")
        context.user_data["wizard"] = "add_url"
        context.user_data["add_platform"] = platform
        await query.edit_message_text(
            f"Пришли ссылку на канал {platform.title()}.\n"
            "Например: https://www.twitch.tv/streamer, https://youtube.com/@channel "
            "или ссылку на канал выбранной площадки."
        )
        return
    if data == "menu:subscriptions":
        await show_subscriptions_menu(update, context)
        return
    if data.startswith("subscription:"):
        subscription_id = int(data.removeprefix("subscription:"))
        subscriptions = database.list_user_subscriptions(update.effective_user.id)
        subscription = next(
            (item for item in subscriptions if item["id"] == subscription_id), None
        )
        if not subscription:
            await query.edit_message_text("Подписка не найдена.")
            return
        status = "в эфире" if subscription["active_stream_id"] else "офлайн"
        await query.edit_message_text(
            f"{subscription['platform'].title()} · {subscription['channel_name']}\n"
            f"Получатель: {subscription['chat_title']}\nСтатус: {status}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Удалить", callback_data=f"remove:ask:{subscription_id}"
                        )
                    ],
                    [InlineKeyboardButton("Назад", callback_data="menu:subscriptions")],
                ]
            ),
        )
        return
    if data.startswith("remove:ask:"):
        subscription_id = int(data.removeprefix("remove:ask:"))
        await query.edit_message_text(
            "Удалить эту подписку?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить", callback_data=f"remove:confirm:{subscription_id}"
                        ),
                        InlineKeyboardButton(
                            "Нет", callback_data=f"subscription:{subscription_id}"
                        ),
                    ]
                ]
            ),
        )
        return
    if data.startswith("remove:confirm:"):
        subscription_id = int(data.removeprefix("remove:confirm:"))
        removed = database.remove_user_subscription(
            update.effective_user.id, subscription_id
        )
        await query.edit_message_text(
            "Подписка удалена." if removed else "Подписка уже удалена.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "К подпискам", callback_data="menu:subscriptions"
                        )
                    ],
                    [InlineKeyboardButton("В меню", callback_data="menu:home")],
                ]
            ),
        )
        return
    if data == "menu:check":
        await query.edit_message_text("Проверяю каналы…")
        await check_command(update, context)
        return
    if data == "menu:appearance":
        await show_appearance_menu(update, context)
        return
    if data == "appearance:template":
        clear_wizard(context)
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_template_edit_target(update, context)
        return
    if data == "appearance:description":
        clear_wizard(context)
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_description_edit_target(update, context)
        return
    if data == "appearance:preview":
        clear_wizard(context)
        context.user_data["awaiting_preview"] = True
        await query.edit_message_text(
            "Пришли картинку как фото.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
            ),
        )
        return
    if data == "appearance:clear_preview":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_preview_clear_target(update, context)
        return
    if data == "appearance:preview_source":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_preview_source_target(update, context)
        return
    if data == "appearance:emojis":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_emoji_target(update, context)
        return
    if data == "appearance:colors":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_button_color_target(update, context)
        return
    if data == "appearance:individual_colors":
        await choose_individual_button_color_target(update, context)
        return
    if data == "appearance:buttons":
        await choose_button_settings_target(update, context)
        return
    if data == "appearance:custom_buttons":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_custom_button_target(update, context)
        return
    if data == "appearance:blur":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_blur_target(update, context)
        return
    if data == "appearance:reset":
        await choose_reset_target(update, context)
        return
    if data == "appearance:platform_groups":
        await choose_platform_group_target(update, context)
        return
    if data == "appearance:example":
        await query.edit_message_text("Выбираю настройки для примера.")
        await choose_example_target(update, context)
        return
    if data == "appearance:status":
        chats = database.list_user_chats(update.effective_user.id)
        if not chats:
            await query.edit_message_text("Нет доступных каналов или групп.")
            return
        lines = ["Текущие настройки:"]
        for chat in chats:
            settings = database.get_notification_settings(chat["chat_id"])
            preview = "своя картинка" if settings["preview_file_id"] else "автопревью"
            description = (
                f"описание: {settings['notification_description']}"
                if settings["notification_description"]
                else "без описания"
            )
            target = (
                f"тема #{settings['notification_thread_id']}"
                if settings["notification_thread_id"]
                else "общий чат"
            )
            blur = "блюр Twitch-превью" if settings["blur_preview"] else "без блюра"
            custom_buttons = len(database.get_custom_buttons(chat["chat_id"]))
            preview_source = {
                "auto": "автовыбор",
                "twitch": "Twitch",
                "youtube": "YouTube",
                "kick": "Kick",
            }[settings["preview_platform"]]
            lines.append(
                f"• {chat['title']}: {settings['notification_template']} "
                f"({target}, {preview}, источник: {preview_source}, {blur}, "
                f"{description}, кастомных кнопок: {custom_buttons})"
            )
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
            ),
        )


async def menu_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        return

    text = update.effective_message.text.strip()
    if text == MENU_CANCEL:
        await show_main_menu(update, context, "Действие отменено.")
        return
    if text == MENU_ADD:
        clear_wizard(context)
        context.user_data["wizard"] = "add_platform"
        await render_ui(
            update,
            context,
            "Выбери платформу:",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Twitch", callback_data="add:twitch"),
                        InlineKeyboardButton("YouTube", callback_data="add:youtube"),
                    ],
                    [
                        InlineKeyboardButton("Kick", callback_data="add:kick"),
                        InlineKeyboardButton("VK", callback_data="add:vk"),
                    ],
                    [
                        InlineKeyboardButton("Rutube", callback_data="add:rutube"),
                        InlineKeyboardButton("Instagram", callback_data="add:instagram"),
                    ],
                    [InlineKeyboardButton("TikTok", callback_data="add:tiktok")],
                    [InlineKeyboardButton("Отмена", callback_data="menu:home")],
                ]
            ),
        )
        return
    if text == MENU_SUBSCRIPTIONS:
        await show_subscriptions_menu(update, context)
        return
    if text == MENU_CHECK:
        await check_command(update, context)
        return
    if text == MENU_APPEARANCE:
        await show_appearance_menu(update, context)
        return
    if text == MENU_HELP:
        await update.effective_message.reply_text(help_text(), reply_markup=main_menu())
        return

    wizard = context.user_data.get("wizard")
    if wizard == "add_url":
        await begin_subscription_from_url(
            update, context, context.user_data["add_platform"], text
        )
        return
    if wizard == "template_text":
        if len(text) > 300:
            await update.effective_message.reply_text(
                "Текст слишком длинный: максимум 300 символов. Пришли другой."
            )
            return
        await choose_template_target(update, context, text)
        return
    if wizard == "template_edit_text":
        if len(text) > 300:
            await update.effective_message.reply_text(
                "Текст слишком длинный: максимум 300 символов. Пришли другой."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["template_edit_chat_id"]
        database.set_notification_template(chat_id, text)
        clear_wizard(context)
        await render_ui(
            update, context, "Заголовок уведомления обновлён.", main_inline_keyboard()
        )
        return
    if wizard == "description_text":
        if len(text) > 350:
            await update.effective_message.reply_text(
                "Описание слишком длинное: максимум 350 символов. Пришли другой текст."
            )
            return
        await choose_description_target(update, context, text)
        return
    if wizard == "description_edit_text":
        if len(text) > 350:
            await update.effective_message.reply_text(
                "Описание слишком длинное: максимум 350 символов. Пришли другой текст."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["description_edit_chat_id"]
        database.set_notification_description(chat_id, text)
        clear_wizard(context)
        await render_ui(
            update, context, "Описание уведомления обновлено.", main_inline_keyboard()
        )
        return
    if wizard == "discord_text":
        try:
            discord_url = parse_discord_url(text)
        except ValueError as error:
            await update.effective_message.reply_text(
                f"{error}\nПришли корректную ссылку или нажми «Отмена»."
            )
            return
        await choose_discord_target(update, context, discord_url)
        return
    if wizard == "emoji_text":
        emoji = text.strip()
        if len(emoji) > 16 or any(character.isspace() for character in emoji):
            await update.effective_message.reply_text(
                "Пришли один эмодзи без пробелов, например 🎮."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["emoji_chat_id"]
        platform_key = context.user_data["emoji_platform"]
        custom_emoji_id = next(
            (
                entity.custom_emoji_id
                for entity in update.effective_message.entities or ()
                if getattr(entity.type, "value", entity.type) == "custom_emoji"
                and entity.custom_emoji_id
            ),
            None,
        )
        if custom_emoji_id:
            database.set_button_custom_emoji_id(
                chat_id, platform_key, custom_emoji_id
            )
        else:
            database.set_button_emoji(chat_id, platform_key, emoji)
            database.set_button_custom_emoji_id(chat_id, platform_key, None)
        platform = platform_key.title()
        clear_wizard(context)
        await render_ui(
            update, context, f"Эмодзи для {platform} сохранён.", main_inline_keyboard()
        )
        return
    if wizard == "custom_button_emoji":
        emoji = "" if text == "-" else text.strip()
        if emoji and (len(emoji) > 16 or any(character.isspace() for character in emoji)):
            await render_ui(
                update,
                context,
                "Пришли один эмодзи без пробелов или «-», чтобы убрать его.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
                ),
            )
            return
        custom_emoji_id = next(
            (
                entity.custom_emoji_id
                for entity in update.effective_message.entities or ()
                if getattr(entity.type, "value", entity.type) == "custom_emoji"
                and entity.custom_emoji_id
            ),
            None,
        )
        database: Database = context.application.bot_data["database"]
        changed = database.set_custom_button_emoji(
            context.user_data["custom_button_chat_id"],
            context.user_data["custom_button_emoji_index"],
            emoji,
            custom_emoji_id,
        )
        clear_wizard(context)
        await render_ui(
            update,
            context,
            "Эмодзи кастомной кнопки сохранён."
            if changed
            else "Кнопка уже удалена.",
            main_inline_keyboard(),
        )
        return
    if wizard == "custom_button_label":
        if not text or len(text) > 64:
            await render_ui(
                update,
                context,
                "Название кнопки должно содержать от 1 до 64 символов.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
                ),
            )
            return
        context.user_data["custom_button_label"] = text
        context.user_data["wizard"] = "custom_button_url"
        await render_ui(
            update,
            context,
            "Пришли ссылку для этой кнопки (http:// или https://).",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
            ),
        )
        return
    if wizard == "button_rename":
        if not text or len(text) > 64:
            await update.effective_message.reply_text(
                "Название кнопки должно содержать от 1 до 64 символов."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["custom_button_chat_id"]
        kind = context.user_data["button_rename_kind"]
        item_id = context.user_data["button_rename_id"]
        if kind == "s":
            database.set_subscription_button_label(chat_id, item_id, text)
            changed = True
        else:
            changed = database.set_custom_button_label(chat_id, item_id, text)
        clear_wizard(context)
        await render_ui(
            update,
            context,
            "Название кнопки сохранено." if changed else "Кнопка уже удалена.",
            main_inline_keyboard(),
        )
        return
    if wizard == "custom_button_url":
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            await render_ui(
                update,
                context,
                "Нужна корректная ссылка, начинающаяся с http:// или https://.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
                ),
            )
            return
        context.user_data["custom_button_url"] = text
        context.user_data["wizard"] = "custom_button_group"
        await render_ui(
            update,
            context,
            "Пришли номер строки от 1 до 20. Кнопки с одинаковым номером "
            "будут расположены в одной строке.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
            ),
        )
        return
    if wizard == "custom_button_group":
        try:
            group = int(text)
        except ValueError:
            group = 0
        if not 1 <= group <= 20:
            await render_ui(
                update,
                context,
                "Пришли номер строки целым числом от 1 до 20.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
                ),
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["custom_button_chat_id"]
        buttons = database.get_custom_buttons(chat_id)
        if len(buttons) >= 20:
            await render_ui(
                update,
                context,
                "Можно добавить не более 20 кастомных кнопок.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
                ),
            )
            return
        database.add_custom_button(
            chat_id,
            context.user_data["custom_button_label"],
            context.user_data["custom_button_url"],
            group,
        )
        clear_wizard(context)
        await render_ui(
            update, context, "Кастомная кнопка сохранена.", main_inline_keyboard()
        )
        return
    if wizard == "custom_button_group_edit":
        try:
            group = int(text)
        except ValueError:
            group = 0
        if not 1 <= group <= 20:
            await update.effective_message.reply_text(
                "Пришли номер строки целым числом от 1 до 20."
            )
            return
        database: Database = context.application.bot_data["database"]
        changed = database.set_custom_button_group(
            context.user_data["custom_button_chat_id"],
            context.user_data["custom_button_index"],
            group,
        )
        clear_wizard(context)
        await render_ui(
            update,
            context,
            "Группа кнопки сохранена." if changed else "Кнопка уже удалена.",
            main_inline_keyboard(),
        )
        return
    if wizard == "platform_button_group":
        try:
            group = int(text)
        except ValueError:
            group = 0
        if not 1 <= group <= 20:
            await update.effective_message.reply_text(
                "Пришли номер строки целым числом от 1 до 20."
            )
            return
        database: Database = context.application.bot_data["database"]
        database.set_subscription_button_group(
            context.user_data["platform_group_chat_id"],
            context.user_data["platform_group_subscription_id"],
            group,
        )
        clear_wizard(context)
        await render_ui(
            update,
            context,
            "Группа кнопки площадки сохранена.",
            main_inline_keyboard(),
        )
        return

    await render_ui(update, context, "Используй кнопки меню.", main_inline_keyboard())


async def select_target_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_subscription")
    if not pending or not query.data.startswith("target_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /add.")
        return

    try:
        chat_id = int(query.data.removeprefix("target_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /add.")
        return

    database: Database = context.application.bot_data["database"]
    user_id = update.effective_user.id
    if not database.user_can_access_chat(user_id, chat_id):
        await query.edit_message_text(
            "Нет доступа к этому чату. Добавь бота в него администратором заново."
        )
        return

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ADMIN_STATUSES:
            await query.edit_message_text(
                "Ты больше не администратор этого чата, поэтому выбрать его нельзя."
            )
            return
        subscription_id = database.add_subscription(chat_id=chat_id, **pending)
    except sqlite3.IntegrityError:
        await query.edit_message_text("Этот канал уже отслеживается в выбранном чате.")
        return
    except Exception as error:
        logger.warning("Не удалось создать подписку: %s", error)
        await query.edit_message_text(f"Не удалось добавить канал: {error}")
        return

    context.user_data.pop("pending_subscription", None)
    context.user_data.pop("wizard", None)
    context.user_data.pop("add_platform", None)
    await query.edit_message_text(
        f"Готово: #{subscription_id} — {pending['channel_name']}.\n"
        "Первый опрос только запомнит текущий статус эфира.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В меню", callback_data="menu:home")]]
        ),
    )


async def select_template_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    template = context.user_data.get("pending_template")
    if not query.data.startswith("template_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /template.")
        return

    try:
        chat_id = int(query.data.removeprefix("template_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /template.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    if template is None:
        settings = database.get_notification_settings(chat_id)
        context.user_data["wizard"] = "template_edit_text"
        context.user_data["template_edit_chat_id"] = chat_id
        await query.edit_message_text(
            "Текущий заголовок:\n"
            f"{settings['notification_template']}\n\n"
            "Пришли новый заголовок. Доступны {count}, {time}, {titleYT}, "
            "{titleTwich}, {titleKick}, {categoryYT}, {categoryTwich}, "
            "{categoryKick}.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
            ),
        )
        return

    database.set_notification_template(chat_id, template)
    context.user_data.pop("pending_template", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Заголовок уведомления сохранён.\n"
        "Предпросмотр: "
        f"{template.replace('{count}', '2').replace('{time}', '12:00')}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def select_description_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    description = context.user_data.get("pending_description")
    if not query.data.startswith("description_chat:"):
        await query.edit_message_text(
            "Эта кнопка уже неактуальна. Открой «Оформление» заново."
        )
        return

    try:
        chat_id = int(query.data.removeprefix("description_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    if description is None:
        settings = database.get_notification_settings(chat_id)
        current_description = settings["notification_description"] or "— пустое —"
        context.user_data["wizard"] = "description_edit_text"
        context.user_data["description_edit_chat_id"] = chat_id
        await query.edit_message_text(
            f"Текущее описание:\n{current_description}\n\n"
            "Пришли новое описание (до 350 символов). Доступны {count}, {time}, "
            "{titleYT}, {titleTwich}, {titleKick}, {categoryYT}, "
            "{categoryTwich}, {categoryKick}.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
            ),
        )
        return

    database.set_notification_description(chat_id, description)
    context.user_data.pop("pending_description", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Описание сохранено. Оно появится в следующем уведомлении.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def select_example_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("example_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Открой «Оформление» заново.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    settings = database.get_notification_settings(chat_id)
    text = format_live_notification(
        [],
        settings["notification_template"],
        settings["notification_description"],
        count_override=1,
    )
    sample_keyboard = notification_keyboard(
        database.get_chat_subscriptions(chat_id),
        database.get_button_emojis(chat_id),
        database.get_button_custom_emoji_ids(chat_id),
        settings["button_style"] or None,
        database.get_button_styles(chat_id),
        database.get_custom_buttons(chat_id),
        database.get_platform_button_groups(chat_id),
        database.get_subscription_button_groups(chat_id),
        database.get_subscription_button_labels(chat_id),
    )
    try:
        if settings["preview_file_id"]:
            await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=settings["preview_file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=sample_keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=sample_keyboard,
            )
    except Exception as error:
        logger.warning("Не удалось показать пример уведомления: %s", error)
        await query.edit_message_text(f"Не удалось показать пример: {error}")
        return

    await query.edit_message_text(
        "Пример отправлен сюда, в личный чат. Уведомление в канале не публиковалось.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def select_discord_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    url = context.user_data.get("pending_discord_url")
    if not url or not query.data.startswith("discord_chat:"):
        await query.edit_message_text(
            "Эта кнопка уже неактуальна. Открой «Оформление» заново."
        )
        return

    try:
        chat_id = int(query.data.removeprefix("discord_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.add_custom_button(chat_id, "Discord", url)
    context.user_data.pop("pending_discord_url", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Discord-ссылка сохранена как кастомная кнопка.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def select_preview_clear_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("clear_preview_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.clear_preview_file_id(chat_id)
    await query.edit_message_text(
        "Своя картинка удалена. В следующем уведомлении будет использовано "
        "автоматическое превью.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def select_preview_source_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("preview_source_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    await query.edit_message_text(
        "Выбери площадку для автоматического превью. Своя загруженная картинка "
        "всегда имеет приоритет.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Автовыбор", callback_data=f"preview_platform:{chat_id}:auto")]]
            + [
                [
                    InlineKeyboardButton(
                        name, callback_data=f"preview_platform:{chat_id}:{platform}"
                    )
                    for platform, name in list(PLATFORM_NAMES.items())[index : index + 2]
                ]
                for index in range(0, len(PLATFORM_NAMES), 2)
            ]
            + [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
        ),
    )


async def set_preview_platform(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, platform = query.data.split(":", 2)
        chat_id = int(chat_id_text)
    except ValueError:
        await query.edit_message_text("Некорректная настройка. Повтори её.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    try:
        database.set_preview_platform(chat_id, platform)
    except ValueError:
        await query.edit_message_text("Неизвестная площадка.")
        return
    label = "автовыбор" if platform == "auto" else PLATFORM_NAMES[platform]
    await query.edit_message_text(
        f"Источник автоматического превью: {label}.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
        ),
    )


async def select_emoji_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("emoji_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    emojis = database.get_button_emojis(chat_id)
    await query.edit_message_text(
        "Выбери кнопку:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{emojis[platform]} {name}",
                        callback_data=f"emoji_platform:{chat_id}:{platform}",
                    )
                    for platform, name in list(PLATFORM_NAMES.items())[index : index + 2]
                ]
                for index in range(0, len(PLATFORM_NAMES), 2)
            ]
            + [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
        ),
    )


async def select_emoji_platform(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, platform = query.data.split(":", 2)
        chat_id = int(chat_id_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    if platform not in DEFAULT_BUTTON_EMOJIS:
        await query.edit_message_text("Неизвестная площадка.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    context.user_data["wizard"] = "emoji_text"
    context.user_data["emoji_chat_id"] = chat_id
    context.user_data["emoji_platform"] = platform
    await query.edit_message_text(
        f"Пришли новый эмодзи для кнопки {platform.title()}.\n"
        "Можно отправить один Unicode-эмодзи или кастомный эмодзи из Telegram.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
        ),
    )


async def select_button_color_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("color_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                name, callback_data=f"color_set:{chat_id}:{style}"
            )
        ]
        for style, name in BUTTON_STYLES.items()
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await query.edit_message_text(
        "Выбери цвет для всех кнопок уведомления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_button_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, style = query.data.split(":", 2)
        chat_id = int(chat_id_text)
        color_name = BUTTON_STYLES[style]
    except (ValueError, KeyError):
        await query.edit_message_text("Некорректный цвет. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    database.set_button_style(chat_id, style)
    await query.edit_message_text(
        f"Цвет «{color_name}» сохранён для всех кнопок уведомления.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
        ),
    )


async def select_individual_button_color_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("individual_colors_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"{PLATFORM_NAMES[subscription['platform']]} · "
                f"{subscription['channel_name']}",
                callback_data=f"individual_color:{chat_id}:s{subscription['id']}",
            )
        ]
        for subscription in database.get_chat_subscriptions(chat_id)
    ]
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    f"Кастомная · {button['label'][:30]}",
                    callback_data=f"individual_color:{chat_id}:c{index}",
                )
            ]
            for index, button in enumerate(database.get_custom_buttons(chat_id))
        ]
    )
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери кнопку. Индивидуальный цвет имеет приоритет над общим.",
        InlineKeyboardMarkup(keyboard),
    )


async def show_button_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("button_settings_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    await render_ui(
        update,
        context,
        "Настройка кнопок уведомления:",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Кастомные кнопки", callback_data=f"custom_chat:{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Эмодзи площадок", callback_data=f"emoji_chat:{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Группы каналов",
                        callback_data=f"platform_groups_chat:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Переименовать кнопку",
                        callback_data=f"button_rename_chat:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Цвет всех кнопок", callback_data=f"color_chat:{chat_id}"
                    ),
                    InlineKeyboardButton(
                        "Цвет по кнопкам",
                        callback_data=f"individual_colors_chat:{chat_id}",
                    ),
                ],
                [InlineKeyboardButton("Назад", callback_data="menu:appearance")],
            ]
        ),
    )


async def select_button_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("button_rename_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"{PLATFORM_NAMES[item['platform']]} · {item['channel_name']}",
                callback_data=f"button_rename:{chat_id}:s{item['id']}",
            )
        ]
        for item in database.get_chat_subscriptions(chat_id)
    ]
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    f"Кастомная · {button['label'][:30]}",
                    callback_data=f"button_rename:{chat_id}:c{index}",
                )
            ]
            for index, button in enumerate(database.get_custom_buttons(chat_id))
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "Назад", callback_data=f"button_settings_chat:{chat_id}"
            )
        ]
    )
    await render_ui(
        update,
        context,
        "Выбери кнопку для переименования:",
        InlineKeyboardMarkup(keyboard),
    )


async def begin_button_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, raw_key = query.data.split(":", 2)
        chat_id = int(chat_id_text)
        kind, item_id = raw_key[0], int(raw_key[1:])
    except (ValueError, IndexError):
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if kind not in {"s", "c"} or not database.user_can_access_chat(
        update.effective_user.id, chat_id
    ):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    if kind == "s":
        subscription = next(
            (
                item
                for item in database.get_chat_subscriptions(chat_id)
                if item["id"] == item_id
            ),
            None,
        )
        if not subscription:
            await query.edit_message_text("Этот канал больше не привязан.")
            return
        current_label = database.get_subscription_button_labels(chat_id).get(
            str(item_id), PLATFORM_NAMES[subscription["platform"]]
        )
    else:
        buttons = database.get_custom_buttons(chat_id)
        if not 0 <= item_id < len(buttons):
            await query.edit_message_text("Эта кнопка уже удалена.")
            return
        current_label = str(buttons[item_id]["label"])
    context.user_data["wizard"] = "button_rename"
    context.user_data["custom_button_chat_id"] = chat_id
    context.user_data["button_rename_kind"] = kind
    context.user_data["button_rename_id"] = item_id
    await query.edit_message_text(
        f"Текущее название: «{current_label}».\nПришли новое название кнопки.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отмена", callback_data=f"button_rename_chat:{chat_id}"
                    )
                ]
            ]
        ),
    )


async def select_individual_button_color(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, raw_key = query.data.split(":", 2)
        chat_id = int(chat_id_text)
        kind, item_id = raw_key[0], int(raw_key[1:])
        button_key = (
            f"subscription:{item_id}" if kind == "s" else f"custom:{item_id}"
        )
    except (ValueError, IndexError):
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if kind not in {"s", "c"} or not database.user_can_access_chat(
        update.effective_user.id, chat_id
    ):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                name,
                callback_data=f"individual_color_set:{chat_id}:{raw_key}:{style}",
            )
        ]
        for style, name in BUTTON_STYLES.items()
    ]
    keyboard.append(
        [
            InlineKeyboardButton(
                "По умолчанию",
                callback_data=f"individual_color_set:{chat_id}:{raw_key}:default",
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("Назад", callback_data=f"individual_colors_chat:{chat_id}")]
    )
    await render_ui(
        update,
        context,
        "Выбери цвет этой кнопки:",
        InlineKeyboardMarkup(keyboard),
    )


async def set_individual_button_color(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, raw_key, style = query.data.split(":", 3)
        chat_id = int(chat_id_text)
        kind, item_id = raw_key[0], int(raw_key[1:])
        button_key = (
            f"subscription:{item_id}" if kind == "s" else f"custom:{item_id}"
        )
    except (ValueError, IndexError):
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if kind not in {"s", "c"} or not database.user_can_access_chat(
        update.effective_user.id, chat_id
    ):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    try:
        database.set_individual_button_style(
            chat_id, button_key, None if style == "default" else style
        )
    except ValueError:
        await query.edit_message_text("Неизвестный цвет.")
        return
    await query.edit_message_text(
        "Цвет кнопки сохранён.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Назад к кнопкам",
                        callback_data=f"individual_colors_chat:{chat_id}",
                    )
                ],
                [InlineKeyboardButton("В оформление", callback_data="menu:appearance")],
            ]
        ),
    )


async def select_custom_button_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("custom_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    buttons = database.get_custom_buttons(chat_id)
    keyboard = [
        [InlineKeyboardButton("➕ Добавить кнопку", callback_data=f"custom_add:{chat_id}")]
    ]
    keyboard.extend(
        [
            InlineKeyboardButton(
                f"Строка {button['group']}: {button['label'][:32]}",
                callback_data=f"custom_group:{chat_id}:{index}",
            ),
            InlineKeyboardButton(
                button["emoji"] or "😀",
                callback_data=f"custom_emoji:{chat_id}:{index}",
            ),
            InlineKeyboardButton(
                "🗑", callback_data=f"custom_delete:{chat_id}:{index}"
            ),
        ]
        for index, button in enumerate(buttons)
    )
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await query.edit_message_text(
        "Кастомные кнопки: "
        + (str(len(buttons)) if buttons else "пока нет")
        + ". Нажми кнопку, чтобы изменить номер её строки.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def begin_custom_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("custom_add:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    context.user_data["wizard"] = "custom_button_label"
    context.user_data["custom_button_chat_id"] = chat_id
    await query.edit_message_text(
        "Пришли название новой кнопки.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
        ),
    )


async def delete_custom_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, index_text = query.data.split(":", 2)
        chat_id, index = int(chat_id_text), int(index_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    deleted = database.remove_custom_button(chat_id, index)
    await query.edit_message_text(
        "Кнопка удалена." if deleted else "Эта кнопка уже удалена.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Назад к кнопкам", callback_data=f"custom_chat:{chat_id}"
                    )
                ],
                [InlineKeyboardButton("В оформление", callback_data="menu:appearance")],
            ]
        ),
    )


async def begin_custom_button_group_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, index_text = query.data.split(":", 2)
        chat_id, index = int(chat_id_text), int(index_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    buttons = database.get_custom_buttons(chat_id)
    if not 0 <= index < len(buttons):
        await query.edit_message_text("Эта кнопка уже удалена.")
        return
    context.user_data["wizard"] = "custom_button_group_edit"
    context.user_data["custom_button_chat_id"] = chat_id
    context.user_data["custom_button_index"] = index
    await query.edit_message_text(
        f"Текущая строка: {buttons[index]['group']}.\n"
        "Пришли новый номер строки от 1 до 20.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
        ),
    )


async def begin_custom_button_emoji_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, index_text = query.data.split(":", 2)
        chat_id, index = int(chat_id_text), int(index_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    buttons = database.get_custom_buttons(chat_id)
    if not 0 <= index < len(buttons):
        await query.edit_message_text("Эта кнопка уже удалена.")
        return
    context.user_data["wizard"] = "custom_button_emoji"
    context.user_data["custom_button_chat_id"] = chat_id
    context.user_data["custom_button_emoji_index"] = index
    await query.edit_message_text(
        f"Пришли эмодзи для кнопки «{buttons[index]['label']}».\n"
        "Можно отправить Unicode-эмодзи или кастомный эмодзи Telegram. "
        "Отправь «-», чтобы убрать эмодзи.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
        ),
    )


async def select_platform_group_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("platform_groups_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    subscriptions = database.get_chat_subscriptions(chat_id)
    if not subscriptions:
        await render_ui(
            update,
            context,
            "В этом чате пока нет привязанных каналов.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
            ),
        )
        return
    groups = database.get_subscription_button_groups(chat_id)
    platform_groups = database.get_platform_button_groups(chat_id)
    keyboard = [
        [
            InlineKeyboardButton(
                f"{PLATFORM_NAMES[subscription['platform']]} · "
                f"{subscription['channel_name']}: строка "
                f"{groups.get(str(subscription['id']), platform_groups[subscription['platform']])}",
                callback_data=f"subscription_group:{chat_id}:{subscription['id']}",
            )
        ]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu:appearance")])
    await render_ui(
        update,
        context,
        "Выбери привязанный канал. Одинаковый номер строки объединяет его "
        "кнопку с другими каналами и кастомными кнопками.",
        InlineKeyboardMarkup(keyboard),
    )


async def begin_platform_group_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, subscription_id_text = query.data.split(":", 2)
        chat_id, subscription_id = int(chat_id_text), int(subscription_id_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    subscription = next(
        (
            item
            for item in database.get_chat_subscriptions(chat_id)
            if item["id"] == subscription_id
        ),
        None,
    )
    if not subscription:
        await query.edit_message_text("Этот канал больше не привязан.")
        return
    group = database.get_subscription_button_groups(chat_id).get(
        str(subscription_id),
        database.get_platform_button_groups(chat_id)[subscription["platform"]],
    )
    context.user_data["wizard"] = "platform_button_group"
    context.user_data["platform_group_chat_id"] = chat_id
    context.user_data["platform_group_subscription_id"] = subscription_id
    await render_ui(
        update,
        context,
        f"Текущая строка {subscription['channel_name']}: {group}.\n"
        "Пришли новый номер строки от 1 до 20.",
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="menu:appearance")]]
        ),
    )


async def toggle_preview_blur(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("blur_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    enabled = database.toggle_preview_blur(chat_id)
    await query.edit_message_text(
        "Блюр фона Twitch-превью включён. Аватар и обложка категории не размываются."
        if enabled
        else "Блюр фона Twitch-превью выключен.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data="menu:appearance")]]
        ),
    )


async def ask_reset_notification_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("reset_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    await render_ui(
        update,
        context,
        "Сбросить заголовок, описание, картинку, блюр, источник превью, кнопки, "
        "эмодзи, цвет и тему форума до заводских значений?\n\n"
        "Подписки на стримеров не будут удалены.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Да, сбросить", callback_data=f"reset_confirm:{chat_id}"
                    ),
                    InlineKeyboardButton(
                        "Отмена", callback_data="menu:appearance"
                    ),
                ]
            ]
        ),
    )


async def reset_notification_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("reset_confirm:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    database.reset_notification_settings(chat_id)
    clear_wizard(context)
    await render_ui(
        update,
        context,
        "Настройки уведомлений сброшены до заводских. Подписки сохранены.",
        main_inline_keyboard(),
    )


async def select_preview_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    file_id = context.user_data.get("pending_preview_file_id")
    if not file_id or not query.data.startswith("preview_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /preview.")
        return

    try:
        chat_id = int(query.data.removeprefix("preview_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /preview.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.set_preview_file_id(chat_id, file_id)
    context.user_data.pop("pending_preview_file_id", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Картинка сохранена. Она будет использована в следующем уведомлении.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("В оформление", callback_data="menu:appearance")]]
        ),
    )


async def check_streams(
    application: Application,
    *,
    only_subscription_ids: set[int] | None = None,
) -> list[str]:
    database: Database = application.bot_data["database"]
    providers: StreamProviders = application.bot_data["providers"]
    subscriptions = database.get_all_subscriptions()
    results = []
    started_chats: set[int] = set()
    changed_chats: set[int] = set()
    check_scrape = (
        only_subscription_ids is not None
        or time.monotonic() - application.bot_data["last_scrape_check"]
        >= YOUTUBE_POLL_INTERVAL_SECONDS
    )
    check_slow_scrape = (
        only_subscription_ids is not None
        or time.monotonic() - application.bot_data["last_slow_scrape_check"]
        >= SLOW_SCRAPE_POLL_INTERVAL_SECONDS
    )

    for subscription in subscriptions:
        if (
            only_subscription_ids is not None
            and subscription["id"] not in only_subscription_ids
        ):
            continue
        if (
            subscription["platform"] in SLOW_SCRAPE_PLATFORMS
            and not check_slow_scrape
        ):
            continue
        if (
            subscription["platform"] in SCRAPE_PLATFORMS - SLOW_SCRAPE_PLATFORMS
            and not check_scrape
        ):
            continue

        try:
            stream = await fetch_live_stream(providers, subscription)
        except (RuntimeError, ValueError, httpx.HTTPError, KeyError) as error:
            logger.warning(
                "Проверка %s/%s не удалась: %s",
                subscription["platform"],
                subscription["channel_key"],
                error,
            )
            results.append(
                f"⚠ #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: ошибка API — {error}"
            )
            continue

        stream_id = stream.stream_id if stream else None
        if stream:
            results.append(
                f"🔴 #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: эфир найден — {stream.title}"
            )
        else:
            results.append(
                f"⚪ #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: активный эфир не найден"
            )

        if not subscription["initialized"]:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=stream_id,
            )
            logger.info(
                "Первичный статус %s/%s: %s",
                subscription["platform"],
                subscription["channel_key"],
                "в эфире" if stream else "офлайн",
            )
            continue

        if stream and stream_id != subscription["active_stream_id"]:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=stream_id,
            )
            started_chats.add(subscription["chat_id"])
            changed_chats.add(subscription["chat_id"])
        elif not stream and subscription["active_stream_id"] is not None:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=None,
            )
            changed_chats.add(subscription["chat_id"])

    if check_scrape:
        application.bot_data["last_scrape_check"] = time.monotonic()
    if check_slow_scrape:
        application.bot_data["last_slow_scrape_check"] = time.monotonic()

    pending_chats: set[int] = application.bot_data["pending_notification_chats"]
    for chat_id in started_chats:
        settings = database.get_notification_settings(chat_id)
        if settings and settings["notification_message_id"]:
            await send_or_edit_notification(application, chat_id)
        elif chat_id not in pending_chats:
            pending_chats.add(chat_id)
            application.job_queue.run_once(
                delayed_notification,
                when=COMBINE_DELAY_SECONDS,
                data=chat_id,
                name=f"combined-notification-{chat_id}",
            )

    for chat_id in changed_chats:
        if not any(
            subscription["active_stream_id"]
            for subscription in database.get_chat_subscriptions(chat_id)
        ):
            database.clear_notification_message(chat_id)

    return results


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_streams(context.application)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанная ошибка Telegram: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.delete_my_commands()
    await application.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    application.job_queue.run_repeating(
        scheduled_check,
        interval=FAST_POLL_INTERVAL_SECONDS,
        first=5,
        name="stream-status-check",
    )
    logger.info(
        "Проверка Twitch/Kick каждые %d секунд, публичных источников каждые %d/%d секунд",
        FAST_POLL_INTERVAL_SECONDS,
        YOUTUBE_POLL_INTERVAL_SECONDS,
        SLOW_SCRAPE_POLL_INTERVAL_SECONDS,
    )


async def post_shutdown(application: Application) -> None:
    providers: StreamProviders = application.bot_data["providers"]
    await providers.close()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        logger.warning("Twitch не настроен: добавь TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET")
    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        logger.warning("Kick не настроен: добавь KICK_CLIENT_ID и KICK_CLIENT_SECRET")

    database = Database(DB_PATH)
    providers = StreamProviders()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["database"] = database
    application.bot_data["providers"] = providers
    application.bot_data["pending_notification_chats"] = set()
    application.bot_data["last_scrape_check"] = 0.0
    application.bot_data["last_slow_scrape_check"] = 0.0

    application.add_error_handler(on_error)
    application.add_handler(
        ChatMemberHandler(
            track_connected_chat,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("subscriptions", subscriptions_command))
    application.add_handler(CommandHandler("appearance", appearance_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("template", template_command))
    application.add_handler(CommandHandler("preview", preview_command))
    application.add_handler(CommandHandler("topic", topic_command))
    application.add_handler(MessageHandler(filters.PHOTO, receive_preview_photo))
    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^(menu|add|subscription|remove|appearance):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(select_target_chat, pattern=r"^target_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_template_chat, pattern=r"^template_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_description_chat, pattern=r"^description_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_example_chat, pattern=r"^example_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_discord_chat, pattern=r"^discord_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(
            select_preview_clear_chat, pattern=r"^clear_preview_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            select_preview_source_chat, pattern=r"^preview_source_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(set_preview_platform, pattern=r"^preview_platform:")
    )
    application.add_handler(
        CallbackQueryHandler(select_emoji_chat, pattern=r"^emoji_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(show_button_settings, pattern=r"^button_settings_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_button_rename, pattern=r"^button_rename_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(begin_button_rename, pattern=r"^button_rename:")
    )
    application.add_handler(
        CallbackQueryHandler(select_emoji_platform, pattern=r"^emoji_platform:")
    )
    application.add_handler(
        CallbackQueryHandler(select_button_color_chat, pattern=r"^color_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(set_button_color, pattern=r"^color_set:")
    )
    application.add_handler(
        CallbackQueryHandler(
            select_individual_button_color_chat,
            pattern=r"^individual_colors_chat:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            select_individual_button_color, pattern=r"^individual_color:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            set_individual_button_color, pattern=r"^individual_color_set:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(select_custom_button_chat, pattern=r"^custom_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(begin_custom_button, pattern=r"^custom_add:")
    )
    application.add_handler(
        CallbackQueryHandler(delete_custom_button, pattern=r"^custom_delete:")
    )
    application.add_handler(
        CallbackQueryHandler(
            begin_custom_button_group_edit, pattern=r"^custom_group:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            begin_custom_button_emoji_edit, pattern=r"^custom_emoji:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            select_platform_group_chat, pattern=r"^platform_groups_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            begin_platform_group_edit, pattern=r"^subscription_group:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(toggle_preview_blur, pattern=r"^blur_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(
            ask_reset_notification_settings, pattern=r"^reset_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            reset_notification_settings, pattern=r"^reset_confirm:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(select_preview_chat, pattern=r"^preview_chat:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler)
    )

    logger.info("Бот уведомлений запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
