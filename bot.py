
import asyncio
import json
import os
from pathlib import Path

import requests
from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://www.dtek-krem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

CITY = "с-ще Глеваха"
STREET = "вул. Вокзальна"
HOUSE = "53"

STATE_FILE = Path("state.json")


def send_message(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=20,
    )
    response.raise_for_status()


async def check_schedule():
    cookies, csrf_token = await get_cleared_cookies(
        SCHEDULE_URL
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": SCHEDULE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": (
            "application/x-www-form-urlencoded; charset=UTF-8"
        ),
    }

    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    async with AsyncSession(
        timeout=20,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    ) as session:
        async with DtekClient(
            "krem",
            ajax_url=AJAX_URL,
            session=session,
        ) as client:

            address = await client.get_group_by_address(
                city=CITY,
                street=STREET,
                house_number=HOUSE,
            )

            group = str(
                address.group_display_name or address.group_id
            )

            print(f"Група за даними ДТЕК: {group}")

            if "1.2" not in group:
                send_message(
                    "⚠️ Не вдалося підтвердити чергу 1.2.\n"
                    f"📍 {CITY}, {STREET}, {HOUSE}\n"
                    f"Сайт ДТЕК повернув: {group}\n"
                    "Перевір адресу на офіційному сайті ДТЕК."
                )
                return

            schedule = await client.get_today_schedule(
                city=CITY,
                street=STREET,
                house_number=HOUSE,
            )

    if not schedule:
        print("Графік на сьогодні недоступний.")
        return

    current = {
        str(slot): str(
            getattr(status, "value", status)
        ).upper()
        for slot, status in schedule.items()
    }

    previous = None

    if STATE_FILE.exists():
        try:
            previous = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            previous = None

    STATE_FILE.write_text(
        json.dumps(
            {
                "group": group,
                "schedule": current,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if previous is None:
        title = "✅ Почав відстежувати графік ДТЕК"
    elif (
        previous.get("group") != group
        or previous.get("schedule") != current
    ):
        title = "🔔 Графік ДТЕК змінився"
    else:
        print("Змін графіка не виявлено.")
        return

    outage_labels = {
        "NO": "відключення за графіком",
        "FIRST": "відключення в першій половині інтервалу",
        "SECOND": "відключення в другій половині інтервалу",
        "MAYBE": "можливе відключення",
        "MFIRST": "можливе відключення в першій половині",
        "MSECOND": "можливе відключення в другій половині",
    }

    message = (
        f"{title}\n"
        f"📍 {CITY}, {STREET}, {HOUSE}\n"
        f"⚡ Черга: {group}\n\n"
        "🔌 Години без світла:\n"
    )

    outage_found = False

    for slot, status in current.items():
        if status not in outage_labels:
            continue

        try:
            number = int(slot)
            start_hour = (number - 1) % 24
            end_hour = number % 24
            time_text = (
                f"{start_hour:02d}:00–{end_hour:02d}:00"
            )
        except ValueError:
            time_text = f"Інтервал {slot}"

        message += (
            f"• {time_text} — {outage_labels[status]}\n"
        )
        outage_found = True

    if not outage_found:
        message += "За графіком відключень немає.\n"

    send_message(message)
    print("Повідомлення надіслано в Telegram.")


if __name__ == "__main__":
    try:
        asyncio.run(check_schedule())
    except Exception as error:
        print(
            f"Помилка перевірки: "
            f"{type(error).__name__}: {error}"
        )
        raise
