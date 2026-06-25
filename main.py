"""
XVIP Hybrid Telegram Bot + Userbot
====================================
Architecture:
  - Bot Client   : Telethon bot-mode  — Admin commands
  - Userbot      : Telethon StringSession — source monitoring & pipeline delivery
  - DB           : Supabase REST — stores dynamic channel lists
  - Session      : STRING_SESSION env var (mandatory) — set manually before deploy

Required env vars:
  API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS,
  SUPABASE_URL, SUPABASE_KEY,
  STRING_SESSION,
  TERA_CONVERTER_BOT, DISK_CONVERTER_BOT,
  TERA_DESTINATION, DISK_DESTINATION

Optional env vars (one-time seed — DB takes over after first save):
  TERA_SOURCE_CHANNELS, DISK_SOURCE_CHANNELS
"""

import asyncio
import functools
import json
import logging
import os
import re
import sys
from typing import Optional

import httpx
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("xvip")


# ─────────────────────────────────────────────
# ENV CONFIG
# ─────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _parse_ids(raw: str) -> list[int]:
    result = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


def _clean_username(u: str) -> str:
    return u.strip().lstrip("@").lower()


API_ID             = int(_require("API_ID"))
API_HASH           = _require("API_HASH")
BOT_TOKEN          = _require("BOT_TOKEN")
ADMIN_IDS          = set(_parse_ids(_require("ADMIN_IDS")))
SUPABASE_URL       = _require("SUPABASE_URL").rstrip("/")
SUPABASE_KEY       = _require("SUPABASE_KEY")
STRING_SESSION     = _require("STRING_SESSION")

TERA_CONVERTER_BOT = _clean_username(_require("TERA_CONVERTER_BOT"))
DISK_CONVERTER_BOT = _clean_username(_require("DISK_CONVERTER_BOT"))
TERA_DESTINATION   = _clean_username(_require("TERA_DESTINATION"))
DISK_DESTINATION   = _clean_username(_require("DISK_DESTINATION"))

# Seed values from env (used only if DB has no data yet)
_TERA_SEED = _parse_ids(os.environ.get("TERA_SOURCE_CHANNELS", ""))
_DISK_SEED = _parse_ids(os.environ.get("DISK_SOURCE_CHANNELS", ""))


# ─────────────────────────────────────────────
# DYNAMIC CHANNEL STATE (mutated at runtime)
# ─────────────────────────────────────────────
TERA_SOURCE_IDS: list[int] = list(_TERA_SEED)
DISK_SOURCE_IDS: list[int] = list(_DISK_SEED)


def all_source_ids() -> set[int]:
    return set(TERA_SOURCE_IDS + DISK_SOURCE_IDS)


# ─────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────
_SB_BASE = SUPABASE_URL
if _SB_BASE.endswith("/rest/v1"):
    _SB_BASE = _SB_BASE[: -len("/rest/v1")]

_SB_TABLE    = f"{_SB_BASE}/rest/v1/bot_config"
_TERA_CH_KEY = "tera_source_channels"
_DISK_CH_KEY = "disk_source_channels"

_SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


async def _sb_get(key: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                _SB_TABLE,
                headers=_SB_HEADERS,
                params={"key_name": f"eq.{key}", "select": "key_value"},
            )
        if r.status_code == 200:
            rows = r.json()
            if rows:
                return rows[0]["key_value"]
        else:
            log.warning("Supabase GET %s → HTTP %s", key, r.status_code)
    except Exception as exc:
        log.warning("Supabase GET failed for '%s': %s", key, exc)
    return None


