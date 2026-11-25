import asyncio
import logging
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOTSMS_API_KEY = os.environ["GOTSMS_API_KEY"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SERVICE_NAME = os.getenv("SERVICE_NAME", "Telegram")
TARGET_PRICE = 1.0
# ===============================================

BASE_URL = "https://app.gotsms.com/api"
TZ = ZoneInfo("Europe/Moscow")

HEADERS = {
    "Authorization": f"Bearer {GOTSMS_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

buying_task: Optional[asyncio.Task] = None
is_buying = False
bought_count = 0
status = "Остановлен"

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🕐 Установить время")],
        [KeyboardButton(text="▶️ Запустить сейчас"), KeyboardButton(text="🛑 Остановить")],
        [KeyboardButton(text="📊 Статус")],
    ],
    resize_keyboard=True,
)


def is_admin(message: Message) -> bool:
    return message.from_user and message.from_user.id == ADMIN_ID and message.chat.type == "private"


async def api_request(method: str, endpoint: str, json_data: dict = None) -> dict:
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=HEADERS, json=json_data, timeout=30) as resp:
            try:
                return await resp.json()
            except Exception:
                text = await resp.text()
                logger.error(f"Не JSON: {text[:200]}")
                return {"success": False}


async def find_plan_id() -> Optional[str]:
    """Ищем тариф через search + fallback по всем страницам"""
    try:
        # 1. Сначала быстрый поиск через search
        data = await api_request(
            "GET",
            f"rents/plans?search={SERVICE_NAME}&per_page=100&include_unavailable=1"
        )
        plans = data.get("data", [])
        logger.info(f"search='{SERVICE_NAME}' → найдено планов: {len(plans)}")

        for p in plans:
            name = p.get("service", {}).get("name", "")
            price = float(p.get("price", 0))
            available = p.get("is_available")
            logger.info(f"  {name} | ${price} | available={available}")

            if SERVICE_NAME.lower() in name.lower() and abs(price - TARGET_PRICE) < 0.01:
                if available is True:
                    logger.info(f"Выбран (search): {name} | ${price}")
                    return p["id"]

        # 2. Если через search не нашли за $1 — берём любой тариф этого сервиса
        for p in plans:
            name = p.get("service", {}).get("name", "")
            if SERVICE_NAME.lower() in name.lower() and p.get("is_available") is True:
                logger.info(f"Выбран любой доступный: {name} | ${p.get('price')}")
                return p["id"]

        # 3. Fallback — листаем страницы
        page = 1
        while page <= 15:
            data = await api_request(
                "GET",
                f"rents/plans?per_page=100&page={page}&include_unavailable=1"
            )
            plans = data.get("data", [])
            meta = data.get("meta", {})
            total = meta.get("last_page", 1)
            logger.info(f"Страница {page}/{total}")

            for p in plans:
                name = p.get("service", {}).get("name", "")
                price = float(p.get("price", 0))
                if SERVICE_NAME.lower() in name.lower() and abs(price - TARGET_PRICE) < 0.01:
                    logger.info(f"Найден на стр.{page}: {name} | ${price}")
                    return p["id"]

            if page >= total or not plans:
                break
            page += 1

        logger.warning(f"Не найден: {SERVICE_NAME} за ${TARGET_PRICE}")
        return None

    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return None


async def buy_bulk(plan_id: str, quantity: int = 25) -> dict:
    try:
        return await api_request("POST", "rents/bulk", {
            "plan_id": plan_id,
            "quantity": quantity
        })
    except Exception as e:
        logger.error(f"Ошибка bulk: {e}")
        return {"success": False}


