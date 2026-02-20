import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime

import aiohttp
import pytz
import rebrick
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.types import LinkPreviewOptions

dp = Dispatcher()

REBRICK_TOKEN = os.getenv("REBRICK_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not REBRICK_TOKEN:
    raise RuntimeError("REBRICK_TOKEN env var is empty")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is empty")

rebrick.init(REBRICK_TOKEN)
bot = Bot(TELEGRAM_BOT_TOKEN)


# -----------------------------
# Helpers
# -----------------------------
async def get_current_timestamp() -> str:
    tz = pytz.timezone("Etc/GMT-1")  # Europe/Belgrade ~= GMT+1
    now_utc = datetime.now(pytz.utc)
    return now_utc.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")


async def fetch_image_bytes(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LegoBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, allow_redirects=True, timeout=20) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if not ct.startswith("image/"):
                sample = await resp.text(errors="ignore")
                raise ValueError(f"Not an image. Content-Type={ct}. Sample={sample[:120]!r}")
            return await resp.read()


def format_set_html(data: dict) -> tuple[str, str]:
    name = str(data.get("name", "")).strip()
    year = data.get("year")
    num_parts = data.get("num_parts")
    set_url = str(data.get("set_url", "")).strip()
    set_img_url = str(data.get("set_img_url", "")).strip()

    raw_set_num = str(data.get("set_num", "")).strip()
    m = re.match(r"(\d+)", raw_set_num)
    set_num_clean = m.group(1) if m else raw_set_num

    set_num_e = html.escape(set_num_clean)
    name_e = html.escape(name)
    set_url_e = html.escape(set_url, quote=True)

    year_s = str(year) if year is not None else "—"
    parts_s = str(num_parts) if num_parts is not None else "—"

    lego_url = f"https://www.lego.com/en-us/search?q={set_num_clean}"
    lego_url_e = html.escape(lego_url, quote=True)

    lines = [
        f"ID: <b>{set_num_e}</b>",
        f"Название: <b>{name_e}</b> ({year_s})",
        f"Деталей: <b>{parts_s}</b>\n",
        f'🔗 <a href="{set_url_e}"><b>Rebrickable</b></a>' if set_url else "",
        f'🧱 <a href="{lego_url_e}">Lego</a> <i></i>',
    ]

    return "\n".join([ln for ln in lines if ln != ""]), set_img_url


def normalize_bot_username(u: str) -> str:
    u = (u or "").strip()
    if u.startswith("@"):
        u = u[1:]
    return u


def extract_group_set_id(text: str, bot_username: str) -> int | None:
    """
    Trigger in groups:
      @botname 12345
      @botname 12345-1
    """
    if not text:
        return None

    u = normalize_bot_username(bot_username)
    if not u:
        return None

    pattern = rf"@{re.escape(u)}\s+(\d+)(?:-\d+)?"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def extract_private_set_id(text: str) -> int | None:
    """
    Trigger in private:
      12345
      12345-1
    """
    if not text:
        return None
    m = re.match(r"^\s*(\d+)(?:-\d+)?\s*$", text)
    return int(m.group(1)) if m else None


def looks_like_moc_id(set_id: int) -> bool:
    # по твоему правилу: MOC = 6 цифр
    return len(str(set_id)) == 6


def moc_url_for_id(moc_id: int) -> str:
    return f"https://rebrickable.com/mocs/MOC-{moc_id}/"


def is_not_found_error(err_text: str) -> bool:
    s = (err_text or "").lower()
    return ("404" in s) or ("not found" in s)


async def send_set(message: types.Message, set_id: int):
    response = rebrick.lego.get_set(set_id)  # может кинуть исключение на 404
    data = json.loads(response.read())
    text, image_url = format_set_html(data)

    # 1) Fast: try direct URL
    try:
        await bot.send_photo(
            message.chat.id,
            photo=image_url,
            caption=text,
            parse_mode=ParseMode.HTML,
        )
        return
    except TelegramBadRequest as e:
        # Only fallback for CDN content-type issues
        if "wrong type of the web page content" not in str(e):
            raise

    # 2) Fallback: download & upload bytes
    img_bytes = await fetch_image_bytes(image_url)
    photo = BufferedInputFile(img_bytes, filename="set.jpg")
    await bot.send_photo(
        message.chat.id,
        photo=photo,
        caption=text,
        parse_mode=ParseMode.HTML,
    )


# -----------------------------
# Handlers
# -----------------------------
@dp.message(Command(commands=["start"]))
async def start_handler(message: types.Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return
    await message.answer(
        "Пришли номер набора, например <b>42177</b>\n"
        "В группах: @rebrickable_bot 42177\nВопросы: @pycarrot2",
        parse_mode="HTML",
    )


@dp.message()
async def unified_message_handler(message: types.Message):
    try:
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "rebrickable_bot")
            set_id = extract_group_set_id(message.text or "", bot_username)
            if not set_id:
                return
            await send_set(message, set_id)
            return

        if message.chat.type == ChatType.PRIVATE:
            set_id = extract_private_set_id(message.text or "")
            if not set_id:
                await bot.send_message(
                    message.chat.id,
                    "Пришли номер набора, например <b>42177</b>\nВопросы: @pycarrot2",
                    parse_mode=ParseMode.HTML,
                )
                return
            await send_set(message, set_id)
            return

    except Exception as error:
        err = str(error)
        chat_name = getattr(message.chat, "title", None) or message.chat.full_name
        print(f"{await get_current_timestamp()}|{chat_name}|{message.chat.id} - {err}")

        # если набора нет — пробуем “это MOC?” => отправляем ссылку
        if is_not_found_error(err):
            # set_id у нас уже вытащен из текста (число)
            # но на всякий случай — выковыряем число из исходного сообщения тоже
            raw = (message.text or "").strip()
            m = re.search(r"(\d+)", raw)
            num_s = m.group(1) if m else ""
            try:
                num = int(num_s) if num_s else None
            except ValueError:
                num = None

            if num is not None and looks_like_moc_id(num):
                url = moc_url_for_id(num)
                await bot.send_message(
                    message.chat.id,
                    (
                        f"Похоже на MOC 🔎\n"
                        f"ID: <b>{num}</b>\n\n"
                        f"🔗 <a href='{moc_url_for_id(num)}'><b>Rebrickable</b></a>"
                    ),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(
                        show_above_text=True,
                        url=url,
                    ),
                )
            else:
                # обычный сет не найден
                if num_s:
                    await bot.send_message(message.chat.id, f"Набор {num_s} не найден!")
                else:
                    await bot.send_message(message.chat.id, "Набор не найден!")
            return


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())