import asyncio
import os
import io
import sqlite3
import logging
from datetime import datetime

import aiohttp
import edge_tts
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
WEATHER_KEY = os.getenv("WEATHER_API_KEY", "")
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}

OPENAI_MODEL = "gpt-4o"
VOICE_MALE = "ru-RU-DmitryNeural"
VOICE_FEMALE = "ru-RU-SvetlanaNeural"

ai = AsyncOpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None


def db_init():
    with sqlite3.connect("assistant.db") as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                voice TEXT DEFAULT 'male',
                city TEXT DEFAULT 'Минск');
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, title TEXT,
                deadline TEXT, status TEXT DEFAULT 'active',
                notified_1h INTEGER DEFAULT 0,
                notified_15m INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, role TEXT, content TEXT);
        """)


def db_user_get(tg_id):
    with sqlite3.connect("assistant.db") as c:
        row = c.execute("SELECT voice, city FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
        if not row:
            c.execute("INSERT INTO users (telegram_id) VALUES (?)", (tg_id,))
            c.commit()
            return "male", "Минск"
        return row


def db_user_set(tg_id, field, value):
    with sqlite3.connect("assistant.db") as c:
        c.execute(f"UPDATE users SET {field}=? WHERE telegram_id=?", (value, tg_id))
        c.commit()


def db_task_add(user_id, title, deadline):
    with sqlite3.connect("assistant.db") as c:
        c.execute("INSERT INTO tasks (user_id,title,deadline) VALUES (?,?,?)",
                  (user_id, title, deadline))
        c.commit()


def db_history_add(user_id, role, content):
    with sqlite3.connect("assistant.db") as c:
        c.execute("INSERT INTO history (user_id,role,content) VALUES (?,?,?)",
                  (user_id, role, content))
        c.commit()


def db_history_get(user_id, limit=10):
    with sqlite3.connect("assistant.db") as c:
        rows = c.execute("SELECT role,content FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, limit)).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]


SYSTEM_PROMPT = """Ты — персональный ИИ-ассистент топ-менеджера в Республике Беларусь.
Роли: бизнес-консультант, корпоративный юрист, HR-директор.

По вопросам труда, комплаенса и рисков опирайся СТРОГО на законодательство РБ:
• Трудовой кодекс РБ (Закон № 296-З от 26.07.1999)
• Уголовный кодекс РБ (Закон № 275-З от 09.07.1999)
• Декреты и Указы Президента РБ (Декрет №1, №8, Указ №2 и др.)
• Налоговый кодекс РБ

