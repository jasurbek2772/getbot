import os
import io
import json
import asyncio
import logging
import traceback
from http.server import BaseHTTPRequestHandler

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import RPCError
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.stories import (
    GetPeerStoriesRequest,
    GetPinnedStoriesRequest,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ================= ENV =================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MY_CHAT_ID = int(os.environ["MY_CHAT_ID"])
SESSION_STRING = os.environ["SESSION_STRING"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

_target = os.environ["TARGET_USER_ID"].strip()
TARGET = int(_target) if _target.lstrip("-").isdigit() else _target

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

INTRO = (
    "👋 Привет!\n\n"
    "Я скачиваю материалы целевого пользователя через твой аккаунт "
    "в оригинальном качестве.\n\n"
    "Нажми кнопку, и результат придёт сюда:\n"
    "• 🖼 Аватар: все фото профиля\n"
    "• 🎥 Аватар (гиф/видео): видеоаватарка, если есть\n"
    "• 📌 Закреп сторис: закреплённые истории\n"
    "• 📲 Текущие сторис: активные истории\n\n"
    "Файлы приходят документами, чтобы Telegram не сжимал качество."
)

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🖼 Скачать аватар", "callback_data": "avatar"}],
        [{"text": "🎥 Скачать аватар (гиф)", "callback_data": "avatar_video"}],
        [{"text": "📌 Скачать закреп сторис", "callback_data": "stories_pinned"}],
        [{"text": "📲 Скачать текущие сторис", "callback_data": "stories_current"}],
    ]
}


# ================= BOT API HELPERS =================
async def bot_call(http, method, data=None, files=None):
    r = await http.post(f"{BOT_API}/{method}", data=data, files=files, timeout=120)
    try:
        res = r.json()
    except Exception:
        res = {"ok": False, "description": r.text[:200]}
    if not res.get("ok"):
        log.error("Bot API %s error: %s", method, res)
    return res


async def send_text(http, text, keyboard=None):
    data = {"chat_id": MY_CHAT_ID, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    return await bot_call(http, "sendMessage", data=data)


async def send_file(http, name, content: bytes, caption="", as_video=False):
    if as_video:
        method, field = "sendVideo", "video"
        data = {"chat_id": MY_CHAT_ID, "caption": caption, "supports_streaming": "true"}
    else:
        method, field = "sendDocument", "document"
        data = {
            "chat_id": MY_CHAT_ID,
            "caption": caption,
            "disable_content_type_detection": "true",
        }
    return await bot_call(http, method, data=data, files={field: (name, content)})


# ================= TELETHON HELPERS =================
def make_client():
    return TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        receive_updates=False,
    )


async def get_target(client):
    try:
        return await client.get_input_entity(TARGET)
    except ValueError:
        # в StringSession нет кэша — подгружаем диалоги и пробуем снова
        await client.get_dialogs()
        return await client.get_input_entity(TARGET)


def user_info(user, title):
    username = f"@{user.username}" if user.username else "-"
    return (
        f"{title}\n\n"
        f"Имя: {user.first_name or '-'}\n"
        f"Фамилия: {user.last_name or '-'}\n"
        f"Username: {username}\n"
        f"ID: {user.id}"
    )


# ================= JOBS =================
async def job_avatar(client, http):
    peer = await get_target(client)
    user = await client.get_entity(peer)
    photos = await client.get_profile_photos(peer)

    if not photos:
        await send_text(http, "У пользователя нет фото профиля.")
        return

    await send_text(http, user_info(user, f"🖼 Фото профиля (всего: {len(photos)})"))

    for i, p in enumerate(photos, start=1):
        try:
            data = await client.download_media(p, file=bytes)
        except RPCError as e:
            await send_text(http, f"Ошибка скачивания фото {i}: {e}")
            continue
        if not data:
            await send_text(http, f"Не удалось скачать фото {i}")
            continue
        await send_file(http, f"profile_{i}.jpg", data, caption=f"🖼 Фото {i}/{len(photos)}")
        await asyncio.sleep(0.5)


async def job_avatar_video(client, http):
    peer = await get_target(client)
    user = await client.get_entity(peer)
    full = await client(GetFullUserRequest(id=peer))

    photo = getattr(full.full_user, "profile_photo", None)
    video_sizes = getattr(photo, "video_sizes", None)

    if not photo or not video_sizes:
        await send_text(http, "У пользователя нет видеоаватарки (гиф).")
        return

    best = max(video_sizes, key=lambda v: getattr(v, "w", 0) or 0)
    data = await client.download_media(photo, file=bytes, thumb=best)

    if not data:
        await send_text(http, "Не удалось скачать видеоаватарку.")
        return

    await send_text(http, user_info(user, "🎥 Видеоаватарка"))
    await send_file(http, "profile_video.mp4", data, caption="🎥 Видеоаватарка", as_video=True)


