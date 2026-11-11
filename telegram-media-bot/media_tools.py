"""Инструменты, которые Claude вызывает для работы с медиафайлами.

Все файлы одного чата лежат в отдельной папке (workspace). Claude видит
только имена файлов внутри неё — абсолютные пути и выход за её пределы
запрещены.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from pathlib import Path

FFMPEG_TIMEOUT_SEC = 600
MAX_FRAMES = 8

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".gif"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".wav", ".aac"}

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}$")

TOOLS = [
    {
        "name": "list_files",
        "description": "Список файлов в рабочей папке чата (исходники пользователя и результаты).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "probe_media",
        "description": (
            "Технические данные файла через ffprobe: длительность, разрешение, fps, "
            "кодеки, наличие звука. Вызывай перед монтажом видео."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": ["filename"],
            "additionalProperties": False,
        },
    },
    {
        "name": "view_media",
        "description": (
            "Посмотреть файл глазами: для фото возвращает само изображение, для видео — "
            f"до {MAX_FRAMES} кадров, равномерно распределённых по длительности "
            "(или в указанные моменты времени). Используй, чтобы понять содержание "
            "и проверить результат перед отправкой."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "frames": {
                    "type": "integer",
                    "description": f"Сколько кадров взять из видео (1–{MAX_FRAMES}).",
                },
                "timestamps": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Конкретные моменты в секундах (вместо равномерной выборки).",
                },
            },
            "required": ["filename"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_ffmpeg",
        "description": (
            "Запустить ffmpeg в рабочей папке. Передай аргументы списком БЕЗ слова "
            "'ffmpeg' и без '-y' (добавляются автоматически). Входы и выходы — только "
            "имена файлов из рабочей папки, без путей. Подходит для обрезки, склейки "
            "(concat), кадрирования, масштабирования, цветокоррекции (eq, curves), "
            "текста (drawtext), ускорения/замедления (setpts, atempo), музыки, "
            "переходов (xfade), стабилизации, конвертации, GIF и т.д. Для фото — "
            "тоже ffmpeg: '-i in.jpg -vf ... out.jpg'. Возвращает хвост stderr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "array", "items": {"type": "string"}},
                "output": {
                    "type": "string",
                    "description": "Имя итогового файла (должно встречаться в args).",
                },
            },
            "required": ["args", "output"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_text_file",
        "description": (
            "Создать текстовый файл в рабочей папке — например список для concat "
            "demuxer (file 'a.mp4') или субтитры .srt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_file",
        "description": (
            "Отправить готовый файл пользователю в Telegram. Вызывай только после "
            "проверки результата через view_media/probe_media."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "caption": {"type": "string"},
                "as_document": {
                    "type": "boolean",
                    "description": "true — отправить файлом без сжатия Telegram.",
                },
            },
            "required": ["filename"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    pass


def _resolve(workspace: Path, name: str, must_exist: bool = True) -> Path:
    if not _SAFE_NAME.match(name) or ".." in name:
        raise ToolError(f"Недопустимое имя файла: {name!r}. Только имя без пути.")
    path = (workspace / name).resolve()
    if path.parent != workspace.resolve():
        raise ToolError("Выход за пределы рабочей папки запрещён.")
    if must_exist and not path.exists():
        raise ToolError(f"Файл {name} не найден. Вызови list_files.")
    return path


# Абсолютный путь внутри строки фильтра (movie=/etc/..., subtitles='/x.srt').
# Деление в выражениях (ih*9/16) сюда не попадает: перед "/" там цифра.
_ABS_PATH_IN_ARG = re.compile(r"(^|[=:,;'\"\s])/")
_ALLOWED_ABS = re.compile(r"fontfile=['\"]?/usr/share/fonts/[A-Za-z0-9_./\-]+")


def _check_ffmpeg_args(args: list[str]) -> None:
    for a in args:
        stripped = _ALLOWED_ABS.sub("fontfile=FONT", a)
        if (_ABS_PATH_IN_ARG.search(stripped) or ".." in a or "://" in a
                or a.startswith("~")):
            raise ToolError(f"Аргумент {a!r} запрещён: только локальные имена файлов.")
        if a in ("-y", "-n"):
            raise ToolError("Не передавай -y / -n — они добавляются автоматически.")


async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"Команда не уложилась в {timeout} с и была остановлена.")
    return proc.returncode, err.decode(errors="replace"), out


async def _duration(path: Path) -> float:
    code, _, out = await _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path.name],
        path.parent, 60,
    )
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _image_block(data: bytes, media_type: str = "image/jpeg") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode(),
        },
    }


async def _frame_jpeg(src: Path, ts: float | None) -> bytes:
    """Один кадр, уменьшенный до 1024px по длинной стороне (экономит токены)."""
    cmd = ["ffmpeg", "-v", "error"]
    if ts is not None:
        cmd += ["-ss", f"{ts:.3f}"]
    cmd += ["-i", src.name, "-frames:v", "1",
            "-vf", "scale='min(1024,iw)':'min(1024,ih)':force_original_aspect_ratio=decrease",
            "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
    code, err, out = await _run(cmd, src.parent, 60)
    if code != 0 or not out:
        raise ToolError(f"Не удалось извлечь кадр: {err[-500:]}")
    return out


class MediaToolbox:
    """Исполняет вызовы инструментов для одного чата."""

    def __init__(self, workspace: Path, send_callback):
        self.workspace = workspace
        self.send_callback = send_callback  # async (path, caption, as_document) -> None

    async def execute(self, name: str, args: dict) -> str | list:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ToolError(f"Неизвестный инструмент {name}")
        return await handler(**args)

    async def _tool_list_files(self) -> str:
        files = sorted(p for p in self.workspace.iterdir() if p.is_file())
        if not files:
            return "Папка пуста."
        return "\n".join(f"{p.name}\t{p.stat().st_size / 1_048_576:.2f} MB" for p in files)

    async def _tool_probe_media(self, filename: str) -> str:
        path = _resolve(self.workspace, filename)
        code, err, out = await _run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path.name],
            self.workspace, 60,
        )
        if code != 0:
            raise ToolError(err[-1500:])
        data = json.loads(out)
        brief = {
            "format": {k: data.get("format", {}).get(k)
                       for k in ("format_name", "duration", "size", "bit_rate")},
            "streams": [
                {k: s.get(k) for k in (
                    "codec_type", "codec_name", "width", "height", "r_frame_rate",
                    "duration", "sample_rate", "channels", "pix_fmt",
                ) if s.get(k) is not None}
                | ({"rotation": s["side_data_list"][0].get("rotation")}
                   if s.get("side_data_list") else {})
                for s in data.get("streams", [])
            ],
        }
        return json.dumps(brief, ensure_ascii=False)

    async def _tool_view_media(self, filename: str, frames: int = 4,
                               timestamps: list[float] | None = None) -> list:
        path = _resolve(self.workspace, filename)
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            return [_image_block(await _frame_jpeg(path, None)),
                    {"type": "text", "text": f"{filename}"}]
        if ext in AUDIO_EXT:
            raise ToolError("Это аудио — используй probe_media.")
        duration = await _duration(path)
        if timestamps:
            points = [t for t in timestamps if 0 <= t <= max(duration, 0.1)][:MAX_FRAMES]
        else:
            n = max(1, min(frames, MAX_FRAMES))
            points = [duration * (i + 0.5) / n for i in range(n)] if duration else [0.0]
        blocks: list = []
        for t in points:
            blocks.append({"type": "text", "text": f"Кадр на {t:.2f} с:"})
            blocks.append(_image_block(await _frame_jpeg(path, t)))
        blocks.append({"type": "text", "text": f"Длительность {filename}: {duration:.2f} с"})
        return blocks

    async def _tool_run_ffmpeg(self, args: list[str], output: str) -> str:
        out_path = _resolve(self.workspace, output, must_exist=False)
        if output not in args:
            raise ToolError("Имя output должно присутствовать в args.")
        _check_ffmpeg_args(args)
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y",
               "-protocol_whitelist", "file,pipe,concat,subfile", *args]
        code, err, _ = await _run(cmd, self.workspace, FFMPEG_TIMEOUT_SEC)
        tail = err[-2000:]
        if code != 0 or not out_path.exists():
            raise ToolError(f"ffmpeg завершился с кодом {code}:\n{tail}")
        size = out_path.stat().st_size / 1_048_576
        return f"Готово: {output} ({size:.2f} MB).\n{tail[-600:]}"

    async def _tool_write_text_file(self, filename: str, content: str) -> str:
        path = _resolve(self.workspace, filename, must_exist=False)
        if path.suffix.lower() not in {".txt", ".srt", ".ass", ".vtt"}:
            raise ToolError("Разрешены только .txt, .srt, .ass, .vtt")
        path.write_text(content, encoding="utf-8")
        return f"Записан {filename}"

    async def _tool_send_file(self, filename: str, caption: str = "",
                              as_document: bool = False) -> str:
        path = _resolve(self.workspace, filename)
        size_mb = path.stat().st_size / 1_048_576
        if size_mb > 49:
            raise ToolError(
                f"Файл {size_mb:.1f} MB — больше лимита Telegram Bot API (50 MB). "
                "Пережми его (уменьши битрейт/разрешение, -crf 28) и попробуй снова."
            )
        await self.send_callback(path, caption, as_document)
        return f"Файл {filename} отправлен пользователю."


def reset_workspace(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
