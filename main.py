import asyncio
import base64
import datetime
import io
import json
import logging
import math
import os
import random
import shutil
import string
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# =========================== КОНФИГ — ПРАВЬ ЗДЕСЬ ===========================

BOT_TOKEN = "123456:AA..."          # токен от @BotFather
BOT_USERNAME = "your_bot"           # username бота без @
ADMIN_IDS = {123456789}             # твой telegram id (можно несколько через запятую: {111, 222})

DEFAULT_PRICES = {
    "template_static": 5,
    "template_animated": 15,
    "ai_static": 25,
    "ai_animated": 50,
}
PRICE_LABELS = {
    "template_static": "Шаблон — статика",
    "template_animated": "Шаблон — анимация",
    "ai_static": "AI — статика",
    "ai_animated": "AI — анимация",
}

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")
TMP_DIR = "tmp"
FONT_PATH = os.path.join("assets", "fonts", "DejaVuSans-Bold.ttf")

EMOJI_SIZE = 100
SUPERSAMPLE = 4
CANVAS = EMOJI_SIZE * SUPERSAMPLE

FPS = 30
DURATION_SEC = 2
FRAMES = FPS * DURATION_SEC

TEMPLATES = {
    "circle_outline": "Круг (обводка)",
    "circle_filled": "Круг (заливка)",
    "shield": "Щит",
    "hexagon": "Шестиугольник",
    "square": "Скруглённый квадрат",
    "ribbon": "Лента / бейдж",
}

logging.basicConfig(level=logging.INFO)
router = Router()
PENDING: dict = {}  # user_id -> параметры генерации / готовый файл до/после оплаты


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================== ХРАНИЛИЩЕ (JSON) ===========================

_DB_DEFAULT = {
    "users": {},       # uid -> profile
    "promocodes": {},  # code -> {stars, max_uses, used_by[], active}
    "settings": {"prices": {}, "maintenance": False},
    "payments": [],    # лог реальных платежей звёздами
}

_USER_DEFAULT = {
    "pack_name": None, "pack_title": None, "count": 0, "generated": 0,
    "balance": 0, "free_generations": 0, "banned": False,
    "username": None, "first_name": None, "joined_at": None,
}


def _ensure_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(_DB_DEFAULT, f, ensure_ascii=False, indent=2)


def _load_db() -> dict:
    _ensure_db()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            db = json.load(f)
        except json.JSONDecodeError:
            db = {}
    for k, v in _DB_DEFAULT.items():
        db.setdefault(k, v if not isinstance(v, (dict, list)) else (dict(v) if isinstance(v, dict) else list(v)))
    return db


def _save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_user(user_id: int) -> dict:
    db = _load_db()
    user = dict(_USER_DEFAULT)
    user.update(db["users"].get(str(user_id), {}))
    return user


def set_user(user_id: int, **fields):
    db = _load_db()
    uid = str(user_id)
    user = dict(_USER_DEFAULT)
    user.update(db["users"].get(uid, {}))
    user.update(fields)
    db["users"][uid] = user
    _save_db(db)
    return user


def touch_user(tg_user):
    user = get_user(tg_user.id)
    if user["joined_at"] is None:
        set_user(tg_user.id, joined_at=datetime.datetime.utcnow().isoformat(),
                  username=tg_user.username, first_name=tg_user.first_name)
    else:
        set_user(tg_user.id, username=tg_user.username, first_name=tg_user.first_name)


def all_user_ids() -> list:
    return [int(uid) for uid in _load_db()["users"].keys()]


def increment_generated(user_id: int):
    set_user(user_id, generated=get_user(user_id).get("generated", 0) + 1)


def increment_pack_count(user_id: int, by: int = 1):
    set_user(user_id, count=get_user(user_id).get("count", 0) + by)


def add_balance(user_id: int, amount: int):
    return set_user(user_id, balance=get_user(user_id).get("balance", 0) + amount)


def add_free_generations(user_id: int, amount: int):
    return set_user(user_id, free_generations=get_user(user_id).get("free_generations", 0) + amount)


def set_banned(user_id: int, banned: bool):
    return set_user(user_id, banned=banned)


# --- настройки / цены ---

