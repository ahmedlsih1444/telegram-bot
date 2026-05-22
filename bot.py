"""
Group Media Moderation Bot
- Admins reply to media with @bot احذف / @bot delete to remove it
- Super user (@hmdslih) bypasses all checks; his media is protected from others
- Auto-deletes messages with banned phrases (except from super user)
- Reports every deletion to the group owner via DM
- Alerts super user when someone tries to delete his media
- Runs an HTTP health server so Render free tier can host it 24/7
"""

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from telegram import Chat, ChatMember, ChatMemberAdministrator, ChatMemberOwner, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Configuration ─────────────────────────────────────────────────────────────

TOKEN        = "8779308980:AAFdyE1RkgPpwGamwWsLaSNeIYelVWREzC0"
SUPER        = "hmdslih"
TRIGGERS     = {"احذف", "delete"}
BANNED       = {"كسخت ايثار", "كسخت المدير"}
CACHE_TTL    = 60
CHATS_FILE   = "known_chats.json"
PORT         = int(os.environ.get("PORT", 8080))

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ── State ─────────────────────────────────────────────────────────────────────

_admin_cache: dict[int, tuple[frozenset, Optional[int], float]] = {}
_perm_cache:  dict[int, tuple[bool, float]] = {}
_known:       set[int] = set()
_super_id:    Optional[int] = None
_bot_name:    str = ""

# ── Health server (keeps Render web service alive) ────────────────────────────

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *_):
        pass

def _start_health_server():
    try:
        HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()
    except OSError:
        log.warning("health server port %d in use — skipping (ok on Replit)", PORT)

# ── Disk helpers ──────────────────────────────────────────────────────────────

def _load() -> set[int]:
    try:
        return set(json.loads(open(CHATS_FILE).read()))
    except Exception:
        return set()

def _save() -> None:
    try:
        open(CHATS_FILE, "w").write(json.dumps(list(_known)))
    except Exception:
        pass

# ── Pure checks ───────────────────────────────────────────────────────────────

def _is_super(username: Optional[str]) -> bool:
    return bool(username) and username.lstrip("@").lower() == SUPER.lower()

def _is_media(msg) -> bool:
    return bool(msg.photo or msg.video or msg.animation or msg.sticker or msg.video_note)

def _is_delete_cmd(msg) -> bool:
    text = (msg.text or msg.caption or "").strip()
    return text.lower() in TRIGGERS

def _has_banned(text: Optional[str]) -> bool:
    return bool(text) and any(phrase in text for phrase in BANNED)

# ── Async API helpers ─────────────────────────────────────────────────────────

async def _get_admins(bot, chat_id: int) -> tuple[frozenset, Optional[int]]:
    cached = _admin_cache.get(chat_id)
    if cached and time.monotonic() - cached[2] < CACHE_TTL:
        return cached[0], cached[1]
    try:
        members = await bot.get_chat_administrators(chat_id)
        ids     = frozenset(m.user.id for m in members)
        owner   = next((m.user.id for m in members if isinstance(m, ChatMemberOwner)), None)
    except TelegramError:
        ids, owner = frozenset(), None
    _admin_cache[chat_id] = (ids, owner, time.monotonic())
    return ids, owner

async def _bot_can_delete(bot, chat_id: int) -> bool:
    cached = _perm_cache.get(chat_id)
    if cached and time.monotonic() - cached[1] < CACHE_TTL:
        return cached[0]
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        ok = isinstance(me, ChatMemberOwner) or (
             isinstance(me, ChatMemberAdministrator) and bool(me.can_delete_messages))
    except TelegramError:
        ok = False
    _perm_cache[chat_id] = (ok, time.monotonic())
    return ok

async def _prewarm(bot, chat_id: int) -> None:
    await asyncio.gather(
        _get_admins(bot, chat_id),
        _bot_can_delete(bot, chat_id),
        return_exceptions=True,
    )

