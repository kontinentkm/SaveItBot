# SaveItBot (Telegram)

Telegram-бот на **Python + aiogram**, который принимает ссылку на Instagram (post/reel/story/highlight) и отправляет в чат скачанные фото/видео.

## Требования

- Python 3.10+

## Установка

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка

Скопируй `.env.example` → `.env` (или используй уже созданный `.env`) и вставь токен бота:

```env
BOT_TOKEN=...
```

## Cookies (очень важно для Instagram)

Instagram часто блокирует скачивание без логина. Самый стабильный способ — **экспортировать cookies** из браузера и указать путь к файлу.

1) Поставь расширение “Get cookies.txt (LOCALLY)” (Chrome/Edge)  
2) Зайди в `instagram.com` (в аккаунт)  
3) Экспортируй cookies в Netscape-формате, например: `cookies/instagram-cookies.txt`  
4) Укажи путь в `.env`:

```env
IG_COOKIES_FILE=cookies/instagram-cookies.txt
```

## Запуск

```bash
python -m bot.main
```

## Использование

Просто отправь боту ссылку вида:

- `https://www.instagram.com/p/.../`
- `https://www.instagram.com/reel/.../`
- `https://www.instagram.com/stories/...`
- `https://www.instagram.com/stories/highlights/...`

Бот скачает медиа и отправит в чат. Если медиа больше 10 — отправит **несколькими альбомами** (лимит Telegram: 10 вложений на альбом).

