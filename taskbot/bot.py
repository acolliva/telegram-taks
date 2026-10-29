"""Telegram handlers. Private chats only; every non-command message is a new task."""

from __future__ import annotations

import logging
import tempfile
import time
from datetime import timedelta
from html import escape
from pathlib import Path

from telegram import BotCommand, InputMediaPhoto, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import render
from .config import Config
from .db import DB, Task, now_utc
from .text import clean_transcript, parse_tasks, split_title
from .transcribe import LocalWhisper, NoTranscriber, Transcriber, make_transcriber

log = logging.getLogger(__name__)
HTML = ParseMode.HTML

ATTACH_WINDOW = timedelta(minutes=3)   # a photo without caption joins the task you just added
UNDO_WINDOW = timedelta(minutes=15)    # how long the "undo done" button stays in the list
DETAILS_WINDOW = 15 * 60               # seconds the "add details" mode stays on


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> DB:
    return ctx.bot_data["db"]


def _cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.bot_data["cfg"]


def _transcriber(ctx: ContextTypes.DEFAULT_TYPE) -> Transcriber:
    return ctx.bot_data["transcriber"]


def _name(update: Update) -> str | None:
    u = update.effective_user
    return u.first_name if u else None


# ------------------------------------------------------------------ access


async def _allowed(update: Update, ctx: ContextTypes.DEFAULT_TYPE, warn: bool = True) -> bool:
    user = update.effective_user
    if user and user.id in _cfg(ctx).allowed_users:
        return True
    if warn and update.effective_message:
        await update.effective_message.reply_text(
            f"Non sei ancora abilitato. Il tuo id è <code>{user.id if user else '?'}</code>: "
            "va aggiunto ad ALLOWED_USERS nel file .env del bot.", parse_mode=HTML)
    return False


# ------------------------------------------------------------------- views


async def _edit(ctx, chat_id: int, message_id: int, text: str, kb) -> bool:
    try:
        await ctx.bot.edit_message_text(text, chat_id, message_id, parse_mode=HTML, reply_markup=kb)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        log.info("cannot edit %s/%s: %s", chat_id, message_id, e)
        return False
    except Forbidden:
        return False


