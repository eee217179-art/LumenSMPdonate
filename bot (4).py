import asyncio
import logging
import os
import re
import struct
import time

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "lumensmp.db")

SERVER_NAME = "LumenSMP"

# ---------------------------------------------------------------------------
# Каталог донатов. Чтобы добавить новый — просто допиши запись.
# price = 0 — бесплатный (выдаётся сразу, один раз на аккаунт Telegram).
# commands — команды RCON, {nick} подставляется автоматически.
# ---------------------------------------------------------------------------
DONATES = {
    "test": {
        "title": "Тест",
        "price": 0,
        "description": "Тестовый бесплатный донат.",
        "commands": ["lp user {nick} parent add test"],
    },
}

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lumensmp")
router = Router()


class Buy(StatesGroup):
    waiting_nick = State()


# ----------------------------- RCON клиент ---------------------------------
class RconError(Exception):
    pass


async def rcon_command(command: str, timeout: float = 8.0) -> str:
    """Подключается к RCON, авторизуется, выполняет команду, возвращает ответ."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(RCON_HOST, RCON_PORT), timeout
        )
    except (OSError, asyncio.TimeoutError) as e:
        raise RconError(f"Не удалось подключиться к серверу: {e}")

    async def send(pid: int, ptype: int, body: str):
        payload = struct.pack("<ii", pid, ptype) + body.encode("utf-8") + b"\x00\x00"
        writer.write(struct.pack("<i", len(payload)) + payload)
        await writer.drain()

    async def recv():
        raw_len = await asyncio.wait_for(reader.readexactly(4), timeout)
        (length,) = struct.unpack("<i", raw_len)
        data = await asyncio.wait_for(reader.readexactly(length), timeout)
        pid, ptype = struct.unpack("<ii", data[:8])
        return pid, ptype, data[8:-2].decode("utf-8", errors="ignore")

    try:
        await send(1, 3, RCON_PASSWORD)  # авторизация
        while True:
            pid, ptype, _ = await recv()
            if ptype == 2:  # ответ на авторизацию
                if pid == -1:
                    raise RconError("Неверный RCON пароль")
                break
        await send(2, 2, command)
        _, _, body = await recv()
        return body
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        raise RconError(f"Ошибка RCON: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ------------------------------- База --------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                nick TEXT NOT NULL,
                donate_id TEXT NOT NULL,
                price INTEGER NOT NULL,
                ts INTEGER NOT NULL
            )"""
        )
        await db.commit()


async def already_got(user_id: int, donate_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM purchases WHERE user_id=? AND donate_id=? LIMIT 1",
            (user_id, donate_id),
        )
        return await cur.fetchone() is not None


async def save_purchase(user_id, username, nick, donate_id, price):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO purchases (user_id, username, nick, donate_id, price, ts) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, username, nick, donate_id, price, int(time.time())),
        )
        await db.commit()


# ----------------------------- Клавиатуры ----------------------------------
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить донат", callback_data="shop")],
            [InlineKeyboardButton(text="📜 Мои покупки", callback_data="my")],
        ]
    )