def get_price(kind: str) -> int:
    db = _load_db()
    override = db["settings"].get("prices", {}).get(kind)
    return int(override) if override is not None else DEFAULT_PRICES[kind]


def set_price(kind: str, value: int):
    db = _load_db()
    db["settings"].setdefault("prices", {})[kind] = int(value)
    _save_db(db)


def get_maintenance() -> bool:
    return bool(_load_db()["settings"].get("maintenance", False))


def set_maintenance(value: bool):
    db = _load_db()
    db["settings"]["maintenance"] = value
    _save_db(db)


# --- промокоды ---

def create_promo(code: str, stars: int, max_uses: int):
    db = _load_db()
    db["promocodes"][code.upper()] = {
        "stars": stars, "max_uses": max_uses, "used_by": [], "active": True,
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    _save_db(db)


def delete_promo(code: str) -> bool:
    db = _load_db()
    existed = db["promocodes"].pop(code.upper(), None) is not None
    _save_db(db)
    return existed


def list_promos() -> dict:
    return _load_db()["promocodes"]


def redeem_promo(code: str, user_id: int):
    db = _load_db()
    promo = db["promocodes"].get(code.upper())
    if not promo:
        return False, "Промокод не найден"
    if not promo.get("active", True):
        return False, "Промокод отключён"
    if len(promo["used_by"]) >= promo["max_uses"]:
        return False, "У промокода закончились активации"
    if user_id in promo["used_by"]:
        return False, "Ты уже использовал этот промокод"
    promo["used_by"].append(user_id)
    db["promocodes"][code.upper()] = promo
    _save_db(db)
    add_balance(user_id, promo["stars"])
    return True, promo["stars"]


# --- лог платежей ---

def log_payment(user_id: int, amount: int, kind: str, method: str):
    db = _load_db()
    db["payments"].append({
        "user_id": user_id, "amount": amount, "kind": kind, "method": method,
        "ts": datetime.datetime.utcnow().isoformat(),
    })
    _save_db(db)


def stats_summary() -> dict:
    db = _load_db()
    users = db["users"]
    payments = db["payments"]
    real_payments = [p for p in payments if p["method"] == "stars"]
    return {
        "total_users": len(users),
        "banned_users": sum(1 for u in users.values() if u.get("banned")),
        "total_generated": sum(u.get("generated", 0) for u in users.values()),
        "total_pack_emojis": sum(u.get("count", 0) for u in users.values()),
        "revenue_stars": sum(p["amount"] for p in real_payments),
        "real_payments_count": len(real_payments),
        "active_promos": sum(1 for p in db["promocodes"].values() if p.get("active", True)),
        "total_balance_issued": sum(u.get("balance", 0) for u in users.values()),
    }


# =========================== ШАБЛОНЫ (Pillow) ===========================


def _font_for_text(text: str, max_width: int, max_height: int) -> ImageFont.FreeTypeFont:
    size = int(max_height * 0.9)
    font = ImageFont.truetype(FONT_PATH, size)
    while size > 6:
        font = ImageFont.truetype(FONT_PATH, size)
        bbox = font.getbbox(text)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= max_width and h <= max_height:
            break
        size -= 2
    return font


def _draw_text_centered(draw: ImageDraw.ImageDraw, text: str, box, fill):
    x0, y0, x1, y1 = box
    max_w, max_h = int((x1 - x0) * 0.85), int((y1 - y0) * 0.6)
    font = _font_for_text(text, max_w, max_h)
    bbox = font.getbbox(text)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def render_template(template_id: str, text: str) -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = CANVAS // 12
    box = (pad, pad, CANVAS - pad, CANVAS - pad)
    white = (255, 255, 255, 255)
    sw = max(2, CANVAS // 40)
    w = CANVAS

    if template_id == "circle_outline":
        draw.ellipse(box, outline=white, width=sw)
        _draw_text_centered(draw, text, box, white)

    elif template_id == "circle_filled":
        draw.ellipse((0, 0, w, w), fill=(0, 0, 0, 255))
        draw.ellipse(box, outline=white, width=sw)
        _draw_text_centered(draw, text, box, white)

    elif template_id == "shield":
        pts = [(w * 0.1, w * 0.08), (w * 0.9, w * 0.08), (w * 0.9, w * 0.55), (w * 0.5, w * 0.95), (w * 0.1, w * 0.55)]
        draw.polygon(pts, outline=white, width=sw)
        _draw_text_centered(draw, text, (w * 0.12, w * 0.15, w * 0.88, w * 0.65), white)

    elif template_id == "hexagon":
        cx, cy, r = w / 2, w / 2, w / 2 - pad
        pts = [(cx + r * math.cos(math.pi / 3 * i - math.pi / 2), cy + r * math.sin(math.pi / 3 * i - math.pi / 2)) for i in range(6)]
        draw.polygon(pts, outline=white, width=sw)
        _draw_text_centered(draw, text, box, white)

    elif template_id == "square":
        draw.rounded_rectangle(box, radius=w // 6, outline=white, width=sw)
        _draw_text_centered(draw, text, box, white)

    elif template_id == "ribbon":
        body = (w * 0.08, w * 0.32, w * 0.92, w * 0.68)
        draw.rounded_rectangle(body, radius=w * 0.06, outline=white, width=sw)
        draw.polygon([(w * 0.02, w * 0.5), (w * 0.12, w * 0.35), (w * 0.12, w * 0.65)], outline=white, width=sw // 2)
        draw.polygon([(w * 0.98, w * 0.5), (w * 0.88, w * 0.35), (w * 0.88, w * 0.65)], outline=white, width=sw // 2)
        _draw_text_centered(draw, text, body, white)

    else:
        raise ValueError(f"Unknown template: {template_id}")

    return img.resize((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)


# =========================== AI-ГЕНЕРАЦИЯ (Pollinations.ai, бесплатно) ===========================

POLLINATIONS_STYLE = (
    ", simple flat icon design, centered on plain background, bold clean shapes, "
    "suitable for a tiny 100x100 emoji, no text unless requested"
)


def generate_ai_image(prompt: str) -> Image.Image:
    """Бесплатная генерация через image.pollinations.ai — без API-ключа.
    Анонимный доступ ограничен примерно 1 запросом в 15 секунд — для генерации
    по одной эмодзи за раз (после оплаты) этого достаточно."""
    import requests
    from urllib.parse import quote

    full_prompt = quote(prompt + POLLINATIONS_STYLE)
    url = f"https://image.pollinations.ai/prompt/{full_prompt}"
    params = {"width": 1024, "height": 1024, "nologo": "true"}
    resp = requests.get(url, params=params, timeout=90)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    return img.resize((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)


# =========================== АНИМАЦИЯ (ffmpeg) ===========================


def _pulse_frame(base: Image.Image, t: float) -> Image.Image:
    scale = 1.0 + 0.06 * math.sin(2 * math.pi * t)
    size = max(1, int(EMOJI_SIZE * scale))
    frame = Image.new("RGBA", (EMOJI_SIZE, EMOJI_SIZE), (0, 0, 0, 0))
    resized = base.resize((size, size), Image.LANCZOS)
    off = (EMOJI_SIZE - size) // 2
    frame.paste(resized, (off, off), resized)
    return frame


def build_animated_webm(base_img: Image.Image, out_path: str) -> str:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg не найден на сервере — установи ffmpeg для анимированных эмодзи")

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(FRAMES):
            _pulse_frame(base_img, i / FRAMES).save(os.path.join(tmp, f"f{i:03d}.png"))

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(FPS),
            "-i", os.path.join(tmp, "f%03d.png"),
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", "128k",
            "-auto-alt-ref", "0",
            "-an",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    return out_path


# =========================== КЛАВИАТУРЫ (пользователь) ===========================


def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎨 Создать эмодзи", callback_data="new_emoji")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="📦 Мой набор", callback_data="my_pack")],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="promo_enter")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Готовый шаблон + текст", callback_data="method:template")],
        [InlineKeyboardButton(text="🤖 Нейросеть (по описанию)", callback_data="method:ai")],
    ])


def templates_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=name, callback_data=f"tpl:{tid}")] for tid, name in TEMPLATES.items()])