async def _delete(ctx, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await ctx.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


def _render_list(ctx, user_id: int, page: int):
    db, cfg = _db(ctx), _cfg(ctx)
    now = now_utc()
    done_today = len(db.done_since(render.since_for(1, cfg.tz, now)))
    return render.todo_list(db.open_tasks(), page, now, db.last_done_by(user_id, UNDO_WINDOW), done_today)


def _render_done(ctx, days: int):
    db, cfg = _db(ctx), _cfg(ctx)
    now = now_utc()
    return render.done_view(db.done_since(render.since_for(days, cfg.tz, now)), len(db.open_tasks()),
                            days, cfg.tz, now)


async def refresh_lists(ctx: ContextTypes.DEFAULT_TYPE, except_chat: int | None = None) -> None:
    """Silently re-render the open to-do lists of everyone (edits only, never new messages)."""
    db = _db(ctx)
    for v in db.list_views():
        if v.chat_id == except_chat:
            continue
        text, kb, page = _render_list(ctx, v.chat_id, v.page)
        if not await _edit(ctx, v.chat_id, v.message_id, text, kb):
            db.forget_view(v.chat_id)


async def _show_in(ctx, q_message: Message, kind: str, text: str, kb, page: int = 0) -> None:
    """Show a view in the message that was tapped, and make it this chat's only view."""
    chat_id = q_message.chat_id
    if await _edit(ctx, chat_id, q_message.message_id, text, kb):
        old = _db(ctx).set_view(chat_id, q_message.message_id, kind, page)
        await _delete(ctx, chat_id, old)


async def _send_view(ctx, chat_id: int, kind: str, text: str, kb, page: int = 0) -> None:
    """Send a fresh view at the bottom of the chat and remove the previous one."""
    m = await ctx.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
    old = _db(ctx).set_view(chat_id, m.message_id, kind, page)
    await _delete(ctx, chat_id, old)


# ---------------------------------------------------------------- commands


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    await update.effective_message.reply_text(
        f"Ciao {escape(_name(update) or '')}! " + render.HELP, parse_mode=HTML, reply_markup=render.MENU)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    await update.effective_message.reply_text(render.HELP, parse_mode=HTML, reply_markup=render.MENU)


async def show_todo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    chat_id = update.effective_chat.id
    if update.effective_message.text and not update.effective_message.text.startswith("/"):
        await _delete(ctx, chat_id, update.effective_message.message_id)  # keep the chat tidy
    text, kb, page = _render_list(ctx, update.effective_user.id, 0)
    await _send_view(ctx, chat_id, "list", text, kb, page)


async def show_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    chat_id = update.effective_chat.id
    if update.effective_message.text and not update.effective_message.text.startswith("/"):
        await _delete(ctx, chat_id, update.effective_message.message_id)
    days = _cfg(ctx).done_days
    if ctx.args:
        try:
            days = max(1, min(365, int(ctx.args[0])))
        except ValueError:
            pass
    text, kb = _render_done(ctx, days)
    await _send_view(ctx, chat_id, "done", text, kb)


# ------------------------------------------------------- "add details" mode


def _details_target(ctx: ContextTypes.DEFAULT_TYPE) -> Task | None:
    pending = ctx.user_data.get("details")
    if not pending:
        return None
    task_id, expires = pending
    if time.monotonic() > expires:
        ctx.user_data.pop("details", None)
        return None
    t = _db(ctx).task(task_id)
    if t is None or t.status == "deleted":
        ctx.user_data.pop("details", None)
        return None
    ctx.user_data["details"] = (task_id, time.monotonic() + DETAILS_WINDOW)  # extend while in use
    return t


def _add_details_text(db: DB, t: Task, text: str) -> Task:
    if t.title in render.PLACEHOLDERS:  # a photo/voice-only task gets its title from the first words
        title, rest = split_title(text)
        return db.update_text(t.id, title=title, append_notes=rest or None)
    return db.update_text(t.id, append_notes=text)


async def _ack(msg: Message) -> None:
    try:
        await msg.set_reaction("👍")
    except TelegramError:
        pass


# ---------------------------------------------------------------- new tasks


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    db, msg = _db(ctx), update.effective_message
    text = msg.text.strip()

    target = _details_target(ctx)
    if target:
        _add_details_text(db, target, text)
        await _ack(msg)
        await refresh_lists(ctx)
        return

    user = update.effective_user
    tasks = [db.add_task(title, notes, user.id, user.first_name) for title, notes in parse_tasks(text)]
    if not tasks:
        return
    reply, kb = render.added(tasks)
    await msg.reply_text(reply, parse_mode=HTML, reply_markup=kb, disable_notification=True)
    await refresh_lists(ctx)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    db, msg, user = _db(ctx), update.effective_message, update.effective_user
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()
    groups: dict = ctx.bot_data.setdefault("media_groups", {})

    # Albums arrive as several messages sharing a media_group_id: they all go to one task.
    # (No await before the mapping is stored, so concurrent updates can't create two tasks.)
    if msg.media_group_id and msg.media_group_id in groups:
        t = db.task(groups[msg.media_group_id])
        db.add_photo(t.id, file_id)
        if caption:
            _add_details_text(db, t, caption)
        await refresh_lists(ctx)
        return

    target = _details_target(ctx)
    if target:
        if msg.media_group_id:
            groups[msg.media_group_id] = target.id
        db.add_photo(target.id, file_id)
        if caption:
            _add_details_text(db, target, caption)
        await _ack(msg)
        await refresh_lists(ctx)
        return

    if not caption:
        recent = db.last_added_by(user.id, ATTACH_WINDOW)
        if recent and not msg.media_group_id:
            photo_id = db.add_photo(recent.id, file_id)
            await msg.reply_text(
                f"📎 Foto aggiunta a <b>{escape(recent.title)}</b>", parse_mode=HTML, disable_notification=True,
                reply_markup=render.InlineKeyboardMarkup([[
                    render.B("↩️ Annulla", callback_data=f"UP:{photo_id}"),
                    render.B("➕ Nuova attività", callback_data=f"NP:{photo_id}"),
                ]]))
            await refresh_lists(ctx)
            return

    title, notes = split_title(caption) if caption else (render.PH_PHOTO, "")
    t = db.add_task(title, notes, user.id, user.first_name)
    if msg.media_group_id:
        groups[msg.media_group_id] = t.id
        if len(groups) > 200:
            groups.pop(next(iter(groups)))
    db.add_photo(t.id, file_id)
    reply, kb = render.added([db.task(t.id)])
    await msg.reply_text(reply, parse_mode=HTML, reply_markup=kb, disable_notification=True)
    await refresh_lists(ctx)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, ctx):
        return
    db, msg, user = _db(ctx), update.effective_message, update.effective_user
    voice = msg.voice or msg.audio
    transcriber = _transcriber(ctx)
    target = _details_target(ctx)

    status = None
    transcript = ""
    if not isinstance(transcriber, NoTranscriber):
        status = await msg.reply_text("🎙️ Trascrivo…", disable_notification=True)
        try:
            f = await ctx.bot.get_file(voice.file_id)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "voice.ogg"
                await f.download_to_drive(path)
                transcript = clean_transcript(await transcriber.transcribe(path))
        except Exception:
            log.exception("transcription failed")
            transcript = ""

    if target:
        if transcript:
            _add_details_text(db, target, transcript)
        if not target.voice_file_id:
            db.set_voice(target.id, voice.file_id)
        if status:
            await _delete(ctx, msg.chat_id, status.message_id)
        await _ack(msg)
        await refresh_lists(ctx)
        return

    if transcript:
        title, notes = split_title(transcript)
    else:
        title, notes = render.PH_VOICE, ""
    t = db.add_task(title, notes, user.id, user.first_name, voice_file_id=voice.file_id, from_voice=True)
    reply, kb = render.added([t], transcript=transcript or None)
    if not transcript and status:
        reply += "\n<i>Non sono riuscito a trascriverla: l'audio è salvato con l'attività.</i>"
    if status:
        await _edit(ctx, msg.chat_id, status.message_id, reply, kb)
    else:
        await msg.reply_text(reply, parse_mode=HTML, reply_markup=kb, disable_notification=True)
    await refresh_lists(ctx)


