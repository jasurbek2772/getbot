import os
import re
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
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaGeoLive,
    MessageMediaContact,
    MessageMediaPoll,
    MessageMediaDice,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ================= ENV =================
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
MY_CHAT_ID     = int(os.environ["MY_CHAT_ID"])
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SESSION_STRING = "".join(os.environ["SESSION_STRING"].split()).strip("\"'")

_target = os.environ["TARGET_USER_ID"].strip()
TARGET  = int(_target) if _target.lstrip("-").isdigit() else _target

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CONNECT_TIMEOUT = 20
JOB_TIMEOUT     = 55

INTRO = (
    "👋 Привет!\n\n"
    "Я скачиваю материалы целевого пользователя через твой аккаунт "
    "в оригинальном качестве.\n\n"
    "Нажми кнопку:\n"
    "• 🖼 Аватар: все фото профиля\n"
    "• 🎥 Аватар (гиф/видео): видеоаватарка\n"
    "• 📌 Закреп сторис: закреплённые истории\n"
    "• 📲 Текущие сторис: активные истории\n\n"
    "Или просто скинь ссылку на пост из канала — я скачаю его.\n"
    "Поддерживаются приватные и публичные каналы.\n\n"
    "/status — проверка сессии."
)

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🖼 Скачать аватар",          "callback_data": "avatar"}],
        [{"text": "🎥 Скачать аватар (гиф)",     "callback_data": "avatar_video"}],
        [{"text": "📌 Скачать закреп сторис",    "callback_data": "stories_pinned"}],
        [{"text": "📲 Скачать текущие сторис",   "callback_data": "stories_current"}],
    ]
}

TITLES = {
    "avatar":          "🖼 Скачивание аватара",
    "avatar_video":    "🎥 Скачивание видеоаватарки",
    "stories_pinned":  "📌 Скачивание закрепов",
    "stories_current": "📲 Скачивание текущих сторис",
    "status":          "🔎 Проверка",
}


# ================= BOT API HELPERS =================
async def bot_call(http, method, data=None, files=None):
    try:
        r   = await http.post(f"{BOT_API}/{method}", data=data,
                              files=files, timeout=60)
        res = r.json()
    except Exception as e:
        res = {"ok": False, "description": f"{type(e).__name__}: {e}"}
    if not res.get("ok"):
        log.error("Bot API %s error: %s", method, res)
    return res


async def send_text(http, text, keyboard=None):
    data = {"chat_id": MY_CHAT_ID, "text": str(text)[:4096]}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    return await bot_call(http, "sendMessage", data=data)


async def send_file(http, method, field, raw, filename,
                    caption="", extra=None):
    d = {"chat_id": MY_CHAT_ID}
    if caption:
        d["caption"] = caption[:1024]
    if extra:
        d.update(extra)
    return await bot_call(http, method, data=d,
                          files={field: (filename, raw)})


async def send_doc(http, raw, filename, caption=""):
    return await send_file(http, "sendDocument", "document",
                           raw, filename, caption=caption,
                           extra={"disable_content_type_detection": "true"})


class Status:
    def __init__(self, http, title):
        self.http  = http
        self.title = title
        self.lines = []
        self.mid   = None
        self.t0    = time.monotonic()

    async def add(self, line):
        elapsed = time.monotonic() - self.t0
        self.lines.append(f"[{elapsed:4.1f}с] {line}")
        text = self.title + "\n\n" + "\n".join(self.lines[-15:])
        if self.mid is None:
            res = await bot_call(
                self.http, "sendMessage",
                data={"chat_id": MY_CHAT_ID, "text": text},
            )
            self.mid = (res.get("result") or {}).get("message_id")
        else:
            await bot_call(
                self.http, "editMessageText",
                data={"chat_id": MY_CHAT_ID,
                      "message_id": self.mid, "text": text},
            )


# ================= TELETHON HELPERS =================
def make_client():
    return TelegramClient(
        StringSession(SESSION_STRING),
        API_ID, API_HASH,
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
    await st.add(f"✔ Найден: {user.first_name or '-'}")
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


# ================= LINK PARSER =================
def parse_tg_link(text: str):
    """
    Возвращает (channel, message_id) или None.

    Приватный:  t.me/c/1234567890/99  → channel = -1001234567890
    Публичный:  t.me/username/99      → channel = "username"
    """
    # убираем возможный query string и пробелы
    text = text.strip().split("?")[0]

    # приватный канал: /c/числовой_id/msg_id
    m = re.search(r"t\.me/c/(\d+)/(\d+)", text)
    if m:
        channel_id = int("-100" + m.group(1))
        msg_id     = int(m.group(2))
        return channel_id, msg_id

    # публичный канал: /username/msg_id
    m = re.search(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,})/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))

    return None


