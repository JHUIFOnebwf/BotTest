import asyncio
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

TOKEN = os.environ['BOT_TOKEN']
KEY = os.environ['GOTSMS_API_KEY']
ADMIN = int(os.environ['ADMIN_ID'])
SERVICE = os.getenv('SERVICE_NAME', 'TestprojectsOne')
BUDGET = Decimal('300')
TZ = ZoneInfo('Europe/Moscow')
BASE = 'https://app.gotsms.com'
bot = Bot(TOKEN)
dp = Dispatcher()
task = None
scheduled = None
status = 'Остановлен'
lock = asyncio.Lock()
menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='🕐 Установить время')],
    [KeyboardButton(text='▶️ Запустить сейчас'), KeyboardButton(text='🛑 Остановить')],
    [KeyboardButton(text='💳 Купить тестовый номер'), KeyboardButton(text='📊 Статус')],
], resize_keyboard=True)

class Input(StatesGroup):
    time = State()
    confirm = State()

def allowed(m):
    return m.from_user and m.from_user.id == ADMIN and m.chat.type == 'private'

def session():
    return aiohttp.ClientSession(headers={'Authorization': f'Bearer {KEY}', 'Accept': 'application/json'}, timeout=aiohttp.ClientTimeout(total=20))

async def plans(s):
    async with s.get(BASE+'/api/rents/plans', params={'search': SERVICE, 'duration_type':'day','billing_type':'duration','per_page':100}) as r:
        r.raise_for_status()
        data = await r.json()
    return sorted([p for p in data['data'] if p['service']['name'].casefold()==SERVICE.casefold() and p['duration_in_minutes']==1440 and p['is_available'] and Decimal('0') < Decimal(str(p['price'])) <= BUDGET], key=lambda p: Decimal(str(p['price'])))

async def monitor(target=None):
    global status, scheduled
    try:
        if target:
            await asyncio.sleep(max(0,(target-datetime.now(TZ)).total_seconds()))
        scheduled=None
        status='Мониторинг'
        async with session() as s:
            while True:
                try:
                    p=await plans(s)
                    if p:
                        await bot.send_message(ADMIN,f'Номер доступен: {SERVICE}, цена {p[0]["price"]}. Нажми кнопку покупки.',reply_markup=menu)
                        break
                except aiohttp.ClientResponseError as e:
                    if e.status in (401,403):
                        await bot.send_message(ADMIN,'Ошибка авторизации GotSMS.'); break
                    if e.status==429:
                        await asyncio.sleep(90); continue
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(30)
    except asyncio.CancelledError:
        raise
    except Exception:
        await bot.send_message(ADMIN,'Ошибка мониторинга. Проверь настройки API.')
    finally:
        status='Остановлен'; scheduled=None

async def cancel():
    global task, scheduled, status
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    task=None; scheduled=None; status='Остановлен'

@dp.message(Command('start'))
async def start(m:Message,state:FSMContext):
    if not allowed(m): return
    await state.clear()
    await m.answer(f'GotSMS Test Bot\nСервис: {SERVICE}\nАренда: сутки. Время: МСК.',reply_markup=menu)

@dp.message(F.text=='🕐 Установить время')
async def ask(m:Message,state:FSMContext):
    if not allowed(m): return
    await state.set_state(Input.time)
    await m.answer('Введи время ЧЧ:ММ по Москве.')

async def schedule(m,state,value):
    global task, scheduled, status
    try:
        t=datetime.strptime(value,'%H:%M').time()
    except ValueError:
        await m.answer('Формат: 21:30'); return
    async with lock:
        await cancel()
        now=datetime.now(TZ)
        target=datetime.combine(now.date(),t,tzinfo=TZ)
        if target<=now: target+=timedelta(days=1)
        scheduled=target; status='Ожидание'; task=asyncio.create_task(monitor(target))
    await state.clear()
    await m.answer(f'Запуск: {target:%d.%m.%Y %H:%M} МСК',reply_markup=menu)

@dp.message(Command('start_at'))
async def at(m:Message,state:FSMContext):
    if not allowed(m): return
    parts=m.text.split(maxsplit=1)
    if len(parts)==1: await ask(m,state)
    else: await schedule(m,state,parts[1].strip())

@dp.message(Command('start_now'))
@dp.message(F.text=='▶️ Запустить сейчас')
async def now(m:Message,state:FSMContext):
    global task,status
    if not allowed(m): return
    await state.clear()
    async with lock:
        await cancel(); status='Мониторинг'; task=asyncio.create_task(monitor())
    await m.answer('Мониторинг запущен.',reply_markup=menu)

@dp.message(Command('stop'))
@dp.message(F.text=='🛑 Остановить')
async def stop(m:Message,state:FSMContext):
    if not allowed(m): return
    async with lock: await cancel(); await state.clear()
    await m.answer('Остановлено. Уже отправленные платежные запросы не отменяются.',reply_markup=menu)

@dp.message(Command('status'))
@dp.message(F.text=='📊 Статус')
async def show(m:Message):
    if not allowed(m): return
    await m.answer(f'Статус: {status}\nЗапуск: {scheduled or "нет"}\nМСК: {datetime.now(TZ):%H:%M:%S}',reply_markup=menu)

@dp.message(F.text=='💳 Купить тестовый номер')
async def buy(m:Message,state:FSMContext):
    if not allowed(m): return
    await state.clear()
    try:
        async with session() as s: p=await plans(s)
        if not p:
            await m.answer('Нет подходящего тарифа.'); return
        await state.update_data(plan_id=p[0]['id'],price=str(p[0]['price']))
        await state.set_state(Input.confirm)
        await m.answer(f'Один номер на сутки. Цена: {p[0]["price"]}. Для реальной оплаты отправь ПОДТВЕРЖДАЮ. Для отмены /stop.')
    except Exception:
        await m.answer('Не удалось получить тариф. Проверь API и название сервиса.')

@dp.message(Input.confirm,F.text=='ПОДТВЕРЖДАЮ')
async def confirm(m:Message,state:FSMContext):
    if not allowed(m): return
    async with lock:
        quote=await state.get_data(); await state.clear()
        if not quote.get('plan_id'): return
        try:
            async with session() as s:
                p=next((x for x in await plans(s) if x['id']==quote['plan_id']),None)
                if not p or Decimal(str(p['price']))!=Decimal(quote['price']):
                    await m.answer('Тариф изменился. Запроси новую цену.'); return
                async with s.post(BASE+'/api/rents',json={'plan_id':p['id']}) as r:
                    if r.status!=201:
                        await m.answer(f'Ответ API: HTTP {r.status}. Проверь историю аренды перед повтором.'); return
                    result=await r.json()
                if not result.get('success'):
                    await m.answer('Покупка не подтверждена API. Проверь историю.'); return
                d=result['data']
                await m.answer(f'Аренда создана.\nНомер: {d.get("phone", "см. кабинет")}\nID: {d.get("id")}\nЦена: {d.get("price", "см. кабинет")}',reply_markup=menu)
        except Exception:
            await m.answer('Результат неизвестен. Проверь историю GotSMS перед повтором. Автоповтора нет.')

@dp.message(Input.time)
async def time_input(m:Message,state:FSMContext):
    if allowed(m): await schedule(m,state,(m.text or '').strip())

async def main():
    await dp.start_polling(bot,handle_as_tasks=False)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