async def _send_stories(client, http, stories, title, prefix):
    media = [s for s in stories if getattr(s, "media", None)]
    if not media:
        await send_text(http, f"{title}: нет доступных историй.")
        return

    await send_text(http, f"{title}: найдено {len(media)}")

    for story in media:
        try:
            data = await client.download_media(story.media, file=bytes)
        except RPCError as e:
            await send_text(http, f"Story {story.id}: ошибка скачивания ({e})")
            continue
        if not data:
            await send_text(http, f"Story {story.id}: не удалось скачать")
            continue

        ext = "mp4" if hasattr(story.media, "document") else "jpg"
        res = await send_file(
            http, f"story_{story.id}.{ext}", data, caption=f"{prefix} Story {story.id}"
        )
        if not res.get("ok"):
            await send_text(
                http,
                f"Story {story.id}: не удалось отправить "
                f"({res.get('description', 'возможно, файл больше 50 МБ')})",
            )
        await asyncio.sleep(1)


async def job_stories_current(client, http):
    peer = await get_target(client)
    user = await client.get_entity(peer)
    try:
        result = await client(GetPeerStoriesRequest(peer=peer))
    except RPCError as e:
        await send_text(http, f"Ошибка Telegram: {e}")
        return
    stories = getattr(getattr(result, "stories", None), "stories", None) or []
    await send_text(http, user_info(user, "📲 Текущие сторис"))
    await _send_stories(client, http, stories, "📲 Текущие сторис", "📲")


async def job_stories_pinned(client, http):
    peer = await get_target(client)
    user = await client.get_entity(peer)
    try:
        result = await client(GetPinnedStoriesRequest(peer=peer, offset_id=0, limit=100))
    except RPCError as e:
        await send_text(http, f"Ошибка Telegram: {e}")
        return
    stories = getattr(result, "stories", None) or []
    await send_text(http, user_info(user, "📌 Закреплённые сторис"))
    await _send_stories(client, http, stories, "📌 Закреплённые сторис", "📌")


JOBS = {
    "avatar": job_avatar,
    "avatar_video": job_avatar_video,
    "stories_pinned": job_stories_pinned,
    "stories_current": job_stories_current,
}


# ================= UPDATE PROCESSING =================
async def run_job(action, http):
    client = make_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await send_text(http, "❌ SESSION_STRING недействителен. Сгенерируй заново.")
            return
        await JOBS[action](client, http)
        await send_text(http, "✅ Готово", keyboard=KEYBOARD)
    except Exception as e:
        log.error(traceback.format_exc())
        await send_text(http, f"❌ Ошибка: {type(e).__name__}: {e}", keyboard=KEYBOARD)
    finally:
        await client.disconnect()


async def process_update(update):
    async with httpx.AsyncClient() as http:
        # --- нажатие кнопки ---
        cq = update.get("callback_query")
        if cq:
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            from_id = cq.get("from", {}).get("id")
            if MY_CHAT_ID not in (chat_id, from_id):
                return
            await bot_call(http, "answerCallbackQuery",
                           data={"callback_query_id": cq["id"], "text": "⏳ Работаю..."})
            action = cq.get("data")
            if action in JOBS:
                await run_job(action, http)
            return

        # --- сообщение ---
        msg = update.get("message")
        if not msg:
            return
        if msg.get("chat", {}).get("id") != MY_CHAT_ID:
            return

        text = (msg.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/menu"):
            await send_text(http, INTRO, keyboard=KEYBOARD)
        else:
            await send_text(http, "Выбери действие:", keyboard=KEYBOARD)


# ================= VERCEL HANDLER =================
class handler(BaseHTTPRequestHandler):
    def _reply(self, code=200, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, b"bot is running")

    def do_POST(self):
        if WEBHOOK_SECRET and (
            self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET
        ):
            self._reply(403, b"forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length) or b"{}")
            asyncio.run(process_update(update))
        except Exception:
            log.error(traceback.format_exc())
        # всегда 200, чтобы Telegram не слал повторы
        self._reply(200)
