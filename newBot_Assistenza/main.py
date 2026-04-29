"""
Bot di Assistenza Telegram - traduzione da Rust (teloxide) a Python (python-telegram-bot v21)
"""
import asyncio
import logging
import os

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, ChatMember, ReactionTypeEmoji,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
import telegram.error

# ─── STATI FSM ────────────────────────────────────────────────────────────────
START, ASKING_SUPPORT, IN_SESSION = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
DB_URL = os.getenv("DB_URL")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

async def init_db() -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_banned BOOLEAN DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tickets (
                user_id    INTEGER PRIMARY KEY,
                thread_id  INTEGER NOT NULL,
                wait_msg_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS msg_map (
                source_chat_id INTEGER,
                source_msg_id  INTEGER,
                dest_chat_id   INTEGER,
                dest_msg_id    INTEGER,
                PRIMARY KEY (source_chat_id, source_msg_id)
            );
            CREATE TABLE IF NOT EXISTS user_system_msgs (
                user_id INTEGER PRIMARY KEY,
                msg_id  INTEGER
            );
        """)
        await db.commit()


async def createUser(user_id: int):
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "INSERT INTO users (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()


async def checkUser(user_id: int) -> bool:
    async with aiosqlite.connect(DB_URL) as db:
        async with db.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def delUser(user_id: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "DELETE FROM users WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_URL) as db:
        async with db.execute(
                "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            result = await cur.fetchone()
            return result is not None and result[0]


async def ban_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def unban_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def get_ticket(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT thread_id, wait_msg_id FROM tickets WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_ticket_by_thread(thread_id: int) -> dict | None:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT user_id FROM tickets WHERE thread_id = ?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def save_msg_map(src_chat: int, src_msg: int, dst_chat: int, dst_msg: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "INSERT OR REPLACE INTO msg_map VALUES (?, ?, ?, ?)",
            (src_chat, src_msg, dst_chat, dst_msg),
        )
        await db.commit()


async def get_msg_map(src_chat: int, src_msg: int) -> dict | None:
    async with aiosqlite.connect(DB_URL) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT dest_chat_id, dest_msg_id FROM msg_map "
                "WHERE source_chat_id = ? AND source_msg_id = ?",
                (src_chat, src_msg),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_system_msg(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_URL) as db:
        async with db.execute(
                "SELECT msg_id FROM user_system_msgs WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def save_system_msg(user_id: int, msg_id: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_system_msgs (user_id, msg_id) VALUES (?, ?)",
            (user_id, msg_id),
        )
        await db.commit()


async def delete_system_msg(user_id: int) -> None:
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "DELETE FROM user_system_msgs WHERE user_id = ?", (user_id,)
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITÀ
# ═══════════════════════════════════════════════════════════════════════════════

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        # if user_id == 1072604942:
        #     return True
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in (
            ChatMember.LEFT, ChatMember.BANNED
        )
    except telegram.error.TelegramError:
        return False


async def send_subscription_barrier(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    channel_link = f"{(await context.bot.getChat(CHANNEL_ID)).invite_link}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Iscriviti", url=channel_link)],
        [InlineKeyboardButton("🔄 Ho effettuato l'iscrizione", callback_data="check_sub")],
    ])
    await context.bot.send_message(
        chat_id,
        "✖️ <b>Per utilizzare il Bot devi essere iscritto al Canale del partito!</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def send_welcome_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    ticket = await get_ticket(chat_id)
    if not ticket:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Assistenza 🛠️", callback_data="assistenza")]
        ])
        messCustom = "<i>Richiedi assistenza cliccando il seguente pulsante.</i>"
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Sei già in sessione.", callback_data="nothing", style="primary")]
        ])
        messCustom = "<i>Per ricevere supporto, scrivi in chat il tuo problema!</i>"
    await context.bot.send_photo(
        chat_id,
        photo="logo.jpg",
        caption=(
            f"🦁 <b>Benvenuto nel Bot di Progresso Riformista!</b>\n"
            f"{messCustom}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER UTENTE — /start e messaggi generici
# ═══════════════════════════════════════════════════════════════════════════════

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gestisce /start con logica FSM per il messaggio di sistema pendente."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not await checkUser(user.id):
        await createUser(user.id)

    if await is_banned(user.id):
        return ConversationHandler.END

    # Cerca un messaggio di sistema pendente (secondo /start dopo iscrizione)
    pending_msg_id = await get_system_msg(user.id)
    if pending_msg_id is not None:
        # Cancella il messaggio "Ti sei iscritto! Digita /start"
        try:
            await context.bot.delete_message(chat_id, pending_msg_id)
        except telegram.error.TelegramError:
            pass
        # Cancella questo /start
        try:
            await update.message.delete()
        except telegram.error.TelegramError:
            pass
        # Pulisce il DB
        await delete_system_msg(user.id)
        await send_welcome_menu(context, chat_id)
        return START

    # Controlla se l'utente è iscritto al canale
    if not await check_subscription(context, user.id):
        await send_subscription_barrier(context, chat_id)
        return ConversationHandler.END

    await send_welcome_menu(context, chat_id)
    return START


async def generic_user_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Gestisce i messaggi utente fuori dallo stato AskingSupport.
    Se l'utente ha già un ticket aperto → forward al gruppo admin.
    Altrimenti → mostra il menu di benvenuto (previa verifica iscrizione).
    """
    user = update.effective_user
    chat_id = update.effective_chat.id

    if await is_banned(user.id):
        await update.message.reply_text(text="<b>❌ Sei stato Bannato!</b>", parse_mode='HTML')
        return ConversationHandler.END

    if not await check_subscription(context, user.id):
        await send_subscription_barrier(context, chat_id)
        return ConversationHandler.END

    ticket = await get_ticket(user.id)
    if ticket:
        await forward_to_admin(update, context)
        return IN_SESSION

    if context.user_data["state"] == ASKING_SUPPORT:
        await create_ticket_handler(update, context)
        return IN_SESSION

    await send_welcome_menu(context, chat_id)
    return START


async def in_session_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stato IN_SESSION: ogni messaggio utente viene inoltrato al gruppo admin."""
    user = update.effective_user
    if await is_banned(user.id):
        return IN_SESSION
    await forward_to_admin(update, context)
    return IN_SESSION


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER — TICKET (stato AskingSupport)
# ═══════════════════════════════════════════════════════════════════════════════

async def create_ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Il primo messaggio dopo aver premuto 'Assistenza' crea il topic nel gruppo admin."""
    user = update.effective_user

    if await is_banned(user.id):
        return ConversationHandler.END

    # Crea il topic nel gruppo admin
    topic = await context.bot.create_forum_topic(
        ADMIN_GROUP_ID,
        f"Assistenza: {user.first_name}",
        icon_color=0x6FB9F0,  # colore blu (opzionale)
    )
    thread_id = topic.message_thread_id

    # Salva il ticket nel DB
    async with aiosqlite.connect(DB_URL) as db:
        await db.execute(
            "INSERT OR REPLACE INTO tickets (user_id, thread_id, wait_msg_id) VALUES (?, ?, NULL)",
            (user.id, thread_id),
        )
        await db.commit()

    # Manda il riepilogo utente con pulsante "Banna"
    ban_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Banna Utente", callback_data=f"ban:{user.id}")]
    ])
    username = f"@{user.username}" if user.username else "N/A"
    await context.bot.send_message(
        ADMIN_GROUP_ID,
        f"• <b>ID:</b> <code>{user.id}</code>\n"
        f"• <b>Nome:</b> {user.first_name}\n"
        f"• <b>Username:</b> {username}",
        message_thread_id=thread_id,
        parse_mode=ParseMode.HTML,
        reply_markup=ban_kb,
    )

    # Inoltra il primo messaggio
    copied = await context.bot.copy_message(
        ADMIN_GROUP_ID,
        update.effective_chat.id,
        update.message.message_id,
        message_thread_id=thread_id,
    )
    await save_msg_map(
        update.effective_chat.id,
        update.message.message_id,
        ADMIN_GROUP_ID,
        copied.message_id,
    )

    return IN_SESSION