def format_kb(method: str) -> InlineKeyboardMarkup:
    p_static = get_price("template_static" if method == "template" else "ai_static")
    p_anim = get_price("template_animated" if method == "template" else "ai_animated")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🖼 Статичный — ⭐{p_static}", callback_data="fmt:static")],
        [InlineKeyboardButton(text=f"🎞 Анимированный — ⭐{p_anim}", callback_data="fmt:animated")],
    ])


def addpack_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в мой набор", callback_data="addpack:yes")],
        [InlineKeyboardButton(text="Не сейчас", callback_data="addpack:no")],
    ])


# =========================== КЛАВИАТУРЫ (админ) ===========================


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="👤 Найти пользователя", callback_data="admin:lookup")],
        [InlineKeyboardButton(text="💰 Выдать/списать баланс", callback_data="admin:balance")],
        [InlineKeyboardButton(text="🎁 Выдать free-генерации", callback_data="admin:freegen")],
        [InlineKeyboardButton(text="🚫 Бан / разбан", callback_data="admin:ban")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin:promo")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="💵 Цены", callback_data="admin:prices")],
        [InlineKeyboardButton(text=f"🛠 Техработы: {'ВКЛ 🔴' if get_maintenance() else 'выкл 🟢'}", callback_data="admin:maintenance")],
        [InlineKeyboardButton(text="💾 Бэкап базы", callback_data="admin:backup")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin:exit")],
    ])