# ================= FORWARD MEDIA =================
async def forward_media(client, msg, http, st):
    """Скачивает и пересылает одно сообщение."""
    text  = msg.text or msg.message or ""
    label = f"пост #{msg.id}"

    if not msg.media:
        await send_text(http, f"📄 {label}\n\n{text}" if text else f"📄 {label} (только текст)")
        await st.add("✔ Текстовый пост отправлен")
        return

    await st.add("⬇ Скачиваю медиа...")
    try:
        raw = await client.download_media(msg.media, file=bytes)
    except Exception as e:
        await st.add(f"❌ Не удалось скачать: {e}")
        return

    if not raw:
        await st.add("❌ Пустой файл")
        return

    size_kb = len(raw) // 1024
    await st.add(f"⬆ Отправляю ({size_kb} КБ)...")

    cap   = (f"📢 {label}\n\n{text}").strip()
    media = msg.media

    # Фото
    if isinstance(media, MessageMediaPhoto):
        res = await send_file(http, "sendPhoto", "photo",
                              raw, "photo.jpg", caption=cap)
        if not res.get("ok"):
            await send_doc(http, raw, "photo.jpg", caption=cap)
        await st.add("✔ Фото отправлено")
        return

    # Документ (видео, голосовое, кружок, музыка, gif, файл)
    if isinstance(media, MessageMediaDocument):
        doc   = media.document
        mime  = getattr(doc, "mime_type", "") or ""
        attrs = doc.attributes

        is_round = any(isinstance(a, DocumentAttributeVideo)
                       and a.round_message for a in attrs)
        is_voice = any(isinstance(a, DocumentAttributeAudio)
                       and a.voice for a in attrs)
        is_audio = any(isinstance(a, DocumentAttributeAudio)
                       and not a.voice for a in attrs)
        is_anim  = any(isinstance(a, DocumentAttributeAnimated)
                       for a in attrs)
        is_video = any(isinstance(a, DocumentAttributeVideo)
                       and not a.round_message for a in attrs)

        fname = next(
            (a.file_name for a in attrs
             if isinstance(a, DocumentAttributeFilename) and a.file_name),
            "file",
        )

        if is_round:
            await send_file(http, "sendVideoNote", "video_note",
                            raw, "circle.mp4")
            if text:
                await send_text(http, cap)
            await st.add("✔ Кружок отправлен")
            return

        if is_voice:
            await send_file(http, "sendVoice", "voice",
                            raw, "voice.ogg", caption=cap)
            await st.add("✔ Голосовое отправлено")
            return

        if is_audio:
            a     = next(x for x in attrs
                         if isinstance(x, DocumentAttributeAudio))
            extra = {}
            if getattr(a, "title",     None): extra["title"]     = a.title
            if getattr(a, "performer", None): extra["performer"] = a.performer
            if getattr(a, "duration",  None): extra["duration"]  = str(int(a.duration))
            await send_file(http, "sendAudio", "audio",
                            raw, fname, caption=cap, extra=extra)
            await st.add("✔ Аудио отправлено")
            return

        if is_anim or mime == "image/gif":
            await send_file(http, "sendAnimation", "animation",
                            raw, "anim.gif", caption=cap)
            await st.add("✔ GIF отправлен")
            return

        if is_video or mime.startswith("video/"):
            va    = next((x for x in attrs
                          if isinstance(x, DocumentAttributeVideo)), None)
            extra = {"supports_streaming": "true"}
            if va:
                if getattr(va, "w",        None): extra["width"]    = str(va.w)
                if getattr(va, "h",        None): extra["height"]   = str(va.h)
                if getattr(va, "duration", None): extra["duration"] = str(int(va.duration))
            res = await send_file(http, "sendVideo", "video",
                                  raw, fname, caption=cap, extra=extra)
            if not res.get("ok"):
                await send_doc(http, raw, fname, caption=cap)
            await st.add("✔ Видео отправлено")
            return

        # Прочий файл
        await send_doc(http, raw, fname, caption=cap)
        await st.add("✔ Файл отправлен")
        return

    # Геолокация
    if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive)):
        geo = media.geo
        if hasattr(geo, "lat"):
            await bot_call(http, "sendLocation", data={
                "chat_id":   MY_CHAT_ID,
                "latitude":  str(geo.lat),
                "longitude": str(geo.long),
            })
        if text:
            await send_text(http, cap)
        await st.add("✔ Геолокация отправлена")
        return

    # Опрос
    if isinstance(media, MessageMediaPoll):
        q    = getattr(media.poll, "question", "?")
        opts = [getattr(a, "text", "?")
                for a in getattr(media.poll, "answers", [])]
        await send_text(http,
            f"📢 {label}\n📊 Опрос: {q}\n" +
            "\n".join(f"• {o}" for o in opts))
        await st.add("✔ Опрос отправлен")
        return

    # Неизвестный тип
    await send_text(http, f"📢 {label}\n[{type(media).__name__}]\n{text}")
    await st.add(f"⚠ Неизвестный тип: {type(media).__name__}")


