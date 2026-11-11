# Telegram-бот для монтажа фото и видео с Claude

Отправляете боту фото/видео и пишете, что с ними сделать. Claude смотрит на файлы
(раскадровка видео, сами фото), монтирует их через **ffmpeg** и присылает результат.

Примеры запросов:

- «обрежь с 0:05 до 0:20 и сделай вертикально 9:16 для Reels»
- «склей эти три видео по порядку, добавь плавные переходы и музыку из mp3»
- «ускорь в 2 раза, убери звук, добавь подпись «Отпуск 2026» внизу»
- «сделай фото ярче и теплее, обрежь в квадрат»
- «сделай из видео GIF на 5 секунд»
- после результата: «чуть темнее и подпись крупнее» — бот помнит контекст проекта

## Как это устроено

```
Вы (Telegram) ──фото/видео + текст──▶ bot.py ──▶ Claude API (claude-opus-5-5)
                                         ▲            │ вызывает инструменты
                                         │            ▼
                                   send_file ◀── media_tools.py: probe_media, view_media,
                                                 run_ffmpeg, write_text_file, list_files
```

- Каждый чат — отдельный «проект»: файлы в `data/<chat_id>/`, история диалога в памяти.
- Claude не может «смотреть» видео целиком, поэтому инструмент `view_media` даёт ему
  до 8 кадров из нужных моментов — этого хватает, чтобы понять сюжет и проверить результат.
- ffmpeg запускается без shell, только с файлами из папки проекта (пути наружу,
  URL и `..` блокируются).

## Что понадобится

1. **Токен бота** — напишите [@BotFather](https://t.me/BotFather) → `/newbot` → получите токен.
2. **Ключ Claude API** — [console.anthropic.com](https://console.anthropic.com) → API Keys.
   API платный (оплата за токены, отдельно от подписки Claude Pro/Max). Каждая правка
   видео — это несколько запросов с картинками-кадрами; ориентир — несколько центов
   за задачу.
3. **Где запускать** — любой компьютер или VPS, где есть Python 3.10+ и ffmpeg
   (или Docker). Бот работает через long polling — белый IP и домен не нужны.

## Запуск

### Вариант 1: Docker (проще всего на сервере)

```bash
cd telegram-media-bot
cp .env.example .env        # впишите токены и свой user id
docker build -t media-bot .
docker run -d --name media-bot --restart unless-stopped \
  --env-file .env -v $(pwd)/data:/data media-bot
```

### Вариант 2: напрямую

```bash
# ffmpeg: Ubuntu/Debian — sudo apt install ffmpeg fonts-dejavu-core
#         macOS — brew install ffmpeg
#         Windows — winget install ffmpeg
cd telegram-media-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # впишите токены
python bot.py
```

### Вариант 3: на своём компьютере Windows

1. Откройте «Терминал» (или PowerShell) и установите Python и ffmpeg:
   ```
   winget install Python.Python.3.12
   winget install Gyan.FFmpeg
   ```
   Закройте и снова откройте терминал, проверьте: `python --version` и `ffmpeg -version`.
2. Скачайте код (Code → Download ZIP на GitHub), распакуйте, откройте папку
   `telegram-media-bot`, в адресной строке проводника наберите `powershell` и нажмите Enter.
3. В открывшемся окне:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   notepad .env
   ```
   Впишите токен, ключ Claude и свой id, сохраните.
   (Если PowerShell ругается на `activate`, выполните
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` и повторите.)
4. Запуск: `python bot.py`. Пока окно открыто — бот работает. Остановить: Ctrl+C.
   В следующий раз: открыть папку → `powershell` → `.venv\Scripts\activate` → `python bot.py`.

### Вариант 4: на своём Mac

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # если нет Homebrew
brew install python ffmpeg
cd ~/Downloads/<распакованная папка>/telegram-media-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && open -e .env     # впишите токены и сохраните
python bot.py
```

Шрифт для подписей на видео бот найдёт сам (DejaVu на Linux, Arial на Windows/Mac).
Если нужен другой — укажите путь к .ttf в `FONT_FILE` в `.env`.

### Обязательно ограничьте доступ

Запросы к Claude оплачиваете вы, поэтому впишите свой Telegram id в `ALLOWED_USER_IDS`.
Узнать id: запустите бота, отправьте `/start` — он ответит «Нет доступа. Ваш id: …»
(если список не пуст) — или спросите у [@userinfobot](https://t.me/userinfobot).

## Команды бота

| Команда  | Что делает |
|----------|------------|
| `/start` | подсказка |
| `/new`   | новый проект: удалить файлы и историю |
| `/files` | список файлов проекта |

Файл можно отправить с подписью — подпись сразу считается заданием. Несколько файлов
можно отправить альбомом.

## Ограничения Telegram

- **Скачивание ботом — до 20 МБ**, отправка — до 50 МБ (облачный Bot API).
  Для больших видео поднимите [локальный Bot API сервер](https://github.com/tdlib/telegram-bot-api)
  (до 2 ГБ) и добавьте в `Application.builder()` строку
  `.base_url("http://localhost:8081/bot").local_mode(True)`.
- Фото, отправленное «как фото», Telegram сжимает. Для качества отправляйте «файлом».

## Настройки (`.env`)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `CLAUDE_MODEL` | `claude-opus-5-5` | модель Claude |
| `CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high` — глубина размышлений; `high` точнее на сложном монтаже, но дольше и дороже |
| `DATA_DIR` | `./data` | где хранить файлы проектов |

В запросе включены серверные фолбэки (`fallbacks: "default"`): если модель откажется
от запроса по ошибке фильтров, API сам повторит его на запасной модели.

## Что можно добавить

- Хранить историю в SQLite/Redis, чтобы переживать перезапуск.
- Инструмент для удаления фона с фото (`rembg`) или улучшения качества (Real-ESRGAN).
- Транскрибацию речи (Whisper) → автоматические субтитры.
- Очередь задач (несколько пользователей, долгий рендер).