async def _sb_upsert(key: str, value: str) -> bool:
    try:
        headers = {**_SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(_SB_TABLE, headers=headers, json={"key_name": key, "key_value": value})
        if r.status_code in (200, 201, 204):
            return True
        log.warning("Supabase UPSERT %s → HTTP %s", key, r.status_code)
    except Exception as exc:
        log.warning("Supabase UPSERT failed for '%s': %s", key, exc)
    return False


async def sb_load_channels() -> None:
    """Load channel lists from Supabase on startup. Falls back to env seed."""
    global TERA_SOURCE_IDS, DISK_SOURCE_IDS

    tera_raw = await _sb_get(_TERA_CH_KEY)
    if tera_raw:
        try:
            TERA_SOURCE_IDS = json.loads(tera_raw)
            log.info("Tera channels loaded from DB: %s", TERA_SOURCE_IDS)
        except Exception:
            log.warning("Tera DB parse failed — using env seed.")
    elif _TERA_SEED:
        await _sb_upsert(_TERA_CH_KEY, json.dumps(_TERA_SEED))
        TERA_SOURCE_IDS = list(_TERA_SEED)
        log.info("Tera seed saved to DB: %s", TERA_SOURCE_IDS)

    disk_raw = await _sb_get(_DISK_CH_KEY)
    if disk_raw:
        try:
            DISK_SOURCE_IDS = json.loads(disk_raw)
            log.info("Disk channels loaded from DB: %s", DISK_SOURCE_IDS)
        except Exception:
            log.warning("Disk DB parse failed — using env seed.")
    elif _DISK_SEED:
        await _sb_upsert(_DISK_CH_KEY, json.dumps(_DISK_SEED))
        DISK_SOURCE_IDS = list(_DISK_SEED)
        log.info("Disk seed saved to DB: %s", DISK_SOURCE_IDS)


async def sb_save_tera() -> bool:
    return await _sb_upsert(_TERA_CH_KEY, json.dumps(TERA_SOURCE_IDS))


async def sb_save_disk() -> bool:
    return await _sb_upsert(_DISK_CH_KEY, json.dumps(DISK_SOURCE_IDS))


# ─────────────────────────────────────────────
# GLOBAL CLIENTS
# ─────────────────────────────────────────────
bot_client: TelegramClient = TelegramClient("bot_session", API_ID, API_HASH)
userbot: Optional[TelegramClient] = None
userbot_active: bool = False

# Track registered userbot handlers for clean removal on reload
_userbot_handlers: list = []

# Dedup: converter reply message IDs already processed
_seen_converter_msgs: set[int] = set()
_SEEN_MAX = 500


# ─────────────────────────────────────────────
# BOT MENU
# ─────────────────────────────────────────────
BOT_COMMANDS = [
    BotCommand(command="start",       description="Bot status aur available commands"),
    BotCommand(command="status",      description="Userbot connected hai ya nahi"),
    BotCommand(command="tera_add",    description="Tera source channel add karo"),
    BotCommand(command="tera_remove", description="Tera source channel remove karo"),
    BotCommand(command="tera_list",   description="Tera source channels ki list"),
    BotCommand(command="disk_add",    description="Disk source channel add karo"),
    BotCommand(command="disk_remove", description="Disk source channel remove karo"),
    BotCommand(command="disk_list",   description="Disk source channels ki list"),
    BotCommand(command="report",      description="Sab channels ka live tracking status"),
]


async def set_bot_menu() -> None:
    try:
        await bot_client(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=BOT_COMMANDS,
        ))
        log.info("Bot menu set.")
    except Exception as exc:
        log.warning("Bot menu set failed: %s", exc)


# ─────────────────────────────────────────────
# USERBOT: RELOAD HANDLERS
# ─────────────────────────────────────────────
async def reload_userbot_handlers(notify_admin_id: Optional[int] = None) -> None:
    """Remove tracked userbot handlers and re-attach with current channel lists."""
    global _userbot_handlers, _seen_converter_msgs
    if not userbot or not userbot_active:
        log.warning("reload called but userbot not active.")
        return

    # Remove only our registered handlers (not all handlers)
    for callback, event in _userbot_handlers:
        userbot.remove_event_handler(callback, event)
    _userbot_handlers.clear()
    _seen_converter_msgs.clear()  # reset dedup on reload

    attach_userbot_handlers(userbot)
    log.info("Userbot handlers reloaded. Tera=%s Disk=%s", TERA_SOURCE_IDS, DISK_SOURCE_IDS)

    if notify_admin_id:
        try:
            await bot_client.send_message(
                notify_admin_id,
                "🚀 **Tracking Start Successfully with updated channels!**\n\n"
                f"📡 Tera: {TERA_SOURCE_IDS or 'None'}\n"
                f"💾 Disk: {DISK_SOURCE_IDS or 'None'}",
            )
        except Exception as exc:
            log.warning("Admin notify failed: %s", exc)


async def _save_and_reload(pipeline: str, admin_id: int) -> None:
    ok = await sb_save_tera() if pipeline == "tera" else await sb_save_disk()
    if not ok:
        await bot_client.send_message(admin_id, "⚠️ DB save failed. Changes lost on restart.")
    await reload_userbot_handlers(notify_admin_id=admin_id)


# ─────────────────────────────────────────────
# USERBOT: CHANNEL ACCESS CHECK
# ─────────────────────────────────────────────
async def check_channel_access(ub: TelegramClient, channel_id: int) -> tuple[bool, str]:
    try:
        entity = await ub.get_entity(channel_id)
        name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(channel_id)
        return True, name
    except errors.ChannelPrivateError:
        return False, "Private/Banned"
    except Exception as exc:
        return False, f"Error: {exc}"