async def buying_loop(plan_id: str):
    global is_buying, bought_count, status

    is_buying = True
    bought_count = 0
    status = "Скупаю"
    last_report = 0

    await bot.send_message(
        ADMIN_ID,
        f"🚀 Скупка <b>{SERVICE_NAME}</b>\n"
        f"Пачки по 25 | 0.5 сек\n"
        f"/stop — стоп",
        parse_mode="HTML",
        reply_markup=menu
    )

    while is_buying:
        result = await buy_bulk(plan_id, 25)

        if result.get("success"):
            rented = result.get("meta", {}).get("rented", 0)
            if rented > 0:
                bought_count += rented
                data = result.get("data", [])
                last_phone = data[-1].get("phone", "—") if data else "—"

                if bought_count - last_report >= 25:
                    last_report = bought_count
                    await bot.send_message(
                        ADMIN_ID,
                        f"✅ <b>{bought_count}</b> | +{rented}\n<code>{last_phone}</code>",
                        parse_mode="HTML"
                    )

        await asyncio.sleep(0.5)

    status = "Остановлен"
    await bot.send_message(
        ADMIN_ID,
        f"🛑 Стоп. Куплено: <b>{bought_count}</b>",
        parse_mode="HTML",
        reply_markup=menu
    )


async def wait_and_start(target: datetime):
    global status
    status = f"Ожидание {target.strftime('%H:%M')}"
    wait = (target - datetime.now(TZ)).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    if not is_buying:
        plan_id = await find_plan_id()
        if plan_id:
            await buying_loop(plan_id)
        else:
            await bot.send_message(ADMIN_ID, f"❌ {SERVICE_NAME} не найден")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(
        f"🤖 GotSMS Buyer\nСервис: <b>{SERVICE_NAME}</b>\nЦена: ${TARGET_PRICE}",
        parse_mode="HTML",
        reply_markup=menu
    )


@dp.message(Command("start_now"))
@dp.message(F.text == "▶️ Запустить сейчас")
async def cmd_start_now(message: Message):
    global buying_task, is_buying

    if not is_admin(message):
        return
    if is_buying:
        await message.answer("⚠️ Уже скупаю")
        return

    await message.answer(f"🔍 Ищу <b>{SERVICE_NAME}</b>...", parse_mode="HTML")

    plan_id = await find_plan_id()
    if not plan_id:
        await message.answer(
            f"❌ <b>{SERVICE_NAME}</b> не найден.\n"
            f"Проверь SERVICE_NAME в Railway Variables.",
            parse_mode="HTML"
        )
        return

    buying_task = asyncio.create_task(buying_loop(plan_id))


@dp.message(Command("start_at"))
async def cmd_start_at(message: Message, command: CommandObject):
    global buying_task

    if not is_admin(message):
        return
    if is_buying:
        await message.answer("⚠️ Уже скупаю")
        return

    if not command.args:
        await message.answer("Пример: /start_at 21:00")
        return

    try:
        h, m = map(int, command.args.strip().split(":"))
        t = time(hour=h, minute=m)
    except Exception:
        await message.answer("Формат: 21:00")
        return

    now = datetime.now(TZ)
    target = datetime.combine(now.date(), t, tzinfo=TZ)
    if target <= now:
        target += timedelta(days=1)

    await message.answer(f"⏳ Старт {target.strftime('%d.%m %H:%M')} МСК")
    buying_task = asyncio.create_task(wait_and_start(target))


@dp.message(F.text == "🕐 Установить время")
async def ask_time(message: Message):
    if not is_admin(message):
        return
    await message.answer("Напиши: /start_at 21:00")


@dp.message(Command("stop"))
@dp.message(F.text == "🛑 Остановить")
async def cmd_stop(message: Message):
    global is_buying, buying_task, status

    if not is_admin(message):
        return

    is_buying = False
    status = "Остановлен"

    if buying_task and not buying_task.done():
        buying_task.cancel()
        try:
            await buying_task
        except asyncio.CancelledError:
            pass

    await message.answer(f"🛑 Стоп\nКуплено: <b>{bought_count}</b>", parse_mode="HTML", reply_markup=menu)


@dp.message(Command("status"))
@dp.message(F.text == "📊 Статус")
async def cmd_status(message: Message):
    if not is_admin(message):
        return
    await message.answer(
        f"Статус: <b>{status}</b>\n"
        f"Сервис: {SERVICE_NAME}\n"
        f"Куплено: <b>{bought_count}</b>\n"
        f"МСК: {datetime.now(TZ).strftime('%H:%M:%S')}",
        parse_mode="HTML",
        reply_markup=menu
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