# ================= JOB: LINK =================
async def job_link(client, http, st, channel, msg_id):
    await st.add(f"🔍 Ищу канал {channel}...")
    try:
        entity = await client.get_entity(channel)
    except ValueError:
        await st.add("↻ Нет в кэше, загружаю диалоги...")
        await client.get_dialogs()
        entity = await client.get_entity(channel)

    title = getattr(entity, "title", str(channel))
    await st.add(f"✔ Канал: {title}")
    await st.add(f"📥 Получаю пост #{msg_id}...")

    fetched = await client.get_messages(entity, ids=msg_id)
    msg = fetched if not isinstance(fetched, list) else (fetched[0] if fetched else None)

    if not msg:
        await st.add("❌ Пост не найден.")
        return

    # ── DEBUG: показываем всё что есть в сообщении ──
    await st.add(f"media: {type(msg.media).__name__}")
    await st.add(f"grouped_id: {msg.grouped_id}")

    # Все нестандартные атрибуты
    interesting = []
    for attr in vars(msg):
        if attr.startswith("_"):
            continue
        val = getattr(msg, attr, None)
        if val is not None and val is not False and val != [] and val != "":
            interesting.append(f"{attr}: {type(val).__name__}")
    await st.add("Атрибуты:\n" + "\n".join(interesting))

    # Если media есть — показываем его атрибуты
    if msg.media:
        media_attrs = []
        for attr in vars(msg.media):
            if attr.startswith("_"):
                continue
            val = getattr(msg.media, attr, None)
            if val is not None and val is not False and val != []:
                media_attrs.append(f"  media.{attr}: {type(val).__name__} = {str(val)[:60]}")
        await st.add("Media атрибуты:\n" + "\n".join(media_attrs))




# ================= JOBS (кнопки) =================
async def job_status(client, http, st):
    me = await client.get_me()
    await st.add(f"✔ Сессия: {me.first_name} (id {me.id})")
    await get_target(client, st)
    info = await bot_call(http, "getWebhookInfo")
    wh   = info.get("result", {})
    await st.add(f"Webhook: {wh.get('url')}")
    await st.add(f"last_error: {wh.get('last_error_message', '-')}")
    await st.add(f"pending: {wh.get('pending_update_count')}")


