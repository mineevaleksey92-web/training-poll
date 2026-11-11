"""Telegram-бот: присылаешь фото/видео + текстом говоришь, что сделать,
Claude смотрит на файлы, монтирует их через ffmpeg и присылает результат.

Запуск:  python bot.py   (переменные окружения — см. .env.example)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from media_tools import (
    IMAGE_EXT,
    TOOLS,
    MediaToolbox,
    ToolError,
    reset_workspace,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("media-bot")
# httpx пишет в лог каждый URL Bot API, а в нём токен бота — глушим.
for _noisy in ("httpx", "httpx2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
MAX_TOOL_ROUNDS = 40
MAX_HISTORY_MESSAGES = 60
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ru") or None

SYSTEM_PROMPT = """Ты — видеомонтажёр и фоторедактор внутри Telegram-бота.
Пользователь присылает фото и видео и пишет, что с ними сделать.
Файлы лежат в рабочей папке; работаешь только через инструменты.

Как работать:
1. Посмотри исходники (probe_media для видео, view_media чтобы увидеть содержание).
2. Если задача понятна — сразу делай, не переспрашивай по мелочам. Если просьба
   действительно неоднозначна (например, «сделай красиво» без контекста), выбери
   разумный вариант и коротко скажи, что выбрал.
3. Монтируй через run_ffmpeg. Для видео по умолчанию: H.264 (libx264, -crf 20-23,
   -preset veryfast), AAC-звук, -pix_fmt yuv420p, -movflags +faststart.
   Учитывай поворот (rotation) у видео с телефона и разные разрешения/fps при склейке —
   приводи всё к одному формату (scale+pad, fps, setsar=1) перед concat.
4. Проверь результат: view_media на ключевых моментах и probe_media.
   Если что-то не так — исправь.
5. Отправь через send_file. Итоговые файлы называй out_1.mp4, out_2.jpg и т.д.,
   не перезаписывай исходники.