# ─────────────────────────────────────────────
# USERBOT: ATTACH HANDLERS
# ─────────────────────────────────────────────
def attach_userbot_handlers(ub: TelegramClient) -> None:
    global _userbot_handlers
    tera_ids = list(TERA_SOURCE_IDS)
    disk_ids = list(DISK_SOURCE_IDS)
    watch_ids = list(set(tera_ids + disk_ids))

    # ── Source channel monitor ────────────────────────────────────────────────
    if watch_ids:
        src_event = events.NewMessage(chats=watch_ids)

        async def on_source_message(event):
            msg     = event.message
            chat_id = event.chat_id

            has_media = bool(msg.photo) or (
                msg.document is not None
                and msg.document.mime_type is not None
                and (
                    msg.document.mime_type.startswith("video/")
                    or msg.document.mime_type.startswith("image/")
                )
            )
            if not has_media:
                return

            caption = (msg.message or "").lower()

            if chat_id in tera_ids and "tera" in caption:
                log.info("Tera match from %s → @%s", chat_id, TERA_CONVERTER_BOT)
                await safe_send(ub, TERA_CONVERTER_BOT, file=msg.media, message=msg.message or "")

            elif chat_id in disk_ids and "disk" in caption:
                log.info("Disk match from %s → @%s", chat_id, DISK_CONVERTER_BOT)
                await safe_send(ub, DISK_CONVERTER_BOT, file=msg.media, message=msg.message or "")

        ub.add_event_handler(on_source_message, src_event)
        _userbot_handlers.append((on_source_message, src_event))
    else:
        log.warning("No source channels — source handler skipped.")

    # ── Converter reply handler ───────────────────────────────────────────────
    conv_event = events.NewMessage(from_users=[TERA_CONVERTER_BOT, DISK_CONVERTER_BOT])

    async def on_converter_reply(event):
        global _seen_converter_msgs
        msg    = event.message

        # DEDUP: skip if already processed this message
        if msg.id in _seen_converter_msgs:
            log.info("Converter reply msg_id=%s already processed — skipping duplicate.", msg.id)
            return
        _seen_converter_msgs.add(msg.id)
        if len(_seen_converter_msgs) > _SEEN_MAX:
            # Keep only the latest 250
            _seen_converter_msgs = set(list(_seen_converter_msgs)[-250:])

        sender = event.sender
        sender_username = ""
        if sender and getattr(sender, "username", None):
            sender_username = _clean_username(sender.username)

        if sender_username == TERA_CONVERTER_BOT:
            dest, label, keyword = TERA_DESTINATION, "Tera", "tera"
        elif sender_username == DISK_CONVERTER_BOT:
            dest, label, keyword = DISK_DESTINATION, "Disk", "disk"
        else:
            return

        has_media = bool(msg.photo) or (
            msg.document is not None
            and msg.document.mime_type is not None
            and (
                msg.document.mime_type.startswith("video/")
                or msg.document.mime_type.startswith("image/")
            )
        )
        if not has_media:
            log.info("%s converter reply — no media, skipped.", label)
            return

        raw_text = (msg.message or "").lower()
        all_urls = []

        if msg.entities:
            for ent in msg.entities:
                url_attr = getattr(ent, "url", None)
                if url_attr:
                    all_urls.append(url_attr.lower())
                else:
                    chunk = raw_text[ent.offset: ent.offset + ent.length]
                    if chunk.startswith("http"):
                        all_urls.append(chunk)

        if msg.reply_markup:
            for row in getattr(msg.reply_markup, "rows", []):
                for btn in getattr(row, "buttons", []):
                    btn_url = getattr(btn, "url", None)
                    if btn_url:
                        all_urls.append(btn_url.lower())

        # Regex fallback for bold-Unicode formatted text
        all_urls.extend(re.findall(r"https?://\S+", raw_text))
        all_urls = list(set(all_urls))

        if not any(keyword in url for url in all_urls):
            log.info("%s reply — keyword '%s' not in URLs: %s", label, keyword, all_urls)
            return

        log.info("%s → sending to @%s", label, dest)
        await safe_send(ub, dest, file=msg.media, message=msg.message or "")

    ub.add_event_handler(on_converter_reply, conv_event)
    _userbot_handlers.append((on_converter_reply, conv_event))

    # ── Real-time channel deletion/ban alert ──────────────────────────────────
    chat_event = events.ChatAction()

    async def on_chat_action(event):
        try:
            chat_id = event.chat_id
            if chat_id not in all_source_ids():
                return

            action     = event.action_message
            if action is None:
                return

            action_type = type(action).__name__.lower()
            signals     = ["channeldelete", "chatdelete", "userban", "kickedout"]
            if not any(s in action_type for s in signals):
                return

            category = "Tera" if chat_id in TERA_SOURCE_IDS else "Disk"
            alert = (
                f"⚠️ **Alert: Tracking Stopped! Source Channel deleted or inaccessible.**\n"
                f"🔹 Category: {category}\n"
                f"🔹 Channel ID: `{chat_id}`"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await bot_client.send_message(admin_id, alert)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("on_chat_action error: %s", exc)

    ub.add_event_handler(on_chat_action, chat_event)
    _userbot_handlers.append((on_chat_action, chat_event))


# ─────────────────────────────────────────────
# START USERBOT
# ─────────────────────────────────────────────
async def start_userbot() -> bool:
    global userbot, userbot_active, _userbot_handlers, _seen_converter_msgs

    log.info("Starting userbot...")
    _userbot_handlers.clear()
    _seen_converter_msgs.clear()
    ub = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    try:
        await ub.start()
        if not await ub.is_user_authorized():
            log.error("STRING_SESSION is invalid or expired.")
            return False

        me = await ub.get_me()
        log.info("Userbot connected as: %s (id=%s)", me.username or me.first_name, me.id)

        attach_userbot_handlers(ub)
        userbot        = ub
        userbot_active = True
        return True

    except Exception as exc:
        log.exception("Userbot start failed: %s", exc)
        return False


# ─────────────────────────────────────────────
# SAFE SEND
# ─────────────────────────────────────────────
async def safe_send(client: TelegramClient, target: str, message: str = "", file=None):
    for attempt in range(3):
        try:
            if file:
                return await client.send_message(target, message, file=file)
            if message.strip():
                return await client.send_message(target, message)
            return None
        except errors.FloodWaitError as e:
            log.warning("FloodWait %ds (attempt %d/3)", e.seconds, attempt + 1)
            await asyncio.sleep(e.seconds + 2)
        except errors.UserIsBlockedError:
            log.error("Blocked by %s — skipping.", target)
            return None
        except Exception as exc:
            log.exception("safe_send → %s: %s", target, exc)
            return None
    return None


# ─────────────────────────────────────────────
# ADMIN DECORATOR
# ─────────────────────────────────────────────
def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            return
        await handler(event)
    return wrapper


# ─────────────────────────────────────────────
# ADMIN BOT HANDLERS
# ─────────────────────────────────────────────
def register_bot_handlers() -> None:

    # /start
    @bot_client.on(events.NewMessage(pattern=r"^/start$"))
    @admin_only
    async def cmd_start(event):
        status = "🟢 Connected" if userbot_active else "🔴 Offline"
        await event.respond(
            f"**XVIP Hybrid Bot**\n\n"
            f"Userbot: {status}\n\n"
            f"**Channel Management:**\n"
            f"`/tera_add <id>` — Tera source add\n"
            f"`/tera_remove <id>` — Tera source remove\n"
            f"`/tera_list` — Tera sources list\n"
            f"`/disk_add <id>` — Disk source add\n"
            f"`/disk_remove <id>` — Disk source remove\n"
            f"`/disk_list` — Disk sources list\n\n"
            f"**Reports:**\n"
            f"`/status` — Userbot info\n"
            f"`/report` — Live tracking status\n"
        )

    # /status
    @bot_client.on(events.NewMessage(pattern=r"^/status$"))
    @admin_only
    async def cmd_status(event):
        if userbot_active and userbot:
            try:
                me   = await userbot.get_me()
                name = me.username or me.first_name or str(me.id)
                await event.respond(
                    f"🟢 **Userbot Connected**\n"
                    f"Account: @{name}\n"
                    f"📡 Tera channels: {len(TERA_SOURCE_IDS)}\n"
                    f"💾 Disk channels: {len(DISK_SOURCE_IDS)}"
                )
            except Exception:
                await event.respond("🟡 Userbot started but profile fetch failed.")
        else:
            await event.respond(
                "🔴 **Userbot Offline**\n"
                "Check `STRING_SESSION` env var aur redeploy karo."
            )

    # /tera_add
    @bot_client.on(events.NewMessage(pattern=r"^/tera_add\s+(-?\d+)$"))
    @admin_only
    async def cmd_tera_add(event):
        ch_id = int(event.pattern_match.group(1))
        if ch_id in TERA_SOURCE_IDS:
            await event.respond(f"⚠️ `{ch_id}` already in Tera list.")
            return
        TERA_SOURCE_IDS.append(ch_id)
        await event.respond(f"✅ Added `{ch_id}` to Tera.\nSaving & reloading...")
        await _save_and_reload("tera", event.sender_id)

    # /tera_remove
    @bot_client.on(events.NewMessage(pattern=r"^/tera_remove\s+(-?\d+)$"))
    @admin_only
    async def cmd_tera_remove(event):
        ch_id = int(event.pattern_match.group(1))
        if ch_id not in TERA_SOURCE_IDS:
            await event.respond(f"⚠️ `{ch_id}` not found in Tera list.")
            return
        TERA_SOURCE_IDS.remove(ch_id)
        await event.respond(f"🗑 Removed `{ch_id}` from Tera.\nSaving & reloading...")
        await _save_and_reload("tera", event.sender_id)

    # /tera_list
    @bot_client.on(events.NewMessage(pattern=r"^/tera_list$"))
    @admin_only
    async def cmd_tera_list(event):
        if not TERA_SOURCE_IDS:
            await event.respond("📭 Tera list is empty.")
            return
        lines = "\n".join(f"• `{ch}`" for ch in TERA_SOURCE_IDS)
        await event.respond(f"📡 **Tera Source Channels ({len(TERA_SOURCE_IDS)}):**\n{lines}")

    # /disk_add
    @bot_client.on(events.NewMessage(pattern=r"^/disk_add\s+(-?\d+)$"))
    @admin_only
    async def cmd_disk_add(event):
        ch_id = int(event.pattern_match.group(1))
        if ch_id in DISK_SOURCE_IDS:
            await event.respond(f"⚠️ `{ch_id}` already in Disk list.")
            return
        DISK_SOURCE_IDS.append(ch_id)
        await event.respond(f"✅ Added `{ch_id}` to Disk.\nSaving & reloading...")
        await _save_and_reload("disk", event.sender_id)

    # /disk_remove
    @bot_client.on(events.NewMessage(pattern=r"^/disk_remove\s+(-?\d+)$"))
    @admin_only
    async def cmd_disk_remove(event):
        ch_id = int(event.pattern_match.group(1))
        if ch_id not in DISK_SOURCE_IDS:
            await event.respond(f"⚠️ `{ch_id}` not found in Disk list.")
            return
        DISK_SOURCE_IDS.remove(ch_id)
        await event.respond(f"🗑 Removed `{ch_id}` from Disk.\nSaving & reloading...")
        await _save_and_reload("disk", event.sender_id)

    # /disk_list
    @bot_client.on(events.NewMessage(pattern=r"^/disk_list$"))
    @admin_only
    async def cmd_disk_list(event):
        if not DISK_SOURCE_IDS:
            await event.respond("📭 Disk list is empty.")
            return
        lines = "\n".join(f"• `{ch}`" for ch in DISK_SOURCE_IDS)
        await event.respond(f"💾 **Disk Source Channels ({len(DISK_SOURCE_IDS)}):**\n{lines}")

    # /report
    @bot_client.on(events.NewMessage(pattern=r"^/report$"))
    @admin_only
    async def cmd_report(event):
        if not userbot_active or not userbot:
            await event.respond("🔴 Userbot offline — channel check possible nahi.\nCheck `STRING_SESSION` env var.")
            return

        await event.respond("🔍 Checking all source channels... please wait.")

        async def check_list(ids: list[int]) -> list[str]:
            lines = []
            for ch_id in ids:
                ok, name = await check_channel_access(userbot, ch_id)
                lines.append(f"{'✅' if ok else '❌'} `{ch_id}` — {name}")
            return lines

        tera_lines = await check_list(TERA_SOURCE_IDS)
        disk_lines = await check_list(DISK_SOURCE_IDS)

        await event.respond(
            f"📊 **Live Tracking Report**\n\n"
            f"📡 **Tera ({len(TERA_SOURCE_IDS)}):**\n"
            f"{chr(10).join(tera_lines) or '_None configured_'}\n\n"
            f"💾 **Disk ({len(DISK_SOURCE_IDS)}):**\n"
            f"{chr(10).join(disk_lines) or '_None configured_'}\n\n"
            f"✅ = accessible  ❌ = deleted/banned"
        )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    log.info("Booting XVIP...")

    await bot_client.start(bot_token=BOT_TOKEN)
    log.info("Admin bot started.")

    await set_bot_menu()
    await sb_load_channels()

    register_bot_handlers()

    success = await start_userbot()
    if not success:
        log.error("Userbot failed to start. Check STRING_SESSION env var.")
        # Bot stays alive so admin can see /status
    
    log.info("Running.")
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