def admin_promo_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="admin:promo:create")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin:promo:list")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data="admin:promo:delete")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


def admin_prices_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{label}: ⭐{get_price(key)}", callback_data=f"admin:price:{key}")]
            for key, label in PRICE_LABELS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В админку", callback_data="admin:menu")]])


# =========================== СОСТОЯНИЯ ===========================


class Gen(StatesGroup):
    waiting_template = State()
    waiting_text = State()
    waiting_prompt = State()


class Promo(StatesGroup):
    waiting_code = State()


class Admin(StatesGroup):
    waiting_lookup = State()
    waiting_balance = State()
    waiting_freegen = State()
    waiting_ban = State()
    waiting_promo_create = State()
    waiting_promo_delete = State()
    waiting_broadcast = State()
    waiting_price_value = State()


# =========================== ГЕНЕРАЦИЯ (общая функция) ===========================


async def perform_generation(bot: Bot, chat_id: int, user_id: int, data: dict):
    try:
        if data["method"] == "template":
            base_img = render_template(data["template_id"], data["text"])
        else:
            base_img = await asyncio.to_thread(generate_ai_image, data["prompt"])

        os.makedirs(TMP_DIR, exist_ok=True)

        if data["format"] == "static":
            buf = io.BytesIO()
            base_img.save(buf, format="PNG", optimize=True)
            file_bytes = buf.getvalue()
            filename, sticker_format = "emoji.png", "static"
        else:
            out_path = os.path.join(TMP_DIR, f"{user_id}_{random.randint(0, 999999)}.webm")
            await asyncio.to_thread(build_animated_webm, base_img, out_path)
            with open(out_path, "rb") as f:
                file_bytes = f.read()
            os.remove(out_path)
            filename, sticker_format = "emoji.webm", "video"

        increment_generated(user_id)

        input_file = BufferedInputFile(file_bytes, filename=filename)
        caption = "Готово!" if sticker_format == "static" else "Готово! (превью как файл — предпросмотра видео-эмодзи в чате может не быть)"
        await bot.send_document(chat_id, input_file, caption=caption)

        PENDING[f"file_{user_id}"] = {"bytes": file_bytes, "filename": filename, "format": sticker_format}
        await bot.send_message(chat_id, "Добавить эту эмодзи в твой набор?", reply_markup=addpack_kb())

    except Exception as e:
        logging.exception("Generation failed")
        await bot.send_message(chat_id, f"Не получилось сгенерировать: {e}\nНапиши в поддержку.")