# ------------------------------------------------------------------ buttons


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id not in _cfg(ctx).allowed_users:
        await q.answer("Non autorizzato", show_alert=True)
        return
    db, cfg = _db(ctx), _cfg(ctx)
    user = q.from_user
    chat_id = q.message.chat_id
    action, _, arg = q.data.partition(":")
    args = arg.split(":") if arg else []

    if action == "noop":
        await q.answer()

    elif action == "X":  # close a photo / audio message
        await q.answer()
        await _delete(ctx, chat_id, q.message.message_id)

    elif action == "L":  # to-do list, page n
        await q.answer()
        text, kb, page = _render_list(ctx, user.id, int(args[0]) if args else 0)
        await _show_in(ctx, q.message, "list", text, kb, page)

    elif action == "F":  # done view
        await q.answer()
        text, kb = _render_done(ctx, int(args[0]) if args else cfg.done_days)
        await _show_in(ctx, q.message, "done", text, kb)

    elif action == "O":  # open a task
        t = db.task(int(args[0]))
        if t is None or t.status == "deleted":
            await q.answer("Attività non più presente")
            text, kb, page = _render_list(ctx, user.id, 0)
            await _show_in(ctx, q.message, "list", text, kb, page)
            return
        await q.answer()
        text, kb = render.task_detail(t, cfg.tz, now_utc())
        await _show_in(ctx, q.message, "detail", text, kb)

    elif action in ("D", "DD", "PD"):  # mark done (from list / detail / photo)
        task_id = int(args[0])
        t = db.mark_done(task_id, user.id, user.first_name)
        if t:
            await q.answer(f"☑️ {t.title}"[:190])
        else:
            cur = db.task(task_id)
            who = cur.done_by_name if cur and cur.status == "done" else None
            await q.answer(f"Già fatta da {who}" if who else "Attività non più presente")
        if action == "PD":
            await _delete(ctx, chat_id, q.message.message_id)
            v = db.view(chat_id)
            if v:
                text, kb, page = _render_list(ctx, user.id, v.page if v.kind == "list" else 0)
                if await _edit(ctx, chat_id, v.message_id, text, kb):
                    db.set_view(chat_id, v.message_id, "list", page)
        else:
            page = int(args[1]) if action == "D" and len(args) > 1 else 0
            text, kb, page = _render_list(ctx, user.id, page)
            await _show_in(ctx, q.message, "list", text, kb, page)
        await refresh_lists(ctx, except_chat=chat_id)

    elif action in ("U", "RO"):  # undo "done" from the list / reopen from the detail
        t = db.reopen(int(args[0]))
        await q.answer(f"↩️ Di nuovo da fare: {t.title}"[:190] if t else "Era già da fare")
        if action == "U":
            text, kb, page = _render_list(ctx, user.id, int(args[1]) if len(args) > 1 else 0)
            await _show_in(ctx, q.message, "list", text, kb, page)
        else:
            text, kb = render.task_detail(db.task(int(args[0])), cfg.tz, now_utc())
            await _show_in(ctx, q.message, "detail", text, kb)
        await refresh_lists(ctx, except_chat=chat_id)

    elif action == "P":  # photo viewer: one message, browse with arrows
        t = db.task(int(args[0]))
        photos = db.photos(t.id) if t else []
        if not photos:
            await q.answer("Nessuna foto")
            return
        idx = int(args[1]) % len(photos)
        await q.answer()
        kb = render.photo_buttons(t, idx, len(photos))
        caption = f"{t.title}" + (f" ({idx + 1}/{len(photos)})" if len(photos) > 1 else "")
        if q.message.photo:  # already the viewer: swap the picture
            try:
                await q.edit_message_media(InputMediaPhoto(photos[idx], caption=caption), reply_markup=kb)
            except BadRequest:
                pass
        else:
            await ctx.bot.send_photo(chat_id, photos[idx], caption=caption, reply_markup=kb,
                                     disable_notification=True)

    elif action == "A":  # play the voice note
        t = db.task(int(args[0]))
        await q.answer()
        if t and t.voice_file_id:
            await ctx.bot.send_voice(chat_id, t.voice_file_id, caption=t.title,
                                     reply_markup=render.close_button(), disable_notification=True)

    elif action == "E":  # add details mode
        t = db.task(int(args[0]))
        if t is None or t.status != "open":
            await q.answer("Attività non più aperta")
            return
        await q.answer()
        ctx.user_data["details"] = (t.id, time.monotonic() + DETAILS_WINDOW)
        text, kb = render.details_prompt(t)
        await ctx.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb, disable_notification=True)

    elif action == "N":  # end of add details mode
        pending = ctx.user_data.pop("details", None)
        t = db.task(pending[0]) if pending else None
        await q.answer("Dettagli salvati" if t else "")
        await _edit(ctx, chat_id, q.message.message_id,
                    f"✔️ Dettagli salvati: <b>{escape(t.title)}</b>" if t else "✔️ Fatto", None)

    elif action == "R":  # ask to delete
        t = db.task(int(args[0]))
        await q.answer()
        if t:
            await q.edit_message_reply_markup(render.confirm_delete(t))

    elif action == "RY":  # delete
        t = db.delete(int(args[0]))
        await q.answer(f"🗑 Eliminata: {t.title}"[:190] if t else "")
        text, kb, page = _render_list(ctx, user.id, 0)
        await _show_in(ctx, q.message, "list", text, kb, page)
        await refresh_lists(ctx, except_chat=chat_id)

    elif action == "UA":  # undo "added" (one or more tasks)
        removed = [t for t in (db.delete(int(i)) for i in arg.split(",")) if t]
        await q.answer("Annullato" if removed else "Già rimossa")
        if removed:
            names = ", ".join(escape(t.title) for t in removed)
            await _edit(ctx, chat_id, q.message.message_id, f"<s>{names}</s>\n↩️ Annullato", None)
        ctx.user_data.pop("details", None)
        await refresh_lists(ctx)

    elif action in ("UP", "NP"):  # undo photo attachment / make it a separate task
        row = db.conn.execute("SELECT task_id, file_id FROM photos WHERE id = ?", (int(args[0]),)).fetchone()
        if row is None:
            await q.answer()
            return
        db.remove_photo(int(args[0]))
        if action == "UP":
            await q.answer("Foto tolta")
            await _edit(ctx, chat_id, q.message.message_id, "↩️ Foto tolta", None)
        else:
            t = db.add_task(render.PH_PHOTO, "", user.id, user.first_name)
            db.add_photo(t.id, row["file_id"])
            await q.answer()
            text, kb = render.added([db.task(t.id)])
            await _edit(ctx, chat_id, q.message.message_id, text, kb)
        await refresh_lists(ctx)
    else:
        await q.answer()


