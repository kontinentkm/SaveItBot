"""
Cloud Functions for Firebase (Python) – Telegram webhook + Instagram downloader.

Вся логика бота (парсинг апдейта, скачивание медиа с Instagram и отправка в Telegram)
реализована как HTTP‑функция `telegram_webhook`.

Деплой:
    firebase deploy --only functions

После деплоя возьми URL функции и зарегистрируй вебхук у Telegram:
    curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
         -d "url=<FUNCTION_URL>"
"""

import os
import re
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import requests
from firebase_admin import initialize_app
from firebase_functions import https_fn
from firebase_functions.options import set_global_options
from yt_dlp import YoutubeDL

# Ограничение на количество контейнеров
set_global_options(max_instances=10)

# Инициализация Firebase Admin (если нужно работать с другими сервисами Firebase)
initialize_app()

INSTAGRAM_URL_RE = re.compile(r"(https?://(www\.)?instagram\.com/[^\s]+)")
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm"}
TELEGRAM_ALBUM_LIMIT = 10


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _extract_instagram_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = INSTAGRAM_URL_RE.search(text)
    if not m:
        return None
    return m.group(1).rstrip(").,]")


def _chunked(items: List[Path], size: int) -> Iterable[List[Path]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _collect_media_files(dir_path: Path) -> List[Path]:
    files = [p for p in dir_path.rglob("*") if p.is_file()]
    return sorted(files, key=lambda p: p.name)


def download_instagram_media(url: str, cookies_file: Optional[str], timeout_s: int) -> Tuple[Path, List[Path]]:
    """
    Скачивает медиа с Instagram‑ссылки (post/reel/story/highlight) через yt-dlp.
    Возвращает (tmpdir, [список файлов]).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="igdl_"))
    outtmpl = str(tmpdir / "%(id)s_%(playlist_index)03d.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": timeout_s,
        "noprogress": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        files = _collect_media_files(tmpdir)
        media_files: List[Path] = []
        for f in files:
            if f.suffix.lower() in MEDIA_EXTS:
                media_files.append(f)
        return tmpdir, media_files
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _telegram_api_url(method: str, token: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def tg_send_message(token: str, chat_id: int, text: str) -> None:
    url = _telegram_api_url("sendMessage", token)
    requests.post(
        url,
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def tg_send_media_group(token: str, chat_id: int, files: List[Path]) -> None:
    """
    Отправка медиа группами (альбомами) через sendMediaGroup.
    """
    import json as _json

    for _part_idx, batch in enumerate(_chunked(files, TELEGRAM_ALBUM_LIMIT), start=1):
        media: List[dict] = []
        with ExitStack() as stack:
            files_payload = {}
            for idx, fpath in enumerate(batch):
                field_name = f"file{idx}"
                ext = fpath.suffix.lower()
                if ext in {".mp4", ".mov", ".m4v", ".webm"}:
                    media_type = "video"
                else:
                    media_type = "photo"

                fh = stack.enter_context(open(fpath, "rb"))
                files_payload[field_name] = (fpath.name, fh)

                media.append(
                    {
                        "type": media_type,
                        "media": f"attach://{field_name}",
                    }
                )

            data = {
                "chat_id": str(chat_id),
                "media": _json.dumps(media, ensure_ascii=False),
            }

            url = _telegram_api_url("sendMediaGroup", token)
            requests.post(url, data=data, files=files_payload, timeout=60)


def tg_send_single_media(token: str, chat_id: int, file: Path) -> None:
    ext = file.suffix.lower()
    method = "sendVideo" if ext in {".mp4", ".mov", ".m4v", ".webm"} else "sendPhoto"
    url = _telegram_api_url(method, token)
    with open(file, "rb") as fh:
        files = {"photo" if method == "sendPhoto" else "video": (file.name, fh)}
        requests.post(url, data={"chat_id": str(chat_id)}, files=files, timeout=60)


def handle_update(update: dict, *, token: str, cookies_file: Optional[str], timeout_s: int) -> None:
    """
    Основная логика обработки Telegram‑апдейта.
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = message.get("text") or message.get("caption") or ""
    url = _extract_instagram_url(text)
    if not url:
        tg_send_message(token, chat_id, "Пришли, пожалуйста, ссылку на Instagram (post/reel/story/highlight).")
        return

    tg_send_message(token, chat_id, "Скачиваю медиа с Instagram…")

    tmpdirs: List[Path] = []
    try:
        tmpdir, files = download_instagram_media(url, cookies_file, timeout_s)
        tmpdirs.append(tmpdir)

        if not files:
            tg_send_message(token, chat_id, "Не удалось найти медиа по этой ссылке.")
            return

        if len(files) == 1:
            tg_send_single_media(token, chat_id, files[0])
        else:
            tg_send_media_group(token, chat_id, files)
    except Exception as e:
        tg_send_message(
            token,
            chat_id,
            f"Не получилось скачать по этой ссылке.\nОшибка: {type(e).__name__}",
        )
    finally:
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)


@https_fn.on_request()
def telegram_webhook(req: https_fn.Request) -> https_fn.Response:
    """
    HTTP‑функция для Telegram Webhook.
    """
    if req.method != "POST":
        return https_fn.Response("OK", status=200)

    try:
        update = req.get_json(silent=True) or {}
    except Exception:
        # Некорректный JSON
        return https_fn.Response("Bad Request", status=400)

    token = os.getenv("BOT_TOKEN")
    if not token:
        # Без токена ничего сделать не сможем
        return https_fn.Response("BOT_TOKEN is not configured", status=500)

    cookies_file = os.getenv("IG_COOKIES_FILE") or None
    if cookies_file:
        cookies_file = str(Path(cookies_file).expanduser())
        if not Path(cookies_file).exists():
            cookies_file = None

    timeout_s = _get_env_int("DOWNLOAD_TIMEOUT", 120)

    # Вся логика бота здесь:
    handle_update(update, token=token, cookies_file=cookies_file, timeout_s=timeout_s)

    # Telegram ожидает любой 2xx ответ, тело не важно
    return https_fn.Response("OK", status=200)

