import logging
import os
import sys

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Настройки для Render
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Конфигурация городов и API
# Мы храним ID регионов и DSO, чтобы формировать ссылки динамически
CITIES_CONFIG = {
    "kyiv": {"name": "Киев", "region": 25, "dso": 902},
    "dnipro": {"name": "Днепр", "region": 3, "dso": 301},
}

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---


def get_api_url(city_key):
    """Формирует URL на основе ключа города"""
    config = CITIES_CONFIG.get(city_key)
    if not config:
        return None
    return f"https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/{config['region']}/dsos/{config['dso']}/planned-outages"


def format_time(minutes):
    """Переводит минуты (напр. 90) в формат 01:30"""
    h = minutes // 60
    m = minutes % 60
    return f"{h:02}:{m:02}"


def parse_schedule(data, day_key):
    """Парсит данные конкретного дня"""
    if day_key not in data:
        return "Нет данных."

    day_data = data[day_key]
    slots = day_data.get("slots", [])
    if not slots:
        return "График пуст (свет есть или нет данных)."

    result_lines = []
    date_raw = day_data.get("date", "").split("T")[0]
    result_lines.append(f"📅 <b>Дата: {date_raw}</b>")

    for slot in slots:
        start_time = format_time(slot["start"])
        end_time = format_time(slot["end"])
        status_type = slot["type"]

        if status_type == "Definite":
            icon = "🔴"
            text = "Отключение"
        elif status_type == "NotPlanned":
            icon = "🟢"
            text = "Свет есть"
        else:
            icon = "⚪️"
            text = status_type

        result_lines.append(f"{icon} {start_time} - {end_time} : {text}")

    return "\n".join(result_lines)


# --- ЛОГИКА API ---


async def fetch_city_data(city_key):
    """Загружает полный JSON для города"""
    url = get_api_url(city_key)
    if not url:
        return None

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logging.error(f"Yasno API Error: {response.status}")
                    return None
                return await response.json()
        except Exception as e:
            logging.error(f"Exception fetching data: {e}")
            return None


# --- КЛАВИАТУРЫ ---


def get_cities_keyboard():
    """Кнопки выбора города"""
    buttons = []
    for key, data in CITIES_CONFIG.items():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🏙 {data['name']}", callback_data=f"city:{key}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_groups_keyboard(city_key, groups_list):
    """Кнопки выбора группы (генерируются динамически)"""
    # Сортируем группы, чтобы 1.1 шло перед 1.2
    sorted_groups = sorted(groups_list)

    # Делаем по 2 или 3 кнопки в ряд для красоты
    keyboard = []
    row = []
    for group in sorted_groups:
        row.append(
            InlineKeyboardButton(
                text=f"Гр. {group}", callback_data=f"group:{city_key}:{group}"
            )
        )
        if len(row) == 3:  # 3 кнопки в ряд
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Кнопка Назад
    keyboard.append(
        [InlineKeyboardButton(text="🔙 Назад к городам", callback_data="start")]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_refresh_keyboard(city_key, group_id):
    """Кнопки под графиком"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=f"group:{city_key}:{group_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Выбрать другую группу", callback_data=f"city:{city_key}"
                )
            ],
        ]
    )


# --- ОБРАБОТЧИКИ (HANDLERS) ---


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Выбери свой город, чтобы увидеть график отключений:",
        reply_markup=get_cities_keyboard(),
    )


# Обработка кнопки "Назад" (которая тоже шлет callback 'start')
@dp.callback_query(F.data == "start")
async def cb_start(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Выбери город:", reply_markup=get_cities_keyboard()
    )


# Обработка выбора города (city:kyiv)
@dp.callback_query(F.data.startswith("city:"))
async def cb_city_selected(callback: types.CallbackQuery):
    city_key = callback.data.split(":")[1]

    await callback.answer("Загружаю список групп...")

    data = await fetch_city_data(city_key)

    if not data:
        await callback.message.edit_text(
            "❌ Не удалось получить данные от Yasno. Попробуйте позже.",
            reply_markup=get_cities_keyboard(),
        )
        return

    # Получаем список групп (ключи верхнего уровня JSON)
    # Yasno возвращает JSON вида {"1.1": {...}, "1.2": {...}}
    groups = list(data.keys())

    if not groups:
        await callback.message.edit_text(
            f"❌ Для города {CITIES_CONFIG[city_key]['name']} данные о группах не найдены.",
            reply_markup=get_cities_keyboard(),
        )
        return

    await callback.message.edit_text(
        f"📍 Город: <b>{CITIES_CONFIG[city_key]['name']}</b>.\nВыберите вашу группу:",
        parse_mode="HTML",
        reply_markup=get_groups_keyboard(city_key, groups),
    )


# Обработка выбора группы (group:kyiv:3.1)
@dp.callback_query(F.data.startswith("group:"))
async def cb_group_selected(callback: types.CallbackQuery):
    # Разбираем callback_data
    _, city_key, group_id = callback.data.split(":")

    await callback.answer("Загружаю график...")

    data = await fetch_city_data(city_key)

    if not data or group_id not in data:
        await callback.message.answer("❌ Ошибка получения данных графика.")
        return

    group_data = data[group_id]
    city_name = CITIES_CONFIG[city_key]["name"]

    updated_on = group_data.get("updatedOn", "Неизвестно")

    msg = f"💡 <b>{city_name}, Группа {group_id}</b>\n"
    msg += f"<i>Обновлено: {updated_on}</i>\n\n"

    msg += "👇 <b>СЕГОДНЯ</b>:\n"
    msg += parse_schedule(group_data, "today")
    msg += "\n\n👇 <b>ЗАВТРА</b>:\n"
    msg += parse_schedule(group_data, "tomorrow")

    # Редактируем сообщение или отправляем новое (зависит от ситуации, edit красивее)
    try:
        await callback.message.edit_text(
            msg,
            parse_mode="HTML",
            reply_markup=get_refresh_keyboard(city_key, group_id),
        )
    except Exception:
        # Если сообщение такое же (при обновлении), телеграм может выдать ошибку, игнорируем
        pass


# --- ЗАПУСК ВЕБ-СЕРВЕРА (Webhooks) ---
async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        await bot.set_webhook(f"{WEBHOOK_URL}{webhook_path}")
        logging.info(f"Webhook set to {WEBHOOK_URL}{webhook_path}")


async def handle_root(request):
    """Обработчик для мониторинга (health check)"""
    return web.Response(text="Bot is alive!", status=200)


def main():
    app = web.Application()
    app.router.add_get("/", handle_root)
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=f"/webhook/{BOT_TOKEN}")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(lambda _: on_startup(bot))
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