НЕ применяй законы РФ или других стран.
По юридическим вопросам ссылайся на конкретные статьи.
Если трактовка неоднозначна — добавь:
"⚠️ Дисклеймер: ответ носит информационный характер и не заменяет консультацию лицензированного юриста."
Всегда предлагай безопасный пошаговый алгоритм.
Отвечай кратко, структурно, для Telegram. Сегодня: {today}.
"""


async def voice_to_text(ogg_bytes: bytes) -> str:
    if not ai:
        return ""
    try:
        buf = io.BytesIO(ogg_bytes)
        buf.name = "voice.ogg"
        r = await ai.audio.transcriptions.create(model="whisper-1", file=buf, language="ru")
        return r.text.strip()
    except Exception as e:
        logging.error(f"STT: {e}")
        return ""


async def text_to_voice(text: str, voice_type: str) -> bytes | None:
    try:
        clean = "".join(ch for ch in text if ch.isalnum() or ch in " .,!?;:()-\n")
        voice = VOICE_MALE if voice_type == "male" else VOICE_FEMALE
        buf = io.BytesIO()
        async for chunk in edge_tts.Communicate(clean, voice, rate="+5%").stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()
    except Exception as e:
        logging.error(f"TTS: {e}")
        return None


async def smart_reply(msg: Message, text: str):
    await msg.answer(text)
    voice_type, _ = db_user_get(msg.from_user.id)
    audio = await text_to_voice(text, voice_type)
    if audio:
        await msg.answer_voice(voice=BufferedInputFile(audio, filename="reply.ogg"))


async def ai_consult(user_id: int, question: str) -> str:
    if not ai:
        return "⚠️ ИИ не настроен. Добавьте OPENAI_API_KEY."
    try:
        messages = [{"role": "system",
                     "content": SYSTEM_PROMPT.format(today=datetime.now().strftime("%d.%m.%Y"))}]
        messages += db_history_get(user_id)
        messages.append({"role": "user", "content": question})
        r = await ai.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, max_tokens=1500, temperature=0.4)
        return r.choices[0].message.content
    except Exception as e:
        return f"❌ Ошибка ИИ: {e}"


async def ai_structure(raw: str) -> str:
    if not ai:
        return raw
    try:
        r = await ai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user",
                       "content": f"Преврати диктовку в чистый деловой текст, исправь ошибки. Только результат:\n{raw}"}],
            max_tokens=500, temperature=0.2)
        return r.choices[0].message.content
    except Exception:
        return raw


async def weather_report(city: str) -> str:
    if not WEATHER_KEY:
        return "⚠️ Ключ погоды не настроен."
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.openweathermap.org/data/2.5/weather",
                             params={"q": city, "units": "metric", "lang": "ru",
                                     "appid": WEATHER_KEY}) as r:
                d = await r.json()
        t = round(d["main"]["temp"])
        desc = d["weather"][0]["description"]
        if t < 12:
            advice = "🧥 Возьмите куртку."
        elif t < 20:
            advice = "👔 Комфортно для костюма."
        else:
            advice = "👕 Лёгкий дресс-код."
        if "дожд" in desc:
            advice += " ☔ Зонт."
        return (f"🌤 Погода в г. {city}\n🌡 {t:+d}°C, {desc}\n"
                f"💨 {d['wind']['speed']} м/с · 💧 {d['main']['humidity']}%\n\n💡 {advice}")
    except Exception:
        return "❌ Не удалось получить погоду."


def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🧠 Консультация"), KeyboardButton(text="📋 Задачи")],
        [KeyboardButton(text="🌤 Погода"), KeyboardButton(text="⚙️ Настройки")],
    ], resize_keyboard=True)


def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской голос", callback_data="voice:male"),
         InlineKeyboardButton(text="👩 Женский голос", callback_data="voice:female")],
        [InlineKeyboardButton(text="🌍 Сменить город", callback_data="city:set")],
    ])


class TaskForm(StatesGroup):
    title = State()
    deadline = State()


class CityForm(StatesGroup):
    city = State()


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    if ALLOWED_IDS and msg.from_user.id not in ALLOWED_IDS:
        return await msg.answer("🔒 Доступ ограничен.")
    db_user_get(msg.from_user.id)
    await msg.answer(
        "🏛 <b>Помощник руководителя</b> активен.\n"
        "Консультирую по праву РБ, веду задачи, сообщаю погоду.",
        reply_markup=main_kb())
    voice_type, _ = db_user_get(msg.from_user.id)
    audio = await text_to_voice("Помощник руководителя активен. Выберите раздел.", voice_type)
    if audio:
        await msg.answer_voice(voice=BufferedInputFile(audio, filename="hello.ogg"))


@dp.message(F.voice)
async def on_voice(msg: Message):
    st = await msg.answer("🎧 Распознаю...")
    f = await bot.get_file(msg.voice.file_id)
    buf = bytearray()
    await bot.download_file(f.file_path, buf)
    raw = await voice_to_text(bytes(buf))
    if not raw:
        return await st.edit_text("❌ Не распознал голос.")
    structured = await ai_structure(raw)
    await st.edit_text(f"🎤 <b>Распознано:</b>\n{structured}")
    voice_type, _ = db_user_get(msg.from_user.id)
    audio = await text_to_voice(f"Записал: {structured}", voice_type)
    if audio:
        await msg.answer_voice(voice=BufferedInputFile(audio, filename="ok.ogg"))


@dp.message(F.text == "🧠 Консультация")
async def consult_mode(msg: Message):
    await smart_reply(msg, "💬 Задайте вопрос текстом или голосом — отвечу как юрист по законодательству РБ.")


@dp.message(F.text == "🌤 Погода")
async def show_weather(msg: Message):
    _, city = db_user_get(msg.from_user.id)
    await smart_reply(msg, await weather_report(city))


@dp.message(F.text == "⚙️ Настройки")
async def show_settings(msg: Message):
    await msg.answer("⚙️ Настройки:", reply_markup=settings_kb())


@dp.message(F.text == "📋 Задачи")
async def tasks_menu(msg: Message, state: FSMContext):
    await state.set_state(TaskForm.title)
    await msg.answer("➕ Название новой задачи (или «отмена»):")


@dp.message(TaskForm.title)
async def task_title(msg: Message, state: FSMContext):
    if msg.text.lower() == "отмена":
        await state.clear()
        return await msg.answer("Отменено.")
    await state.update_data(title=msg.text)
    await state.set_state(TaskForm.deadline)
    await msg.answer("📅 Дедлайн ГГГГ-ММ-ДД ЧЧ:ММ (или «нет»):")


@dp.message(TaskForm.deadline)
async def task_deadline(msg: Message, state: FSMContext):
    deadline = None
    if msg.text.lower() != "нет":
        try:
            deadline = datetime.strptime(msg.text.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return await msg.answer("Формат: 2026-09-15 14:00")
    data = await state.get_data()
    db_task_add(msg.from_user.id, data["title"], deadline.isoformat() if deadline else None)
    await state.clear()
    dl = deadline.strftime("%d.%m %H:%M") if deadline else "без дедлайна"
    await smart_reply(msg, f"✅ Задача «{data['title']}» создана. Дедлайн: {dl}. Напомню за час и за 15 минут.")


@dp.callback_query(F.data.startswith("voice:"))
async def set_voice(cb: CallbackQuery):
    v = "male" if cb.data == "voice:male" else "female"
    db_user_set(cb.from_user.id, "voice", v)
    await cb.answer(f"Голос: {'мужской' if v == 'male' else 'женский'}", show_alert=True)


@dp.callback_query(F.data == "city:set")
async def set_city(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CityForm.city)
    await cb.message.answer("🌍 Введите город:")


@dp.message(CityForm.city)
async def save_city(msg: Message, state: FSMContext):
    db_user_set(msg.from_user.id, "city", msg.text.strip())
    await state.clear()
    await msg.answer(f"✅ Город: {msg.text.strip()}")


@dp.message(F.text & ~F.text.startswith("/") & ~F.text.in_(
    {"📋 Задачи", "🌤 Погода", "⚙️ Настройки", "🧠 Консультация"}))
async def free_text(msg: Message):
    await msg.answer("🤖 Формулирую ответ...")
    answer = await ai_consult(msg.from_user.id, msg.text)
    db_history_add(msg.from_user.id, "user", msg.text)
    db_history_add(msg.from_user.id, "assistant", answer)
    await smart_reply(msg, answer)


async def reminder_loop():
    while True:
        try:
            now = datetime.utcnow()
            with sqlite3.connect("assistant.db") as c:
                tasks = c.execute(
                    "SELECT id,user_id,title,deadline,notified_1h,notified_15m "
                    "FROM tasks WHERE status='active' AND deadline IS NOT NULL"
                ).fetchall()
            for tid, uid, title, dl, n1h, n15m in tasks:
                try:
                    deadline = datetime.fromisoformat(dl)
                except Exception:
                    continue
                mins = (deadline - now).total_seconds() / 60
                if 55 <= mins <= 65 and not n1h:
                    await bot.send_message(uid, f"⏰ Через час дедлайн: «{title}»")
                    with sqlite3.connect("assistant.db") as c:
                        c.execute("UPDATE tasks SET notified_1h=1 WHERE id=?", (tid,))
                        c.commit()
                elif 10 <= mins <= 20 and not n15m:
                    await bot.send_message(uid, f"🔥 Через 15 минут дедлайн: «{title}»")
                    with sqlite3.connect("assistant.db") as c:
                        c.execute("UPDATE tasks SET notified_15m=1 WHERE id=?", (tid,))
                        c.commit()
        except Exception as e:
            logging.error(f"Reminder: {e}")
        await asyncio.sleep(300)


async def main():
    db_init()
    asyncio.create_task(reminder_loop())
    print("🏛 Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