# ═══════════════════════════════════════════════════════════════════════════════
# FORWARD utente → admin
# ═══════════════════════════════════════════════════════════════════════════════

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    ticket = await get_ticket(user_id)
    if not ticket:
        return
    thread_id = ticket["thread_id"]
    copied = await context.bot.copy_message(
        ADMIN_GROUP_ID,
        update.effective_chat.id,
        update.message.message_id,
        message_thread_id=thread_id,
    )
    await save_msg_map(
        update.effective_chat.id,
        update.message.message_id,
        ADMIN_GROUP_ID,
        copied.message_id,
    )
    await update.message.set_reaction(reaction=[ReactionTypeEmoji('👍')])
    await asyncio.sleep(2)
    await update.message.set_reaction()


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER ADMIN — risposte nel gruppo
# ═══════════════════════════════════════════════════════════════════════════════

async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce le risposte degli admin nel gruppo (topic) e le inoltra all'utente."""
    msg = update.message
    if not msg or msg.message_thread_id is None:
        return

    thread_id = msg.message_thread_id
    ticket = await get_ticket_by_thread(thread_id)
    if not ticket:
        return
    if msg.from_user.is_bot:
        return

    uid = ticket["user_id"]

    # Comando /del: cancella un messaggio specchiato
    if msg.text in ["/del", ".del", "/delete", ".delete"] and msg.reply_to_message:
        reply = msg.reply_to_message
        m = await get_msg_map(msg.chat.id, reply.message_id)
        if m:
            try:
                await context.bot.delete_message(m["dest_chat_id"], m["dest_msg_id"])
            except telegram.error.TelegramError:
                pass
            try:
                await context.bot.delete_message(msg.chat.id, reply.message_id)
            except telegram.error.TelegramError:
                pass
            try:
                await context.bot.delete_message(msg.chat.id, msg.message_id)
            except telegram.error.TelegramError:
                pass
        return

    # Inoltro normale admin → utente
    copied = await context.bot.copy_message(uid, msg.chat.id, msg.message_id)
    await save_msg_map(msg.chat.id, msg.message_id, uid, copied.message_id)
    await update.message.set_reaction(reaction=[ReactionTypeEmoji('👍')])
    await asyncio.sleep(2)
    await update.message.set_reaction()


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER — messaggi modificati (sync bidirezionale)
# ═══════════════════════════════════════════════════════════════════════════════