async def _delete(bot, chat_id: int, msg_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except BadRequest as e:
        s = str(e).lower()
        if "not found" not in s and "message_id_invalid" not in s:
            log.warning("delete failed (%s): %s", msg_id, e)
    except (Forbidden, TelegramError) as e:
        log.warning("delete failed (%s): %s", msg_id, e)

async def _dm(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    except (Forbidden, TelegramError):
        pass

async def _forward(bot, to_id: int, from_chat: int, msg_id: int) -> None:
    try:
        await bot.forward_message(chat_id=to_id, from_chat_id=from_chat, message_id=msg_id)
    except (Forbidden, TelegramError):
        pass

# ── Core actions ──────────────────────────────────────────────────────────────

async def _report_to_owner(bot, owner_id: int, chat, media_msg, cmd_msg, actor: str) -> None:
    group = chat.title or str(chat.id)
    await asyncio.gather(
        _dm(bot, owner_id,
            f"🗑 *Deletion Report*\n"
            f"📌 Group: {group}\n"
            f"👤 By: {actor}"),
        _forward(bot, owner_id, chat.id, media_msg.message_id),
        _forward(bot, owner_id, chat.id, cmd_msg.message_id),
    )

async def _alert_super_user(bot, chat, cmd_msg, actor: str) -> None:
    if not _super_id:
        return
    group = chat.title or str(chat.id)
    await asyncio.gather(
        _dm(bot, _super_id,
            f"⚠️ *Deletion Attempt Blocked*\n"
            f"📌 Group: {group}\n"
            f"👤 By: {actor}"),
        _forward(bot, _super_id, chat.id, cmd_msg.message_id),
    )
    log.info("super user alerted — attempt by %s", actor)

async def _no_permission_msg(bot, chat_id: int) -> None:
    await _dm(bot, chat_id,
        "⚠️ *Permission Required | صلاحية مطلوبة*\n\n"
        "🇬🇧 Grant me *Delete Messages* in admin settings.\n\n"
        "🇸🇦 منحني صلاحية *حذف الرسائل* من إعدادات المشرفين.")

# ── Handlers ──────────────────────────────────────────────────────────────────

async def on_start(app: Application) -> None:
    global _bot_name
    me = await app.bot.get_me()
    _bot_name = (me.username or "").lower()
    log.info("bot: @%s", _bot_name)
    if _known:
        log.info("pre-warming %d group(s)…", len(_known))
        await asyncio.gather(*(_prewarm(app.bot, c) for c in _known), return_exceptions=True)
    log.info("ready.")

async def on_my_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    r = update.my_chat_member
    if not r or r.chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return
    _admin_cache.pop(r.chat.id, None)
    _perm_cache.pop(r.chat.id, None)
    new = r.new_chat_member
    try:
        if new.status == ChatMember.MEMBER:
            await ctx.bot.send_message(
                chat_id=r.chat.id, parse_mode="Markdown",
                text=(
                    "👋 *مرحباً | Hello!*\n\n"
                    "🇸🇦 يرجى ترقيتي مشرفًا ومنحي صلاحية *حذف الرسائل*.\n\n"
                    "🇬🇧 Please make me admin with *Delete Messages* permission."
                ),
            )
        elif isinstance(new, ChatMemberAdministrator) and not new.can_delete_messages:
            await _no_permission_msg(ctx.bot, r.chat.id)
    except TelegramError:
        pass

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _super_id

    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not (msg and chat and user and chat.type in (Chat.GROUP, Chat.SUPERGROUP)):
        return

    uname    = (user.username or "").lower()
    is_super = _is_super(uname)

    if is_super and not _super_id:
        _super_id = user.id

    if chat.id not in _known:
        _known.add(chat.id)
        _save()
        asyncio.create_task(_prewarm(ctx.bot, chat.id))

    text  = msg.text or msg.caption or ""
    actor = f"@{user.username}" if user.username else f"ID {user.id}"

    if not is_super and _has_banned(text):
        can_del = await _bot_can_delete(ctx.bot, chat.id)
        if not can_del:
            await _no_permission_msg(ctx.bot, chat.id)
            return
        await _delete(ctx.bot, chat.id, msg.message_id)
        log.info("banned phrase deleted | %s | chat %s", actor, chat.id)
        return

    if not _is_delete_cmd(msg):
        return

    target = msg.reply_to_message
    if not target or not _is_media(target):
        return

    if _is_super(target.from_user.username if target.from_user else None) and not is_super:
        await _alert_super_user(ctx.bot, chat, msg, actor)
        return

    (admin_ids, owner_id), can_del = await asyncio.gather(
        _get_admins(ctx.bot, chat.id),
        _bot_can_delete(ctx.bot, chat.id),
    )

    if not is_super and user.id not in admin_ids:
        return

    if not can_del:
        await _no_permission_msg(ctx.bot, chat.id)
        return

    if owner_id:
        await _report_to_owner(ctx.bot, owner_id, chat, target, msg, actor)

    await asyncio.gather(
        _delete(ctx.bot, chat.id, target.message_id),
        _delete(ctx.bot, chat.id, msg.message_id),
    )
    log.info("deleted media=%s cmd=%s by %s", target.message_id, msg.message_id, actor)

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error: %s", ctx.error)

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _known
    _known = _load()

    threading.Thread(target=_start_health_server, daemon=True).start()
    log.info("health server on port %d", PORT)

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_start)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(ChatMemberHandler(on_my_status, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
