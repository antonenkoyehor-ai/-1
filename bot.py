
import asyncio
import json
import os
from datetime import datetime
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
            "parse_mode": "Markdown",
        },
        timeout=20,
    )
    response.raise_for_status()


def status_value(status):
    return str(getattr(status, "value", status)).upper()


def get_outage_intervals(schedule):
    """
    Підтримує годинні слоти 1–24.
    FIRST і SECOND трактуються як половини відповідної години.
    """
    intervals = []
    uncertain = False

    for slot, raw_status in schedule.items():
        status = status_value(raw_status)

        try:
            number = int(slot)
        except (ValueError, TypeError):
            continue

        if not 1 <= number <= 24:
            continue

        hour_start = (number - 1) * 60
        hour_end = number * 60

        if status == "NO":
            intervals.append((hour_start, hour_end))
        elif status == "FIRST":
            intervals.append((hour_start, hour_start + 30))
        elif status == "SECOND":
            intervals.append((hour_start + 30, hour_end))
        elif status in ("MAYBE", "MFIRST", "MSECOND"):
            uncertain = True

    intervals.sort()

    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (
                merged[-1][0],
                max(merged[-1][1], end),
            )
        else:
            merged.append((start, end))

    return merged, uncertain


def format_time(minutes):
    minutes %= 24 * 60
    hour = minutes // 60
    minute = minutes % 60
    return f"{hour:02d}:{minute:02d}"


def format_duration(minutes):
    hours = minutes // 60
    mins = minutes % 60

    if mins == 0:
        return f"{hours} год" if hours == 1 else f"{hours} годин"

    if hours == 0:
        return f"{mins} хв"

    return f"{hours} год {mins} хв"


def build_message(group, schedule):
    intervals, uncertain = get_outage_intervals(schedule)

    # Telegram Markdown: зірочки виділяють рядки жирним.
    lines = [f"*Підгрупа {group}*"]

    total_minutes = sum(end - start for start, end in intervals)

    if not intervals:
        lines.append("Світло є за опублікованим графіком.")
    else:
        for start, end in intervals:
            duration = end - start
            lines.append(
                f"🕒 {format_time(start)} - {format_time(end)} "
                f"(~{format_duration(duration)})"
            )

        lines.append(
            f"🔴 Загалом {format_duration(total_minutes)} без світла"
        )

    if uncertain:
        lines.append(
            "\n⚠️ Є інтервали з можливими відключеннями, "
            "які не включені до загальної тривалості."
        )

    return "\n".join(lines)


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

            if "1.2" not in group:
                send_message(
                    "⚠️ Не вдалося підтвердити підгрупу 1.2.\n"
                    f"{CITY}, {STREET}, {HOUSE}\n"
                    f"ДТЕК повернув: {group}"
                )
                return

            schedule = await client.get_today_schedule(
                city=CITY,
                street=STREET,
                house_number=HOUSE,
            )

    if not schedule:
        print("Графік недоступний.")
        return

    current = {
        str(slot): status_value(status)
        for slot, status in schedule.items()
    }

    today = datetime.now().strftime("%Y-%m-%d")

    previous = None
    if STATE_FILE.exists():
        try:
            previous = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            pass

    # Надсилаємо графік уперше за день або якщо він змінився.
    if (
        previous
        and previous.get("date") == today
        and previous.get("group") == group
        and previous.get("schedule") == current
    ):
        print("Графік не змінився.")
        return

    STATE_FILE.write_text(
        json.dumps(
            {
                "date": today,
                "group": group,
                "schedule": current,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    send_message(build_message(group, schedule))
    print("Графік надіслано.")


if __name__ == "__main__":
    try:
        asyncio.run(check_schedule())
    except Exception as error:
        print(f"Помилка: {type(error).__name__}: {error}")
        raise