async def edit_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg:
        return

    mapping = await get_msg_map(msg.chat.id, msg.message_id)
    if not mapping:
        return

    dst_chat = mapping["dest_chat_id"]
    dst_msg = mapping["dest_msg_id"]

    try:
        if msg.text:
            await context.bot.edit_message_text(msg.text, chat_id=dst_chat, message_id=dst_msg)
        elif msg.caption:
            await context.bot.edit_message_caption(chat_id=dst_chat, message_id=dst_msg, caption=msg.caption)
    except telegram.error.TelegramError as e:
        log.warning("edit_message_handler: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER — callback query (pulsanti inline)
# ═══════════════════════════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # ── check_sub ──────────────────────────────────────────────────────────────
    if data == "check_sub":
        if await check_subscription(context, user_id):
            try:
                await query.message.delete()
            except telegram.error.TelegramError:
                pass
            sent = await context.bot.send_message(
                user_id,
                "✅ <b>Ti sei iscritto!</b>\n<i>Digita /start per iniziare.</i>",
                parse_mode=ParseMode.HTML,
            )
            await save_system_msg(user_id, sent.message_id)
        else:
            await query.answer("❌ Non risulti ancora iscritto al canale.", show_alert=True)
        return

    # ── assistenza ─────────────────────────────────────────────────────────────
    if data == "assistenza":
        if await is_banned(user_id):
            return
        try:
            await query.message.delete()
        except telegram.error.TelegramError:
            pass
        await context.bot.send_message(
            user_id,
            "👤 <b>Sei in contatto con la Segreteria.</b>\n<i>Scrivi e noi ti risponderemo!</i>",
            parse_mode=ParseMode.HTML,
        )
        # Imposta lo stato AskingSupport (via user_data)
        context.user_data["state"] = ASKING_SUPPORT
        return

    # ── ban:<id> ───────────────────────────────────────────────────────────────
    if data.startswith("ban:"):
        target_id = int(data.split(":", 1)[1])
        await ban_user(target_id)
        await query.answer("❌ Utente bannato correttamente.")
        # Rimuove i pulsanti inline dal messaggio corrente
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        except telegram.error.TelegramError:
            pass
        ticket = await get_ticket(target_id)
        try:
            await context.bot.send_message(chat_id=target_id, text="<b>❌ Sei stato Bannato!</b>", parse_mode='HTML')
        except telegram.error.TelegramError:
            pass
        if ticket:
            tid = ticket["thread_id"]
            unban_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Sbanna", callback_data=f"unban:{target_id}")]
            ])
            await context.bot.send_message(
                ADMIN_GROUP_ID,
                f"🚫 L'utente {target_id} è stato bannato.",
                message_thread_id=tid,
                reply_markup=unban_kb,
            )
        return

    # ── unban:<id> ─────────────────────────────────────────────────────────────
    if data.startswith("unban:"):
        target_id = int(data.split(":", 1)[1])
        await unban_user(target_id)
        await query.answer("Utente riabilitato.")
        ban_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Banna Utente", callback_data=f"ban:{target_id}")]
        ])
        try:
            await context.bot.send_message(chat_id=target_id, text="<b>✅ Sei stato unBannato!</b>", parse_mode='HTML')
        except telegram.error.TelegramError:
            pass
        try:
            await query.edit_message_text(
                f"✅ L'utente {target_id} è stato riabilitato.",
                reply_markup=ban_kb,
            )
        except telegram.error.TelegramError:
            pass
        return
    # Broadcasting System - werfrag
    if data.startswith("sendPost:"):
        if (await update.effective_chat.get_member(update.effective_user.id)).status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            message_id = int(data.split(':', 1)[1])

            async with aiosqlite.connect(DB_URL) as db:
                async with db.execute(
                        "SELECT user_id FROM users",
                ) as cur:
                    row = await cur.fetchall()

            success = 0
            danger = 0
            for idx in row:
                try:
                    await context.bot.copy_message(message_id=message_id, from_chat_id=update.effective_chat.id, chat_id=idx[0])
                    success += 1
                except telegram.error.RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    continue
                except telegram.error.TelegramError:
                    danger += 1
                    if not (await is_banned(idx[0])):
                        await delUser(idx[0])

            await query.edit_message_reply_markup()
            message = (f"<b>Messaggio inviato correttamente.</b>\n\n"
                       f"<b>🤓 Nerd Statistic</b>\n\n"
                       f"<i>✅ Success:</i> <code>{success}</code>\n"
                       f"<i>❌ Error (Bot Blocked):</i> <code>{danger}</code>")
            await query.edit_message_text(message, parse_mode='HTML')
        else:
            await query.answer(text="❌ Errore : Solo gli amministratori possono fare questo comando.")
        return
    if data == "refusePost":
        if (await update.effective_chat.get_member(update.effective_user.id)).status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:

            message = f"<b>Operazione annullata.</b>"
            await query.edit_message_reply_markup()
            await query.edit_message_text(message, parse_mode='HTML')
        else:
            await query.answer(text="❌ Errore : Solo gli amministratori possono fare questo comando.")
        return