# =========================== ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ ===========================


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)
    await message.answer(
        "Привет! Я делаю кастомные эмодзи двумя способами:\n"
        "— из готового шаблона со своим текстом\n"
        "— по описанию через нейросеть\n\n"
        "Оплата — звёздами Telegram, прямо тут, за каждую сгенерированную эмодзи.\n"
        "Есть промокоды на баланс — /promo",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    text = (
        f"👤 Профиль\n"
        f"Баланс: ⭐{user['balance']}\n"
        f"Бесплатных генераций: {user['free_generations']}\n"
        f"Сгенерировано всего: {user['generated']}\n"
        f"Эмодзи в наборе: {user['count']}\n"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "new_emoji")
async def cb_new_emoji(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    if user["banned"]:
        await call.answer("Ты заблокирован в этом боте.", show_alert=True)
        return
    if get_maintenance() and not is_admin(call.from_user.id):
        await call.answer("Идут технические работы, попробуй позже.", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text("Как создаём эмодзи?", reply_markup=method_kb())
    await call.answer()


@router.callback_query(F.data == "my_pack")
async def cb_my_pack(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user.get("pack_name"):
        await call.message.edit_text("У тебя пока нет набора — он создастся автоматически после первой эмодзи.", reply_markup=main_menu_kb(call.from_user.id))
    else:
        await call.message.edit_text(
            f"Твой набор: {user['pack_name']}\nЭмодзи в наборе: {user.get('count', 0)}\nhttps://t.me/addemoji/{user['pack_name']}",
            reply_markup=main_menu_kb(call.from_user.id),
        )
    await call.answer()


@router.callback_query(F.data == "promo_enter")
async def cb_promo_enter(call: CallbackQuery, state: FSMContext):
    await state.set_state(Promo.waiting_code)
    await call.message.edit_text("Пришли промокод текстом:")
    await call.answer()


@router.message(Promo.waiting_code)
async def msg_promo_code(message: Message, state: FSMContext):
    await state.clear()
    ok, result = redeem_promo(message.text.strip(), message.from_user.id)
    if ok:
        await message.answer(f"Промокод активирован! Начислено ⭐{result} на баланс.", reply_markup=main_menu_kb(message.from_user.id))
    else:
        await message.answer(f"Не вышло: {result}", reply_markup=main_menu_kb(message.from_user.id))


@router.callback_query(F.data == "method:template")
async def cb_method_template(call: CallbackQuery, state: FSMContext):
    await state.update_data(method="template")
    await state.set_state(Gen.waiting_template)
    await call.message.edit_text("Выбери шаблон:", reply_markup=templates_kb())
    await call.answer()


@router.callback_query(F.data == "method:ai")
async def cb_method_ai(call: CallbackQuery, state: FSMContext):
    await state.update_data(method="ai")
    await state.set_state(Gen.waiting_prompt)
    await call.message.edit_text("Опиши, что должно быть на эмодзи (на английском лучше всего работает):")
    await call.answer()


@router.callback_query(F.data.startswith("tpl:"), Gen.waiting_template)
async def cb_pick_template(call: CallbackQuery, state: FSMContext):
    tpl_id = call.data.split(":", 1)[1]
    await state.update_data(template_id=tpl_id)
    await state.set_state(Gen.waiting_text)
    await call.message.edit_text(f"Шаблон: {TEMPLATES[tpl_id]}\n\nТеперь пришли текст, который нужно разместить на эмодзи:")
    await call.answer()


@router.message(Gen.waiting_text)
async def msg_got_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text.strip()[:20])
    data = await state.get_data()
    await message.answer("Какой формат нужен?", reply_markup=format_kb(data["method"]))


@router.message(Gen.waiting_prompt)
async def msg_got_prompt(message: Message, state: FSMContext):
    await state.update_data(prompt=message.text.strip()[:300])
    data = await state.get_data()
    await message.answer("Какой формат нужен?", reply_markup=format_kb(data["method"]))


@router.callback_query(F.data.startswith("fmt:"))
async def cb_pick_format(call: CallbackQuery, state: FSMContext, bot: Bot):
    fmt = call.data.split(":", 1)[1]
    data = await state.get_data()
    method = data.get("method")
    if method is None:
        await call.answer("Начни заново — /start", show_alert=True)
        return

    kind = f"{method}_{fmt}"  # template_static / template_animated / ai_static / ai_animated
    price = get_price(kind)
    title = f"Эмодзи: {TEMPLATES[data['template_id']]}" if method == "template" else "Эмодзи по описанию (AI)"
    gen_data = {**data, "format": fmt}
    await state.clear()
    user_id = call.from_user.id
    user = get_user(user_id)

    # 1. бесплатные генерации в приоритете
    if user.get("free_generations", 0) > 0:
        set_user(user_id, free_generations=user["free_generations"] - 1)
        await call.message.edit_text("Списана 1 бесплатная генерация ⭐️ Генерирую...")
        await call.answer()
        await perform_generation(bot, call.message.chat.id, user_id, gen_data)
        return

    # 2. внутренний баланс
    if user.get("balance", 0) >= price:
        set_user(user_id, balance=user["balance"] - price)
        log_payment(user_id, price, kind, method="balance")
        await call.message.edit_text(f"Списано ⭐{price} с баланса. Генерирую...")
        await call.answer()
        await perform_generation(bot, call.message.chat.id, user_id, gen_data)
        return

    # 3. реальная оплата звёздами
    PENDING[user_id] = gen_data
    await bot.send_invoice(
        chat_id=user_id,
        title=title,
        description=f"{'Анимированная' if fmt == 'animated' else 'Статичная'} эмодзи, формат {'video' if fmt == 'animated' else 'png'}",
        payload=f"gen:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)],
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot):
    user_id = message.from_user.id
    data = PENDING.pop(user_id, None)
    if not data:
        await message.answer("Оплата получена, но данные генерации потерялись — напиши мне ещё раз, что нужно.")
        return

    kind = f"{data['method']}_{data['format']}"
    amount = message.successful_payment.total_amount
    log_payment(user_id, amount, kind, method="stars")

    await message.answer("Оплата получена ⭐️ Генерирую...")
    await perform_generation(bot, message.chat.id, user_id, data)


def _random_suffix(n=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


@router.callback_query(F.data == "addpack:no")
async def cb_addpack_no(call: CallbackQuery):
    PENDING.pop(f"file_{call.from_user.id}", None)
    await call.message.edit_text("Ок, файл уже у тебя в чате.")
    await call.answer()


@router.callback_query(F.data == "addpack:yes")
async def cb_addpack_yes(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    file_data = PENDING.pop(f"file_{user_id}", None)
    if not file_data:
        await call.answer("Файл не найден, сгенерируй заново", show_alert=True)
        return

    user = get_user(user_id)
    input_file = BufferedInputFile(file_data["bytes"], filename=file_data["filename"])
    sticker = InputSticker(sticker=input_file, format=file_data["format"], emoji_list=["⭐"])

    try:
        if not user.get("pack_name"):
            pack_name = f"u{user_id}{_random_suffix()}_by_{BOT_USERNAME}"
            pack_title = f"{call.from_user.first_name}'s emoji"[:64]
            await bot.create_new_sticker_set(
                user_id=user_id, name=pack_name, title=pack_title, stickers=[sticker], sticker_type="custom_emoji",
            )
            set_user(user_id, pack_name=pack_name, pack_title=pack_title, count=1)
        else:
            await bot.add_sticker_to_set(user_id=user_id, name=user["pack_name"], sticker=sticker)
            increment_pack_count(user_id)

        pack_name = get_user(user_id)["pack_name"]
        await call.message.edit_text(f"Добавлено! Набор: https://t.me/addemoji/{pack_name}")
    except Exception as e:
        logging.exception("Sticker set error")
        await call.message.edit_text(f"Не получилось добавить в набор: {e}")
    await call.answer()


# =========================== АДМИН-ПАНЕЛЬ ===========================


def _admin_guard(call: CallbackQuery) -> bool:
    return is_admin(call.from_user.id)


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.clear()
    await call.message.edit_text("Админ-панель:", reply_markup=admin_menu_kb())
    await call.answer()


@router.callback_query(F.data == "admin:exit")
async def cb_admin_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_menu_kb(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if not _admin_guard(call):
        return await call.answer()
    s = stats_summary()
    text = (
        "📊 Статистика\n\n"
        f"Пользователей: {s['total_users']} (заблокировано: {s['banned_users']})\n"
        f"Сгенерировано эмодзи всего: {s['total_generated']}\n"
        f"Эмодзи добавлено в паки: {s['total_pack_emojis']}\n"
        f"Реальных платежей звёздами: {s['real_payments_count']}\n"
        f"Выручка звёздами: ⭐{s['revenue_stars']}\n"
        f"Активных промокодов: {s['active_promos']}\n"
        f"Баланса выдано пользователям (текущий суммарный): ⭐{s['total_balance_issued']}\n"
    )
    await call.message.edit_text(text, reply_markup=back_to_admin_kb())
    await call.answer()


@router.callback_query(F.data == "admin:lookup")
async def cb_admin_lookup(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_lookup)
    await call.message.edit_text("Пришли user_id для просмотра профиля:", reply_markup=back_to_admin_kb())
    await call.answer()


@router.message(Admin.waiting_lookup)
async def msg_admin_lookup(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой user_id.", reply_markup=back_to_admin_kb())
        return
    u = get_user(uid)
    text = (
        f"👤 Пользователь {uid}\n"
        f"Username: @{u['username']}\nИмя: {u['first_name']}\n"
        f"Заблокирован: {'да' if u['banned'] else 'нет'}\n"
        f"Баланс: ⭐{u['balance']}\nFree-генераций: {u['free_generations']}\n"
        f"Сгенерировано: {u['generated']}\nЭмодзи в паке: {u['count']}\n"
        f"Пак: {u['pack_name']}\nРегистрация: {u['joined_at']}\n"
    )
    await message.answer(text, reply_markup=back_to_admin_kb())


@router.callback_query(F.data == "admin:balance")
async def cb_admin_balance(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_balance)
    await call.message.edit_text(
        "Пришли: `user_id сумма` (сумма может быть отрицательной, чтобы списать)\nНапример: `123456789 50`",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb(),
    )
    await call.answer()


@router.message(Admin.waiting_balance)
async def msg_admin_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid_str, amount_str = message.text.split()
        uid, amount = int(uid_str), int(amount_str)
    except (ValueError, AttributeError):
        await message.answer("Формат: `user_id сумма`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb())
        return
    user = add_balance(uid, amount)
    await message.answer(f"Готово. Баланс пользователя {uid} теперь: ⭐{user['balance']}", reply_markup=back_to_admin_kb())


@router.callback_query(F.data == "admin:freegen")
async def cb_admin_freegen(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_freegen)
    await call.message.edit_text(
        "Пришли: `user_id количество` бесплатных генераций для выдачи\nНапример: `123456789 3`",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb(),
    )
    await call.answer()


@router.message(Admin.waiting_freegen)
async def msg_admin_freegen(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid_str, count_str = message.text.split()
        uid, count = int(uid_str), int(count_str)
    except (ValueError, AttributeError):
        await message.answer("Формат: `user_id количество`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb())
        return
    user = add_free_generations(uid, count)
    await message.answer(f"Готово. У пользователя {uid} теперь {user['free_generations']} free-генераций.", reply_markup=back_to_admin_kb())


@router.callback_query(F.data == "admin:ban")
async def cb_admin_ban(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_ban)
    await call.message.edit_text("Пришли user_id, чтобы переключить бан (бан/разбан):", reply_markup=back_to_admin_kb())
    await call.answer()


@router.message(Admin.waiting_ban)
async def msg_admin_ban(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой user_id.", reply_markup=back_to_admin_kb())
        return
    user = get_user(uid)
    new_state = not user["banned"]
    set_banned(uid, new_state)
    await message.answer(f"Пользователь {uid} теперь {'заблокирован 🚫' if new_state else 'разблокирован ✅'}.", reply_markup=back_to_admin_kb())


@router.callback_query(F.data == "admin:promo")
async def cb_admin_promo(call: CallbackQuery):
    if not _admin_guard(call):
        return await call.answer()
    await call.message.edit_text("Промокоды:", reply_markup=admin_promo_kb())
    await call.answer()


@router.callback_query(F.data == "admin:promo:create")
async def cb_admin_promo_create(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_promo_create)
    await call.message.edit_text(
        "Пришли: `КОД сумма_звёзд макс_активаций`\nНапример: `SALE2026 50 100`",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb(),
    )
    await call.answer()


@router.message(Admin.waiting_promo_create)
async def msg_admin_promo_create(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        code, stars_str, uses_str = message.text.split()
        stars, uses = int(stars_str), int(uses_str)
    except (ValueError, AttributeError):
        await message.answer("Формат: `КОД сумма_звёзд макс_активаций`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_admin_kb())
        return
    create_promo(code, stars, uses)
    await message.answer(f"Промокод {code.upper()} создан: ⭐{stars}, активаций {uses}.", reply_markup=admin_promo_kb())


@router.callback_query(F.data == "admin:promo:list")
async def cb_admin_promo_list(call: CallbackQuery):
    if not _admin_guard(call):
        return await call.answer()
    promos = list_promos()
    if not promos:
        text = "Промокодов пока нет."
    else:
        lines = []
        for code, p in promos.items():
            status = "активен" if p.get("active", True) else "выключен"
            lines.append(f"`{code}` — ⭐{p['stars']}, исп. {len(p['used_by'])}/{p['max_uses']} ({status})")
        text = "🎟 Промокоды:\n\n" + "\n".join(lines)
    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_promo_kb())
    await call.answer()


@router.callback_query(F.data == "admin:promo:delete")
async def cb_admin_promo_delete(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_promo_delete)
    await call.message.edit_text("Пришли код промокода для удаления:", reply_markup=back_to_admin_kb())
    await call.answer()


@router.message(Admin.waiting_promo_delete)
async def msg_admin_promo_delete(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    ok = delete_promo(message.text.strip())
    await message.answer("Удалено." if ok else "Такого промокода нет.", reply_markup=admin_promo_kb())


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    await state.set_state(Admin.waiting_broadcast)
    await call.message.edit_text("Пришли текст для рассылки всем пользователям бота:", reply_markup=back_to_admin_kb())
    await call.answer()


@router.message(Admin.waiting_broadcast)
async def msg_admin_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text = message.html_text
    ids = all_user_ids()
    await message.answer(f"Рассылаю на {len(ids)} пользователей...")
    sent, failed = 0, 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Готово. Доставлено: {sent}, не доставлено: {failed}.", reply_markup=back_to_admin_kb())


@router.callback_query(F.data == "admin:prices")
async def cb_admin_prices(call: CallbackQuery):
    if not _admin_guard(call):
        return await call.answer()
    await call.message.edit_text("Текущие цены (нажми, чтобы изменить):", reply_markup=admin_prices_kb())
    await call.answer()


@router.callback_query(F.data.startswith("admin:price:"))
async def cb_admin_price_pick(call: CallbackQuery, state: FSMContext):
    if not _admin_guard(call):
        return await call.answer()
    key = call.data.split(":", 2)[2]
    await state.set_state(Admin.waiting_price_value)
    await state.update_data(price_key=key)
    await call.message.edit_text(f"Новая цена в звёздах для «{PRICE_LABELS[key]}» (текущая ⭐{get_price(key)}):", reply_markup=back_to_admin_kb())
    await call.answer()


@router.message(Admin.waiting_price_value)
async def msg_admin_price_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("price_key")
    await state.clear()
    try:
        value = int(message.text.strip())
        assert value > 0
    except (ValueError, AssertionError):
        await message.answer("Нужно положительное целое число.", reply_markup=admin_prices_kb())
        return
    set_price(key, value)
    await message.answer(f"Цена «{PRICE_LABELS[key]}» обновлена: ⭐{value}", reply_markup=admin_prices_kb())


@router.callback_query(F.data == "admin:maintenance")
async def cb_admin_maintenance(call: CallbackQuery):
    if not _admin_guard(call):
        return await call.answer()
    set_maintenance(not get_maintenance())
    await call.message.edit_text("Админ-панель:", reply_markup=admin_menu_kb())
    await call.answer("Режим техработ переключён")


@router.callback_query(F.data == "admin:backup")
async def cb_admin_backup(call: CallbackQuery, bot: Bot):
    if not _admin_guard(call):
        return await call.answer()
    _ensure_db()
    await bot.send_document(call.from_user.id, FSInputFile(DB_FILE), caption="Бэкап базы данных")
    await call.answer()


# =========================== ЗАПУСК ===========================


async def main():
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не задан — админ-панель будет недоступна никому")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