6. Ответь пользователю коротко, по-русски: что сделано, и что можно поправить.
Лимит Telegram на отправку — 50 МБ; если результат больше, пережми.
Для текста (drawtext, в том числе кириллица) используй fontfile=font.ttf —
этот шрифт лежит в рабочей папке; не удаляй его и не отправляй пользователю."""

client = anthropic.AsyncAnthropic()


@dataclass
class ChatSession:
    workspace: Path
    messages: list = field(default_factory=list)
    pending_files: list[str] = field(default_factory=list)
    counter: int = 0
    album_captions: dict[str, str] = field(default_factory=dict)
    album_scheduled: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


SESSIONS: dict[int, ChatSession] = {}


FONT_CANDIDATES = [
    os.environ.get("FONT_FILE", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux / Docker
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",                          # Windows
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]


def install_font(workspace: Path) -> None:
    """Кладёт шрифт с кириллицей в рабочую папку как font.ttf."""
    target = workspace / "font.ttf"
    if target.exists():
        return
    for cand in FONT_CANDIDATES:
        if cand and Path(cand).is_file():
            shutil.copyfile(cand, target)
            return
    log.warning("Не найден шрифт для подписей; задайте FONT_FILE в .env")


def get_session(chat_id: int) -> ChatSession:
    if chat_id not in SESSIONS:
        ws = DATA_DIR / str(chat_id)
        ws.mkdir(parents=True, exist_ok=True)
        install_font(ws)
        SESSIONS[chat_id] = ChatSession(workspace=ws)
    return SESSIONS[chat_id]


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USERS or user.id in ALLOWED_USERS)


# ---------- Telegram handlers ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text(f"Нет доступа. Ваш id: {update.effective_user.id}")
        return
    await update.message.reply_text(
        "Привет! Пришли фото или видео (можно несколько) и напиши, что сделать.\n"
        "Примеры:\n"
        "• «обрежь с 0:05 до 0:20 и сделай вертикальным 9:16»\n"
        "• «склей эти три видео, добавь плавные переходы»\n"
        "• «сделай фото ярче и теплее, обрежь в квадрат»\n"
        "• «ускорь в 2 раза и добавь подпись внизу»\n\n"
        "Подпись к файлу тоже считается заданием.\n"
        "/new — начать новый проект (очистить файлы и историю)\n"
        "/files — показать файлы проекта"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    s = get_session(update.effective_chat.id)
    async with s.lock:
        reset_workspace(s.workspace)
        install_font(s.workspace)
        s.messages.clear()
        s.pending_files.clear()
        s.counter = 0
    await update.message.reply_text("Новый проект. Присылай файлы.")


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    s = get_session(update.effective_chat.id)
    files = sorted(p.name for p in s.workspace.iterdir() if p.is_file())
    await update.message.reply_text("\n".join(files) or "Файлов нет.")


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    msg = update.message
    s = get_session(update.effective_chat.id)

    if msg.photo:
        tg_file, ext = msg.photo[-1], ".jpg"
    elif msg.video:
        tg_file, ext = msg.video, Path(msg.video.file_name or "v.mp4").suffix or ".mp4"
    elif msg.animation:
        tg_file, ext = msg.animation, ".mp4"
    elif msg.video_note:
        tg_file, ext = msg.video_note, ".mp4"
    elif msg.audio:
        tg_file, ext = msg.audio, Path(msg.audio.file_name or "a.mp3").suffix or ".mp3"
    elif msg.document:
        tg_file, ext = msg.document, Path(msg.document.file_name or "file").suffix.lower()
    else:
        return

    if getattr(tg_file, "file_size", 0) and tg_file.file_size > 20 * 1024 * 1024:
        await msg.reply_text(
            "Файл больше 20 МБ — облачный Bot API Telegram не даёт боту скачать такой. "
            "Сожми видео или подними локальный Bot API сервер (см. README)."
        )
        return

    s.counter += 1
    kind = "photo" if ext.lower() in IMAGE_EXT else "media"
    name = f"{kind}_{s.counter}{ext.lower()}"
    f = await tg_file.get_file()
    await f.download_to_drive(s.workspace / name)
    s.pending_files.append(name)
    log.info("chat %s: saved %s", update.effective_chat.id, name)

    if msg.media_group_id is not None:
        # Альбом приходит отдельными сообщениями; подпись — только у одного.
        # Ждём, пока докачаются остальные файлы, и запускаем задачу один раз.
        if msg.caption:
            s.album_captions[msg.media_group_id] = msg.caption
        if msg.media_group_id not in s.album_scheduled:
            s.album_scheduled.add(msg.media_group_id)
            asyncio.create_task(_run_album(update, context, s, msg.media_group_id))
    elif msg.caption:
        await run_task(update, context, msg.caption)
    else:
        await msg.reply_text(f"Получил {name}. Напиши, что с ним сделать (или пришли ещё файлы).")


async def _run_album(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     s: ChatSession, group_id: str) -> None:
    await asyncio.sleep(4)
    s.album_scheduled.discard(group_id)
    caption = s.album_captions.pop(group_id, None)
    if caption:
        await run_task(update, context, caption)
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            f"Получил файлы: {', '.join(s.pending_files)}. Напиши, что с ними сделать.",
        )


_whisper_model = None
_whisper_lock = asyncio.Lock()


def _transcribe_sync(path: Path) -> str:
    segments, _info = _whisper_model.transcribe(
        str(path), language=WHISPER_LANGUAGE, beam_size=5,
        vad_filter=False,  # VAD требует onnxruntime — без него тоже работает
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


async def transcribe(path: Path) -> str:
    """Распознаёт речь локально через faster-whisper (модель качается один раз)."""
    global _whisper_model
    async with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel  # импорт здесь: пакет необязательный

            log.info("Загружаю модель Whisper '%s' (первый раз — скачивание)…", WHISPER_MODEL)
            _whisper_model = await asyncio.to_thread(
                WhisperModel, WHISPER_MODEL, device="cpu", compute_type="int8"
            )
        return await asyncio.to_thread(_transcribe_sync, path)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое сообщение = текстовая команда: распознаём и выполняем."""
    if not allowed(update):
        return
    msg = update.message
    voice_dir = DATA_DIR / "_voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    path = voice_dir / f"{msg.chat_id}_{msg.message_id}.ogg"
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    try:
        f = await msg.voice.get_file()
        await f.download_to_drive(path)
        text = await transcribe(path)
    except ImportError:
        await msg.reply_text(
            "Распознавание голоса не установлено. На компьютере с ботом выполните:\n"
            "pip install ctranslate2 huggingface-hub tokenizers av tqdm\n"
            "pip install --no-deps faster-whisper\n"
            "и перезапустите бота."
        )
        return
    except Exception:
        log.exception("voice transcription failed")
        await msg.reply_text("Не получилось распознать голосовое. Попробуй ещё раз или напиши текстом.")
        return
    finally:
        path.unlink(missing_ok=True)

    if not text:
        await msg.reply_text("Ничего не расслышал — попробуй ещё раз.")
        return
    await msg.reply_text(f"🎤 «{text}»")
    await run_task(update, context, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await run_task(update, context, update.message.text)


# ---------- Claude agent loop ----------

def trim_history(messages: list) -> list:
    """Обрезает старую историю, начиная с обычного пользовательского сообщения."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    tail = messages[-MAX_HISTORY_MESSAGES:]
    for i, m in enumerate(tail):
        if m["role"] == "user" and isinstance(m["content"], list) and all(
            b.get("type") == "text" for b in m["content"] if isinstance(b, dict)
        ):
            return tail[i:]
    return messages


async def run_task(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    s = get_session(chat_id)

    async def send_file(path: Path, caption: str, as_document: bool) -> None:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        with path.open("rb") as fh:
            ext = path.suffix.lower()
            if as_document:
                await context.bot.send_document(chat_id, fh, caption=caption or None)
            elif ext in IMAGE_EXT:
                await context.bot.send_photo(chat_id, fh, caption=caption or None)
            elif ext == ".gif":
                await context.bot.send_animation(chat_id, fh, caption=caption or None)
            elif ext in {".mp4", ".mov"}:
                await context.bot.send_video(
                    chat_id, fh, caption=caption or None, supports_streaming=True,
                    read_timeout=300, write_timeout=300,
                )
            else:
                await context.bot.send_document(chat_id, fh, caption=caption or None)

    toolbox = MediaToolbox(s.workspace, send_file)

    async with s.lock:
        note = ""
        if s.pending_files:
            note = f"[Новые файлы от пользователя: {', '.join(s.pending_files)}]\n"
            s.pending_files.clear()
        s.messages.append({"role": "user", "content": [{"type": "text", "text": note + text}]})
        s.messages[:] = trim_history(s.messages)

        status = await update.message.reply_text("⏳ Работаю…")
        try:
            final_text = await agent_loop(s, toolbox, chat_id, context)
        except anthropic.RateLimitError:
            final_text = "Слишком много запросов к Claude, попробуй через минуту."
        except anthropic.APIStatusError as e:
            log.exception("Claude API error")
            final_text = f"Ошибка Claude API ({e.status_code}). Попробуй ещё раз или /new."
        except anthropic.APIConnectionError:
            final_text = "Нет связи с Claude API, попробуй позже."
        try:
            await status.delete()
        except Exception:
            pass
        for chunk in split_message(final_text or "Готово."):
            await context.bot.send_message(chat_id, chunk)


async def agent_loop(s: ChatSession, toolbox: MediaToolbox, chat_id: int,
                     context: ContextTypes.DEFAULT_TYPE) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        async with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=s.messages,
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = await stream.get_final_message()

        # Ответ модели сохраняем целиком (в т.ч. thinking-блоки) — без изменений.
        s.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return "Claude отказался выполнять этот запрос."
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            return "\n".join(b.text for b in response.content if b.type == "text").strip()

        results = []
        for tu in tool_uses:
            log.info("chat %s: tool %s %s", chat_id, tu.name, str(tu.input)[:300])
            try:
                out = await toolbox.execute(tu.name, dict(tu.input))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            except (ToolError, TypeError) as e:
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": f"Ошибка: {e}", "is_error": True})
            except Exception as e:  # сбой отправки в Telegram и т.п.
                log.exception("tool failed")
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": f"Внутренняя ошибка: {e}", "is_error": True})
        s.messages.append({"role": "user", "content": results})

    return "Задача оказалась слишком длинной — остановился. Уточни запрос или разбей на шаги."


def split_message(text: str, limit: int = 4000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def main() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(
            f"Не найдено: {', '.join(missing)}. Установите ffmpeg "
            "(Windows: winget install Gyan.FFmpeg, затем откройте НОВОЕ окно терминала; "
            "macOS: brew install ffmpeg; Linux: apt install ffmpeg) и запустите бота снова."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(300)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("files", cmd_files))
    media = (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE
             | filters.AUDIO | filters.Document.ALL)
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(media, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started, model=%s", MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