# --------------------------------------------------------------------- app


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("dafare", "Cosa c'è da fare"),
        BotCommand("fatte", "Cosa è stato fatto (es. /fatte 30)"),
        BotCommand("aiuto", "Come funziona"),
    ])
    t = app.bot_data["transcriber"]
    if isinstance(t, LocalWhisper):
        app.create_task(t.warm_up())


async def _error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error while handling an update", exc_info=ctx.error)


def create_app(cfg: Config, db: DB, transcriber: Transcriber | None = None) -> Application:
    app = (Application.builder().token(cfg.token).post_init(_post_init)
           .concurrent_updates(True)  # a slow transcription doesn't block the others
           .build())
    app.bot_data["db"] = db
    app.bot_data["cfg"] = cfg
    app.bot_data["transcriber"] = transcriber or make_transcriber(
        cfg.transcriber, model=cfg.whisper_model, openai_key=cfg.openai_api_key, openai_model=cfg.openai_model)

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler(["aiuto", "help"], cmd_help, filters=private))
    app.add_handler(CommandHandler(["dafare", "todo", "lista"], show_todo, filters=private))
    app.add_handler(CommandHandler(["fatte", "fatti", "done"], show_done, filters=private))
    app.add_handler(MessageHandler(private & filters.Text([render.BTN_TODO]), show_todo))
    app.add_handler(MessageHandler(private & filters.Text([render.BTN_DONE]), show_done))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(private & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(_error)
    return app
