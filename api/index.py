import os
import json
import time
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

CONNECT_TIMEOUT = 20   # сек на подключение к Telegram
JOB_TIMEOUT = 50       # сек на всю задачу (maxDuration в vercel.json = 60)

INTRO = (
    "👋 Привет!\n\n"
    "Я скачиваю материалы целевого пользователя через твой аккаунт "
    "в оригинальном качестве.\n\n"
    "Нажми кнопку, и результат придёт сюда:\n"
    "• 🖼 Аватар: все фото профиля\n"
    "• 🎥 Аватар (гиф/видео): видеоаватарка, если есть\n"
    "• 📌 Закреп сторис: закреплённые истории\n"
    "• 📲 Текущие сторис: активные истории\n\n"
    "Во время работы я пишу статус по шагам.\n"
    "/status: проверка сессии и webhook.\n"
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

TITLES = {
    "avatar": "🖼 Скачивание аватара",
    "avatar_video": "🎥 Скачивание видеоаватарки",
    "stories_pinned": "📌 Скачивание закрепов",
    "stories_current": "📲 Скачивание текущих сторис",
    "status": "🔎 Проверка",
}


# ================= BOT API HELPERS =================
async def bot_call(http, method, data=None, files=None):
    try:
        r = await http.post(f"{BOT_API}/{method}", data=data, files=files, timeout=60)
        res = r.json()
    except Exception as e:
        res = {"ok": False, "description": f"{type(e).__name__}: {e}"}
    if not res.get("ok"):
        log.error("Bot API %s error: %s", method, res)
    return res


async def send_text(http, text, keyboard=None):
    data = {"chat_id": MY_CHAT_ID, "text": text[:4000]}
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


class Status:
    """Одно сообщение в чате, которое редактируется по ходу работы."""

    def __init__(self, http, title):
        self.http = http
        self.title = title
        self.lines = []
        self.mid = None
        self.t0 = time.monotonic()

    async def add(self, line):
        elapsed = time.monotonic() - self.t0
        self.lines.append(f"[{elapsed:4.1f}с] {line}")
        text = self.title + "\n\n" + "\n".join(self.lines[-15:])
        if self.mid is None:
            res = await bot_call(
                self.http, "sendMessage", data={"chat_id": MY_CHAT_ID, "text": text}
            )
            self.mid = (res.get("result") or {}).get("message_id")
        else:
            await bot_call(
                self.http,
                "editMessageText",
                data={"chat_id": MY_CHAT_ID, "message_id": self.mid, "text": text},
            )


# ================= TELETHON HELPERS =================
def make_client():
    return TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        receive_updates=False,
    )


async def get_target(client, st):
    await st.add("🔍 Ищу целевого пользователя...")
    try:
        peer = await client.get_input_entity(TARGET)
    except ValueError:
        await st.add("↻ Нет в кэше, загружаю диалоги...")
        await client.get_dialogs()
        peer = await client.get_input_entity(TARGET)
    user = await client.get_entity(peer)
    await st.add(f"✔ Пользователь найден: {user.first_name or '-'}")
    return peer, user


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
async def job_status(client, http, st):
    me = await client.get_me()
    await st.add(f"✔ Сессия: {me.first_name} (id {me.id}, бот: {me.bot})")
    await get_target(client, st)
    info = await bot_call(http, "getWebhookInfo")
    wh = info.get("result", {})
    await st.add(f"Webhook: {wh.get('url')}")
    await st.add(f"allowed_updates: {wh.get('allowed_updates', 'по умолчанию')}")
    await st.add(f"pending: {wh.get('pending_update_count')}")
    await st.add(f"last_error: {wh.get('last_error_message', '-')}")


async def job_avatar(client, http, st):
    peer, user = await get_target(client, st)

    await st.add("📋 Получаю список фото профиля...")
    photos = await client.get_profile_photos(peer)
    if not photos:
        await st.add("⚠ У пользователя нет фото профиля.")
        return
    await st.add(f"✔ Найдено фото: {len(photos)}")
    await send_text(http, user_info(user, f"🖼 Фото профиля (всего: {len(photos)})"))

    for i, p in enumerate(photos, start=1):
        await st.add(f"⬇ Скачиваю {i}/{len(photos)}...")
        try:
            data = await client.download_media(p, file=bytes)
        except RPCError as e:
            await st.add(f"❌ Фото {i}: ошибка Telegram ({e})")
            continue
        if not data:
            await st.add(f"❌ Фото {i}: не удалось скачать")
            continue
        await st.add(f"⬆ Отправляю {i}/{len(photos)} ({len(data) // 1024} КБ)...")
        res = await send_file(http, f"profile_{i}.jpg", data, caption=f"🖼 Фото {i}/{len(photos)}")
        if not res.get("ok"):
            await st.add(f"❌ Фото {i}: не отправилось ({res.get('description')})")
        await asyncio.sleep(0.5)