def shop_kb() -> InlineKeyboardMarkup:
    rows = []
    for did, d in DONATES.items():
        price = "бесплатно" if d["price"] == 0 else f"{d['price']} ₽"
        rows.append(
            [InlineKeyboardButton(text=f"{d['title']} — {price}", callback_data=f"item:{did}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]
    )


# ------------------------------ Хендлеры -----------------------------------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        f"👋 Добро пожаловать в магазин доната <b>{SERVER_NAME}</b>!\n\n"
        "Здесь можно получить донат на сервере. Выбери действие:",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        f"🏠 Магазин доната <b>{SERVER_NAME}</b>", reply_markup=main_kb(), parse_mode="HTML"
    )
    await c.answer()


@router.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    await c.message.edit_text(
        "🛒 <b>Доступные донаты:</b>", reply_markup=shop_kb(), parse_mode="HTML"
    )
    await c.answer()


@router.callback_query(F.data.startswith("item:"))
async def item(c: CallbackQuery, state: FSMContext):
    did = c.data.split(":", 1)[1]
    d = DONATES.get(did)
    if not d:
        await c.answer("Донат не найден", show_alert=True)
        return
    if d["price"] == 0 and await already_got(c.from_user.id, did):
        await c.answer("Ты уже получал этот донат 🙂", show_alert=True)
        return
    await state.update_data(donate_id=did)
    await state.set_state(Buy.waiting_nick)
    price = "бесплатно" if d["price"] == 0 else f"{d['price']} ₽"
    await c.message.edit_text(
        f"📦 <b>{d['title']}</b> — {price}\n{d['description']}\n\n"
        "✍️ Напиши свой <b>ник в Minecraft</b> (3–16 символов: латиница, цифры, _):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Buy.waiting_nick)
async def got_nick(m: Message, state: FSMContext):
    nick = (m.text or "").strip()
    if not NICK_RE.match(nick):
        await m.answer(
            "❌ Некорректный ник. Только латиница, цифры и _, длина 3–16. Попробуй ещё раз:",
            reply_markup=cancel_kb(),
        )
        return

    data = await state.get_data()
    did = data.get("donate_id")
    d = DONATES.get(did)
    if not d:
        await state.clear()
        await m.answer("Что-то пошло не так, начни заново: /start")
        return

    if d["price"] > 0:
        # Сюда подключается оплата (ЮKassa, CryptoBot и т.д.), когда появятся платные донаты.
        await state.clear()
        await m.answer("💳 Оплата пока недоступна.", reply_markup=main_kb())
        return

    if await already_got(m.from_user.id, did):
        await state.clear()
        await m.answer("Ты уже получал этот донат 🙂", reply_markup=main_kb())
        return

    wait = await m.answer("⏳ Выдаю донат...")
    try:
        for cmd in d["commands"]:
            resp = await rcon_command(cmd.format(nick=nick))
            log.info("RCON %s -> %s", cmd.format(nick=nick), resp)
    except RconError as e:
        log.error("RCON error: %s", e)
        await state.clear()
        await wait.edit_text(
            "⚠️ Не удалось выдать донат: сервер недоступен. Попробуй позже или напиши админу.",
            reply_markup=main_kb(),
        )
        for aid in ADMIN_IDS:
            try:
                await m.bot.send_message(aid, f"⚠️ RCON ошибка при выдаче {did} игроку {nick}: {e}")
            except Exception:
                pass
        return

    await save_purchase(m.from_user.id, m.from_user.username, nick, did, d["price"])
    await state.clear()
    await wait.edit_text(
        f"✅ Донат <b>{d['title']}</b> выдан игроку <b>{nick}</b>!\n"
        f"Заходи на {SERVER_NAME} и играй 🎮",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "my")
async def my(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT donate_id, nick, ts FROM purchases WHERE user_id=? ORDER BY ts DESC LIMIT 10",
            (c.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        text = "У тебя пока нет покупок."
    else:
        lines = []
        for did, nick, ts in rows:
            title = DONATES.get(did, {}).get("title", did)
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            lines.append(f"• {title} → {nick} ({date})")
        text = "📜 <b>Твои покупки:</b>\n" + "\n".join(lines)
    await c.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]]
        ),
        parse_mode="HTML",
    )
    await c.answer()


# ------------------------------ Админ --------------------------------------
@router.message(Command("give"))
async def admin_give(m: Message):
    """/give <ник> <id_доната> — выдать донат вручную (только админы)."""
    if m.from_user.id not in ADMIN_IDS:
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or parts[2] not in DONATES or not NICK_RE.match(parts[1]):
        await m.answer(f"Использование: /give <ник> <донат>\nДоступно: {', '.join(DONATES)}")
        return
    nick, did = parts[1], parts[2]
    try:
        for cmd in DONATES[did]["commands"]:
            await rcon_command(cmd.format(nick=nick))
    except RconError as e:
        await m.answer(f"⚠️ {e}")
        return
    await m.answer(f"✅ {DONATES[did]['title']} выдан {nick}")


@router.message(Command("rcon"))
async def admin_rcon(m: Message):
    """/rcon <команда> — выполнить команду на сервере (только админы)."""
    if m.from_user.id not in ADMIN_IDS:
        return
    cmd = (m.text or "").partition(" ")[2].strip()
    if not cmd:
        await m.answer("Использование: /rcon <команда>")
        return
    try:
        resp = await rcon_command(cmd)
    except RconError as e:
        await m.answer(f"⚠️ {e}")
        return
    await m.answer(f"<code>{resp or 'OK'}</code>", parse_mode="HTML")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в .env")
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
