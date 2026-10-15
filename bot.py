
import asyncio
import json
import os
from pathlib import Path

import requests
from dtek_client import DtekClient

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CITY = "с-ще Глеваха"
STREET = "вул. Вокзальна"
HOUSE = "53"
EXPECTED_GROUP = "1.2"

STATE_FILE = Path("state.json")


def send_message(text):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    response.raise_for_status()


async def check_schedule():
    async with DtekClient("krem") as client:
        address = await client.get_group_by_address(
            city=CITY,
            street=STREET,
            house_number=HOUSE,
        )

        group = str(address.group_display_name or address.group_id)

        print(f"Група за даними сайту: {group}")

        if EXPECTED_GROUP not in group:
            send_message(
                "⚠️ Не вдалося підтвердити чергу 1.2 для адреси "
                f"{CITY}, {STREET}, {HOUSE}.\n"
                f"Сайт повернув: {group}\n"
                "Перевір адресу перед використанням графіка."
            )
            return

        schedule = await client.get_today_schedule(
            city=CITY,
            street=STREET,
            house_number=HOUSE,
        )

        if not schedule:
            print("Сьогоднішній графік поки недоступний.")
            return

        current = {
            str(time): getattr(status, "value", str(status))
            for time, status in schedule.items()
        }

        previous = None
        if STATE_FILE.exists():
            previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        STATE_FILE.write_text(
            json.dumps(
                {"group": group, "schedule": current},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if previous is None:
            message = (
                "✅ Почав відстежувати графік ДТЕК!\n"
                f"📍 {CITY}, {STREET}, {HOUSE}\n"
                f"⚡ Черга: {group}\n\n"
                "Поточні дані на сьогодні:\n"
            )
        elif previous.get("schedule") != current:
            message = (
                "🔔 Графік ДТЕК змінився!\n"
                f"📍 {CITY}, {STREET}, {HOUSE}\n"
                f"⚡ Черга: {group}\n\n"
                "Оновлені дані на сьогодні:\n"
            )
        else:
            print("Змін графіка не виявлено.")
            return

        labels = {
            "YES": "світло є",
            "NO": "відключення",
            "MAYBE": "можливе відключення",
            "FIRST": "відключення в першій половині інтервалу",
            "SECOND": "відключення в другій половині інтервалу",
            "MFIRST": "можливе відключення в першій половині",
            "MSECOND": "можливе відключення в другій половині",
            "UNKNOWN": "невідомий статус",
        }

        for time, status in current.items():
            message += f"{time}: {labels.get(status, status)}\n"

        send_message(message)


if __name__ == "__main__":
    try:
        asyncio.run(check_schedule())
    except Exception as error:
        print(f"Помилка перевірки графіка: {type(error).__name__}: {error}")
        raise
