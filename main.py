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
# Токен берем из переменных окружения (настроим в Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
YASNO_URL = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/3/dsos/301/planned-outages"
TARGET_GROUP = "3.1"

# Настройки для Render
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))
# URL вашего приложения на Render (добавим позже)
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)


# --- ЛОГИКА ОБРАБОТКИ ВРЕМЕНИ ---
def format_time(minutes):
    """Переводит минуты (напр. 90) в формат 01:30"""
    h = minutes // 60
    m = minutes % 60
    # Если 24:00, показываем как 24:00 (или 00:00 следующего дня, но для графика удобнее 24:00)
    return f"{h:02}:{m:02}"


def parse_schedule(data, day_key):
    """Парсит данные конкретного дня (today/tomorrow)"""
    if day_key not in data:
        return "Нет данных."

    day_data = data[day_key]
    slots = day_data.get("slots", [])
    if not slots:
        return "График пуст (возможно, нет отключений)."

    result_lines = []
    # Дата из JSON, например "2026-01-09T00..."
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
            text = status_type  # На случай других статусов

        result_lines.append(f"{icon} {start_time} - {end_time} : {text}")

    return "\n".join(result_lines)


# --- ЛОГИКА ПОЛУЧЕНИЯ ДАННЫХ ---
async def get_yasno_schedule():
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(YASNO_URL) as response:
                if response.status != 200:
                    return "❌ Ошибка доступа к API Yasno."

                full_data = await response.json()

                # Ищем нашу группу
                group_data = full_data.get(TARGET_GROUP)
                if not group_data:
                    return f"❌ Группа {TARGET_GROUP} не найдена в ответе API."

                # Формируем отчет
                updated_on = group_data.get("updatedOn", "Неизвестно")
                msg = f"💡 <b>Группа {TARGET_GROUP}</b>\nObnovleno: {updated_on}\n\n"

                msg += "👇 <b>СЕГОДНЯ</b>:\n"
                msg += parse_schedule(group_data, "today")
                msg += "\n\n👇 <b>ЗАВТРА</b>:\n"
                msg += parse_schedule(group_data, "tomorrow")

                return msg
        except Exception as e:
            logging.error(f"Error fetching data: {e}")
            return "❌ Произошла ошибка при получении данных."


# --- ОБРАБОТЧИКИ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💡 Получить график", callback_data="get_schedule"
                )
            ]
        ]
    )
    await message.answer(
        "Привет! Нажми кнопку, чтобы получить график для группы 3.1", reply_markup=kb
    )


@dp.callback_query(F.data == "get_schedule")
async def callback_schedule(callback: types.CallbackQuery):
    await callback.answer("Загружаю данные...")
    schedule_text = await get_yasno_schedule()
    # Кнопка для обновления
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="get_schedule")]
        ]
    )
    await callback.message.answer(schedule_text, parse_mode="HTML", reply_markup=kb)


# --- ЗАПУСК ВЕБ-СЕРВЕРА (Webhooks) ---
async def on_startup(bot: Bot):
    # Устанавливаем вебхук на URL, который выдаст Render
    if WEBHOOK_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        await bot.set_webhook(f"{WEBHOOK_URL}{webhook_path}")
        logging.info(f"Webhook set to {WEBHOOK_URL}{webhook_path}")


def main():
    # Создаем aiohttp приложение
    app = web.Application()

    # Обработчик запросов от Telegram
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=f"/webhook/{BOT_TOKEN}")

    # Настраиваем приложение и запускаем
    setup_application(app, dp, bot=bot)

    # Регистрируем функцию запуска (установка вебхука)
    app.on_startup.append(lambda _: on_startup(bot))

    # Запускаем сервер
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
