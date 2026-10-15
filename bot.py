
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
KYIV_TZ = ZoneInfo("Europe/Kyiv")


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
    return str(
        getattr(status, "value", status)
    ).upper()


def schedule_to_dict(schedule):
    if not schedule:
        return None

    return {
        str(slot): status_value(status)
        for slot, status in schedule.items()
    }


def get_outage_intervals(schedule):
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

        start = (number - 1) * 60
        end = number * 60

        if status == "NO":
            intervals.append((start, end))

        elif status == "FIRST":
            intervals.append((start, start + 30))

        elif status == "SECOND":
            intervals.append((start + 30, end))

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
    hours = minutes // 60
    mins = minutes % 60

    return f"{hours:02d}:{mins:02d}"


def format_approx_duration(minutes):
    hours = minutes // 60
    mins = minutes % 60

    if mins == 0:
        return f"{hours} год"

    if hours == 0:
        return f"{mins} хв"

    return f"{hours} год {mins} хв"


def format_total_duration(minutes):
    hours = minutes // 60
    mins = minutes % 60

    if mins:
        return f"{hours} год {mins} хв"

    if hours == 1:
        return "1 годину"

    if 2 <= hours % 10 <= 4 and not 12 <= hours % 100 <= 14:
        return f"{hours} години"

    return f"{hours} годин"


def format_schedule(title, schedule):
    lines = [f"*📅 {title}*"]

    if schedule is None:
        lines.append("Графік ще не опубліковано.")
        return lines

    intervals, uncertain = get_outage_intervals(schedule)

    if not intervals:
        if uncertain:
            lines.append(
                "Точні години відключень ще не визначені."
            )
        else:
            lines.append(
                "За опублікованим графіком відключень немає."
            )

    else:
        total_minutes = sum(
            end - start for start, end in intervals
        )

        for start, end in intervals:
            duration = end - start

            lines.append(
                f"🕒 {format_time(start)} - "
                f"{format_time(end)} "
                f"(~{format_approx_duration(duration)})"
            )

        lines.append(
            f"🔴 Загалом "
            f"{format_total_duration(total_minutes)} "
            f"без світла"
        )

    if uncertain:
        lines.append(
            "⚠️ Також є інтервали з можливими відключеннями."
        )

    return lines


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
                    f"📍 {CITY}, {STREET}, {HOUSE}\n"
                    f"ДТЕК повернув: {group}"
                )
                return

            today_schedule = await client.get_today_schedule(
                city=CITY,
                street=STREET,
                house_number=HOUSE,
            )

            tomorrow_schedule = await client.get_tomorrow_schedule(
                CITY,
                STREET,
                HOUSE,
            )

    today_data = schedule_to_dict(today_schedule)
    tomorrow_data = schedule_to_dict(tomorrow_schedule)

    if today_data is None and tomorrow_data is None:
        print("Графіки на сьогодні та завтра недоступні.")
        return

    today_date = datetime.now(KYIV_TZ).date()
    date_key = today_date.isoformat()

    current = {
        "date": date_key,
        "group": group,
        "today": today_data,
        "tomorrow": tomorrow_data,
    }

    previous = None

    if STATE_FILE.exists():
        try:
            previous = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            previous = None

    if previous == current:
        print("Графіки не змінилися.")
        return

    group_label = "1.2"

    lines = [
        f"*Підгрупа {group_label}*",
        "",
    ]

    lines.extend(
        format_schedule("Сьогодні", today_data)
    )

    lines.append("")

    lines.extend(
        format_schedule("Завтра", tomorrow_data)
    )

    message = "\n".join(lines)

    send_message(message)

    STATE_FILE.write_text(
        json.dumps(
            current,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Графіки надіслано в Telegram.")


if __name__ == "__main__":
    try:
        asyncio.run(check_schedule())
    except Exception as error:
        print(
            f"Помилка: {type(error).__name__}: {error}"
        )
        raise