# =======================
# BROADCAST SYSTEM - werfrag
# =======================
async def onBroadcastUser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == ADMIN_GROUP_ID:
        if (await update.effective_chat.get_member(update.effective_user.id)).status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            if len(context.args) > 0:
                messageRaw = update.message.text_html.split(" ", 1)[1]
                messageRaw = f"<b>📣 BROADCAST INTERNO</b>\n\n{messageRaw}"

                messR = await update.message.reply_text(text=messageRaw, parse_mode='HTML')
                message = (f"<b>📣 ANTEPRIMA BROADCAST INTERNO</b>\n"
                           f"<i>È stata mandata l'anteprima dell'annuncio richiesto.</i>\n"
                           f"<b>MessageID »</b> <i>{messR.message_id}</i>")
                buttons = InlineKeyboardMarkup([[InlineKeyboardButton(text="✅ Approva",
                                                                      callback_data=f"sendPost:{messR.message_id}",
                                                                      style="success"),
                                                 InlineKeyboardButton(text="❌ Rifiuta", callback_data="refusePost",
                                                                      style="danger")]])
                await messR.reply_text(text=message, parse_mode='HTML', reply_to_message_id=messR.message_id,
                                       reply_markup=buttons)

                try:
                    await update.message.delete()
                except telegram.error.TelegramError:
                    pass
            else:
                await update.message.reply_text(text="<b><i>❌ Errore</i> : Inserisci un messaggio da annunciare.</b>",
                                                parse_mode='HTML')
        else:
            await update.message.reply_text(
                text="<b><i>❌ Errore</i> : Solo gli amministratori possono fare questo comando.</b>", parse_mode='HTML')


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application) -> None:
    await init_db()
    log.info("Database inizializzato.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Conversation handler per gli utenti (solo chat private) ────────────────
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_handler),
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND,
                generic_user_message_handler,
                ),
        ],
        states={
            START: [
                CommandHandler("start", start_handler),
                MessageHandler(
                    filters.ChatType.PRIVATE & ~filters.COMMAND,
                    generic_user_message_handler,
                    ),
            ],
            ASKING_SUPPORT: [
                MessageHandler(
                    filters.ChatType.PRIVATE & ~filters.COMMAND,
                    create_ticket_handler,
                    ),
            ],
            IN_SESSION: [
                MessageHandler(
                    filters.ChatType.PRIVATE & ~filters.COMMAND,
                    in_session_message_handler,
                    ),
            ],
        },
        fallbacks=[CommandHandler("start", start_handler)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("broadcast", onBroadcastUser))

    # ── Callback query (pulsanti inline — funziona in qualsiasi chat) ──────────
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ── Risposte degli admin nel gruppo ────────────────────────────────────────
    app.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_GROUP_ID) & filters.IS_TOPIC_MESSAGE,
            admin_reply_handler,
            )
    )

    # ── Messaggi modificati (sincronizzazione bidirezionale) ───────────────────
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE, edit_message_handler)
    )

    log.info("Bot avviato. In attesa di messaggi...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
