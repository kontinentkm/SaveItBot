import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from dotenv import load_dotenv
from yt_dlp import YoutubeDL


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
    # Strip trailing punctuation often added by messengers
    return m.group(1).rstrip(").,]")


def _classify_files(files: List[Path]) -> Tuple[List[Path], List[Path]]:
    photos: List[Path] = []
    videos: List[Path] = []
    for f in files:
        ext = f.suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            photos.append(f)
        elif ext in {".mp4", ".mov", ".m4v", ".webm"}:
            videos.append(f)
    return photos, videos


def _collect_media_files(dir_path: Path) -> List[Path]:
    # yt-dlp can create nested files depending on output template; keep it simple:
    files = [p for p in dir_path.rglob("*") if p.is_file()]
    # Telegram album limit: up to 10 items per group
    return sorted(files, key=lambda p: p.name)


def _chunked(items: List[Path], size: int) -> Iterable[List[Path]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def download_instagram_media(url: str, cookies_file: Optional[str], timeout_s: int) -> Tuple[Path, List[Path]]:
    """
    Downloads media from an Instagram URL using yt-dlp (posts/reels/stories/highlights).
    Returns (tmpdir, list_of_downloaded_media_files).
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
        # Prefer mp4 where possible
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        files = _collect_media_files(tmpdir)
        # Filter only typical media formats (ignore .part, .json, etc.)
        media_files: List[Path] = []
        for f in files:
            if f.suffix.lower() in MEDIA_EXTS:
                media_files.append(f)
        return tmpdir, media_files
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


async def send_albums(message: Message, files: List[Path]) -> None:
    if not files:
        await message.answer("Не удалось найти медиа по этой ссылке.")
        return

    for part_idx, batch in enumerate(_chunked(files, TELEGRAM_ALBUM_LIMIT), start=1):
        # Telegram lets you send media group with mixed photo/video
        media = []
        for f in batch:
            data = f.read_bytes()
            input_file = BufferedInputFile(data, filename=f.name)
            ext = f.suffix.lower()
            if ext in {".mp4", ".mov", ".m4v", ".webm"}:
                media.append(InputMediaVideo(media=input_file))
            else:
                media.append(InputMediaPhoto(media=input_file))

        if len(media) == 1:
            item = media[0]
            if isinstance(item, InputMediaVideo):
                await message.answer_video(item.media)
            else:
                await message.answer_photo(item.media)
        else:
            # For big carousels, we just send multiple albums sequentially.
            if len(files) > TELEGRAM_ALBUM_LIMIT:
                await message.answer(f"Часть {part_idx}/{(len(files) + TELEGRAM_ALBUM_LIMIT - 1) // TELEGRAM_ALBUM_LIMIT}")
            await message.answer_media_group(media)


async def cleanup_dirs(dirs: List[Path]) -> None:
    # best-effort cleanup
    for d in dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


async def main() -> None:
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Put it into .env")

    cookies_file = os.getenv("IG_COOKIES_FILE") or None
    if cookies_file:
        cookies_file = str(Path(cookies_file).expanduser().resolve())
        if not Path(cookies_file).exists():
            # Don't crash on missing cookies; just continue without auth.
            cookies_file = None

    timeout_s = _get_env_int("DOWNLOAD_TIMEOUT", 120)

    bot = Bot(token=token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Пришли ссылку на Instagram (post/reel/story/highlight). Я скачаю фото/видео и отправлю сюда.\n\n"
            "Если Instagram не даёт скачивать без логина — добавь cookies в IG_COOKIES_FILE."
        )

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        url = _extract_instagram_url(message.text or "")
        if not url:
            await message.answer("Пришли, пожалуйста, ссылку на Instagram (post/reel/story/highlight).")
            return

        status = await message.answer("Скачиваю…")
        tmpdirs: List[Path] = []
        try:
            # Download in thread to not block event loop
            tmpdir, files = await asyncio.to_thread(download_instagram_media, url, cookies_file, timeout_s)
            tmpdirs.append(tmpdir)

            await status.edit_text(f"Нашёл {len(files)} файл(ов). Отправляю…")
            await send_albums(message, files)
            await status.delete()
        except Exception as e:
            await status.edit_text(
                "Не получилось скачать по этой ссылке.\n\n"
                f"<b>Ошибка</b>: <code>{type(e).__name__}</code>\n"
                "Частая причина: Instagram требует авторизацию — добавь cookies (см. README)."
            )
        finally:
            await cleanup_dirs(tmpdirs)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