async def job_avatar_video(client, http, st):
    peer, user = await get_target(client, st)

    await st.add("📋 Проверяю видеоаватарку...")
    full = await client(GetFullUserRequest(id=peer))
    photo = getattr(full.full_user, "profile_photo", None)
    video_sizes = getattr(photo, "video_sizes", None)

    if not photo or not video_sizes:
        await st.add("⚠ У пользователя нет видеоаватарки (гиф).")
        return

    best = max(video_sizes, key=lambda v: getattr(v, "w", 0) or 0)
    await st.add(f"⬇ Скачиваю видео ({getattr(best, 'w', '?')}x{getattr(best, 'h', '?')})...")
    data = await client.download_media(photo, file=bytes, thumb=best)
    if not data:
        await st.add("❌ Не удалось скачать видеоаватарку.")
        return

    await st.add(f"⬆ Отправляю ({len(data) // 1024} КБ)...")
    await send_text(http, user_info(user, "🎥 Видеоаватарка"))
    res = await send_file(http, "profile_video.mp4", data, caption="🎥 Видеоаватарка", as_video=True)
    if not res.get("ok"):
        await st.add(f"❌ Не отправилось ({res.get('description')})")


async def _send_stories(client, http, st, stories, title, prefix):
    media = [s for s in stories if getattr(s, "media", None)]
    if not media:
        await st.add("⚠ Нет доступных историй.")
        return

    await st.add(f"✔ Найдено историй: {len(media)}")

    for n, story in enumerate(media, start=1):
        await st.add(f"⬇ Скачиваю {n}/{len(media)} (id {story.id})...")
        try:
            data = await client.download_media(story.media, file=bytes)
        except RPCError as e:
            await st.add(f"❌ Story {story.id}: ошибка Telegram ({e})")
            continue
        if not data:
            await st.add(f"❌ Story {story.id}: не удалось скачать")
            continue

        ext = "mp4" if hasattr(story.media, "document") else "jpg"
        await st.add(f"⬆ Отправляю {n}/{len(media)} ({len(data) // 1024} КБ)...")
        res = await send_file(
            http, f"story_{story.id}.{ext}", data, caption=f"{prefix} Story {story.id}"
        )
        if not res.get("ok"):
            await st.add(
                f"❌ Story {story.id}: не отправилось "
                f"({res.get('description', 'возможно, файл больше 50 МБ')})"
            )
        await asyncio.sleep(1)


async def job_stories_current(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Запрашиваю текущие сторис...")
    try:
        result = await client(GetPeerStoriesRequest(peer=peer))
    except RPCError as e:
        await st.add(f"❌ Ошибка Telegram: {e}")
        return
    stories = getattr(getattr(result, "stories", None), "stories", None) or []
    await send_text(http, user_info(user, "📲 Текущие сторис"))
    await _send_stories(client, http, st, stories, "📲 Текущие сторис", "📲")


async def job_stories_pinned(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Запрашиваю закреплённые сторис...")
    try:
        result = await client(GetPinnedStoriesRequest(peer=peer, offset_id=0, limit=100))
    except RPCError as e:
        await st.add(f"❌ Ошибка Telegram: {e}")
        return
    stories = getattr(result, "stories", None) or []
    await send_text(http, user_info(user, "📌 Закреплённые сторис"))
    await _send_stories(client, http, st, stories, "📌 Закреплённые сторис", "📌")


JOBS = {
    "avatar": job_avatar,
    "avatar_video": job_avatar_video,
    "stories_pinned": job_stories_pinned,
    "stories_current": job_stories_current,
    "status": job_status,
}


# ================= RUNNER =================
async def run_job(action, http):
    st = Status(http, TITLES.get(action, action))
    await st.add("▶ Запуск")
    client = make_client()
    try:
        await st.add("🔌 Подключаюсь к Telegram...")
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)

        if not await client.is_user_authorized():
            await st.add("❌ SESSION_STRING недействителен. Сгенерируй заново и обнови в Vercel.")
            return
        await st.add("✔ Подключено")

        await asyncio.wait_for(JOBS[action](client, http, st), JOB_TIMEOUT)
        await st.add("✅ Готово")

    except asyncio.TimeoutError:
        await st.add(
            "⏱ Превышено время ожидания. Либо Telegram отвечает медленно, "
            "либо файлы слишком большие для лимита функции Vercel."
        )
    except Exception as e:
        log.error(traceback.format_exc())
        tb = traceback.format_exc().strip().splitlines()[-3:]
        await st.add(f"❌ Ошибка: {type(e).__name__}: {e}")
        await st.add("Детали:\n" + "\n".join(tb))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await send_text(http, "Выбери действие:", keyboard=KEYBOARD)


async def process_update(update):
    async with httpx.AsyncClient() as http:
        # --- нажатие кнопки ---
        cq = update.get("callback_query")
        if cq:
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
            from_id = (cq.get("from") or {}).get("id")
            action = cq.get("data")
            log.info("callback: action=%s chat=%s from=%s", action, chat_id, from_id)

            if MY_CHAT_ID not in (chat_id, from_id):
                await bot_call(http, "answerCallbackQuery",
                               data={"callback_query_id": cq["id"], "text": "⛔ Нет доступа"})
                return

            if action not in JOBS:
                await bot_call(http, "answerCallbackQuery",
                               data={"callback_query_id": cq["id"], "text": "Неизвестная кнопка"})
                return

            await bot_call(http, "answerCallbackQuery",
                           data={"callback_query_id": cq["id"], "text": "⏳ Принято, работаю..."})
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
        elif text.startswith("/status"):
            await run_job("status", http)
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
            log.info("update keys: %s", list(update.keys()))
            asyncio.run(process_update(update))
        except Exception:
            log.error(traceback.format_exc())
        # всегда 200, чтобы Telegram не слал повторы
        self._reply(200)
