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
            text = await resp.text()
            try:
                data = await resp.json()
            except:
                logger.error(f"Не JSON ответ: {text[:300]}")
                return {"success": False}
            return data


async def find_plan_id() -> Optional[str]:
    try:
        # Берём все планы, включая недоступные
        data = await api_request("GET", "rents/plans?per_page=100&include_unavailable=1")
        plans = data.get("data", [])

        logger.info(f"Получено планов: {len(plans)}")
        logger.info(f"Ищем сервис содержащий: '{SERVICE_NAME}'")

        suitable = []
        for p in plans:
            name = p.get("service", {}).get("name", "")
            available = p.get("is_available")
            price = p.get("price")
            plan_id = p.get("id")
            logger.info(f"  {name} | available={available} | price={price} | id={plan_id}")

            if SERVICE_NAME.lower() in name.lower():
                suitable.append(p)

        if not suitable:
            logger.warning("Ни одного плана с таким названием не найдено")
            return None

        # Сначала доступные
        available = [p for p in suitable if p.get("is_available") is True]
        if available:
            available.sort(key=lambda x: float(x.get("price", 9999)))
            best = available[0]
            logger.info(f"Выбран доступный: {best['service']['name']} | ${best.get('price')}")
            return best["id"]

        # Если доступных нет — берём любой
        suitable.sort(key=lambda x: float(x.get("price", 9999)))
        best = suitable[0]
        logger.info(f"Доступных нет, берём любой: {best['service']['name']} | ${best.get('price')}")
        return best["id"]

    except Exception as e:
        logger.error(f"Ошибка поиска плана: {e}")
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
        f"🚀 Начал скупку <b>{SERVICE_NAME}</b>\n"
        f"Пачки по 25 номеров каждые 0.5 сек\n"
        f"Остановить: /stop",
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
                        f"✅ Куплено: <b>{bought_count}</b>\n"
                        f"Последняя пачка: +{rented}\n"
                        f"Последний: <code>{last_phone}</code>",
                        parse_mode="HTML"
                    )

        await asyncio.sleep(0.5)

    status = "Остановлен"
    await bot.send_message(
        ADMIN_ID,
        f"🛑 Остановлено. Куплено: <b>{bought_count}</b>",
        parse_mode="HTML",
        reply_markup=menu
    )


async def wait_and_start(target: datetime):
    global status
    status = f"Ожидание до {target.strftime('%H:%M')}"
    wait_seconds = (target - datetime.now(TZ)).total_seconds()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    if not is_buying:
        plan_id = await find_plan_id()
        if plan_id:
            await buying_loop(plan_id)
        else:
            await bot.send_message(ADMIN_ID, f"❌ Не найден тариф для {SERVICE_NAME}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(
        f"🤖 GotSMS Auto-Buyer\nСервис: <b>{SERVICE_NAME}</b>",
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
        await message.answer("⚠️ Уже идёт скупка")
        return

    await message.answer("🔍 Ищу тариф...")

    plan_id = await find_plan_id()
    if not plan_id:
        await message.answer(
            f"❌ Не найден тариф для <b>{SERVICE_NAME}</b>\n"
            f"Проверь логи Railway — там будет список всех сервисов, которые видит API.",
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
        await message.answer("⚠️ Уже идёт скупка")
        return

    if not command.args:
        await message.answer("Пример: /start_at 21:00")
        return

    try:
        hour, minute = map(int, command.args.strip().split(":"))
        t = time(hour=hour, minute=minute)
    except Exception:
        await message.answer("Формат: 21:00")
        return

    now = datetime.now(TZ)
    target = datetime.combine(now.date(), t, tzinfo=TZ)
    if target <= now:
        target += timedelta(days=1)

    await message.answer(f"⏳ Запуск в {target.strftime('%d.%m %H:%M')} МСК")
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

    await message.answer(f"🛑 Остановлено\nКуплено: <b>{bought_count}</b>", parse_mode="HTML", reply_markup=menu)


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