async def job_avatar(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Получаю список фото...")
    photos = await client.get_profile_photos(peer)
    if not photos:
        await st.add("⚠ Нет фото профиля.")
        return
    await st.add(f"✔ Найдено: {len(photos)}")
    await send_text(http, user_info(user, f"🖼 Фото профиля (всего: {len(photos)})"))
    for i, p in enumerate(photos, start=1):
        await st.add(f"⬇ {i}/{len(photos)}...")
        try:
            data = await client.download_media(p, file=bytes)
        except RPCError as e:
            await st.add(f"❌ Фото {i}: {e}")
            continue
        if not data:
            await st.add(f"❌ Фото {i}: пустой файл")
            continue
        await st.add(f"⬆ Отправляю {i}/{len(photos)} ({len(data)//1024} КБ)...")
        res = await send_file(http, "sendPhoto", "photo",
                              data, f"profile_{i}.jpg",
                              caption=f"🖼 Фото {i}/{len(photos)}")
        if not res.get("ok"):
            await send_doc(http, data, f"profile_{i}.jpg",
                           caption=f"🖼 Фото {i}/{len(photos)}")
        await asyncio.sleep(0.5)


async def job_avatar_video(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Проверяю видеоаватарку...")
    full        = await client(GetFullUserRequest(id=peer))
    photo       = getattr(full.full_user, "profile_photo", None)
    video_sizes = getattr(photo, "video_sizes", None)
    if not photo or not video_sizes:
        await st.add("⚠ Нет видеоаватарки.")
        return
    best = max(video_sizes, key=lambda v: getattr(v, "w", 0) or 0)
    await st.add(f"⬇ Скачиваю ({getattr(best,'w','?')}x{getattr(best,'h','?')})...")
    data = await client.download_media(photo, file=bytes, thumb=best)
    if not data:
        await st.add("❌ Не удалось скачать.")
        return
    await st.add(f"⬆ Отправляю ({len(data)//1024} КБ)...")
    await send_text(http, user_info(user, "🎥 Видеоаватарка"))
    res = await send_file(http, "sendVideo", "video",
                          data, "profile_video.mp4",
                          caption="🎥 Видеоаватарка",
                          extra={"supports_streaming": "true"})
    if not res.get("ok"):
        await send_doc(http, data, "profile_video.mp4", caption="🎥 Видеоаватарка")


async def _send_stories(client, http, st, stories, prefix):
    media = [s for s in stories if getattr(s, "media", None)]
    if not media:
        await st.add("⚠ Нет доступных историй.")
        return
    await st.add(f"✔ Найдено: {len(media)}")
    for n, story in enumerate(media, start=1):
        await st.add(f"⬇ {n}/{len(media)} (id {story.id})...")
        try:
            data = await client.download_media(story.media, file=bytes)
        except RPCError as e:
            await st.add(f"❌ Story {story.id}: {e}")
            continue
        if not data:
            await st.add(f"❌ Story {story.id}: пустой файл")
            continue
        ext = "mp4" if hasattr(story.media, "document") else "jpg"
        await st.add(f"⬆ Отправляю ({len(data)//1024} КБ)...")
        await send_doc(http, data, f"story_{story.id}.{ext}",
                       caption=f"{prefix} Story {story.id}")
        await asyncio.sleep(1)


async def job_stories_current(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Запрашиваю текущие сторис...")
    try:
        result = await client(GetPeerStoriesRequest(peer=peer))
    except RPCError as e:
        await st.add(f"❌ {e}")
        return
    stories = getattr(getattr(result, "stories", None), "stories", None) or []
    await send_text(http, user_info(user, "📲 Текущие сторис"))
    await _send_stories(client, http, st, stories, "📲")


async def job_stories_pinned(client, http, st):
    peer, user = await get_target(client, st)
    await st.add("📋 Запрашиваю закреплённые сторис...")
    try:
        result = await client(GetPinnedStoriesRequest(peer=peer, offset_id=0, limit=100))
    except RPCError as e:
        await st.add(f"❌ {e}")
        return
    stories = getattr(result, "stories", None) or []
    await send_text(http, user_info(user, "📌 Закреплённые сторис"))
    await _send_stories(client, http, st, stories, "📌")


JOBS = {
    "avatar":          job_avatar,
    "avatar_video":    job_avatar_video,
    "stories_pinned":  job_stories_pinned,
    "stories_current": job_stories_current,
    "status":          job_status,
}


# ================= RUNNER =================
async def run_job(action, http, extra=None):
    st     = Status(http, TITLES.get(action, action))
    client = None
    await st.add("▶ Запуск")
    try:
        if len(SESSION_STRING) < 100:
            await st.add(f"❌ SESSION_STRING слишком короткий ({len(SESSION_STRING)} символов).")
            return

        client = make_client()
        await st.add("🔌 Подключаюсь...")
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)

        if not await client.is_user_authorized():
            await st.add("❌ SESSION_STRING недействителен. Сгенерируй заново.")
            return
        await st.add("✔ Подключено")

        if action == "link":
            channel, msg_id = extra
            await asyncio.wait_for(
                job_link(client, http, st, channel, msg_id), JOB_TIMEOUT
            )
        else:
            await asyncio.wait_for(JOBS[action](client, http, st), JOB_TIMEOUT)

        await st.add("✅ Готово")

    except asyncio.TimeoutError:
        await st.add("⏱ Превышено время ожидания (55 с).")
    except Exception as e:
        log.error(traceback.format_exc())
        tb = traceback.format_exc().strip().splitlines()[-3:]
        await st.add(f"❌ {type(e).__name__}: {e}")
        await st.add("\n".join(tb))
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        await send_text(http, "Выбери действие:", keyboard=KEYBOARD)


# ================= UPDATE PROCESSING =================
async def process_update(update):
    async with httpx.AsyncClient() as http:

        # --- кнопка ---
        cq = update.get("callback_query")
        if cq:
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
            from_id = (cq.get("from") or {}).get("id")
            action  = cq.get("data")

            if MY_CHAT_ID not in (chat_id, from_id):
                await bot_call(http, "answerCallbackQuery",
                               data={"callback_query_id": cq["id"],
                                     "text": "⛔ Нет доступа"})
                return

            if action not in JOBS:
                await bot_call(http, "answerCallbackQuery",
                               data={"callback_query_id": cq["id"],
                                     "text": "Неизвестная кнопка"})
                return

            await bot_call(http, "answerCallbackQuery",
                           data={"callback_query_id": cq["id"],
                                 "text": "⏳ Принято..."})
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
            return

        if text.startswith("/status"):
            await run_job("status", http)
            return

        # --- ссылка на пост ---
        parsed = parse_tg_link(text)
        if parsed:
            channel, msg_id = parsed
            await run_job("link", http, extra=parsed)
            return

        await send_text(http,
            "Скинь ссылку на пост из канала или выбери действие:",
            keyboard=KEYBOARD)


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
        self._reply(200)
