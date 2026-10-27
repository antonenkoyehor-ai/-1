import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies


# =========================
# НАЛАШТУВАННЯ
# =========================

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://www.dtek-krem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

CITY = "с-ще Глеваха"
STREET = "вул. Вокзальна"
HOUSE = "53"

GROUP = "1.2"
ALERT_MINUTES = 10

STATE_FILE = Path("state.json")
ALERTS_FILE = Path("alerts.json")

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Максимальний час очікування, секунд
COOKIES_TIMEOUT = 60
ADDRESS_TIMEOUT = 35
SCHEDULE_TIMEOUT = 35


# =========================
# ВИМІРЮВАННЯ ЧАСУ
# =========================

async def timed_step(name, awaitable, timeout):
    started = time.perf_counter()
    print(f"[ПОЧАТОК] {name}")

    try:
        result = await asyncio.wait_for(
            awaitable,
            timeout=timeout,
        )

        elapsed = time.perf_counter() - started
        print(f"[ГОТОВО] {name}: {elapsed:.2f} сек.")

        return result

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - started
        print(
            f"[ТАЙМ-АУТ] {name}: "
            f"понад {elapsed:.2f} сек."
        )
        raise

    except Exception as error:
        elapsed = time.perf_counter() - started
        print(
            f"[ПОМИЛКА] {name}: {elapsed:.2f} сек.; "
            f"{type(error).__name__}: {error}"
        )
        raise


# =========================
# TELEGRAM
# =========================

def send_message(message):
    started = time.perf_counter()
    print("[ПОЧАТОК] Надсилання повідомлення Telegram")

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        },
        timeout=(5, 15),
    )

    response.raise_for_status()

    elapsed = time.perf_counter() - started
    print(
        f"[ГОТОВО] Telegram: {elapsed:.2f} сек."
    )


# =========================
# СТАТУСИ ГРАФІКА
# =========================

def status_value(status):
    return str(getattr(status, "value", status)).upper()


def schedule_to_dict(schedule):
    if not schedule:
        return None

    return {
        str(slot): status_value(status)
        for slot, status in schedule.items()
    }


# =========================
# ПОШУК ІНТЕРВАЛІВ
# =========================

def get_outage_intervals(schedule):
    intervals = []
    uncertain = False

    if not schedule:
        return intervals, uncertain

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


# =========================
# ФОРМАТУВАННЯ ЧАСУ
# =========================

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


# =========================
# ФОРМАТУВАННЯ ГРАФІКА
# =========================

def format_schedule(date_label, schedule):
    lines = [f"*📅 {date_label}*"]

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


# =========================
# ЗБЕРЕЖЕННЯ СПОВІЩЕНЬ
# =========================

def load_alerts():
    try:
        data = json.loads(
            ALERTS_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return data

    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return []


def save_alerts(alerts):
    ALERTS_FILE.write_text(
        json.dumps(
            alerts,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# =========================
# ПОПЕРЕДЖЕННЯ ПРО ВИМКНЕННЯ
# =========================

def notify_before_outage(today_schedule, tomorrow_schedule):
    now = datetime.now(KYIV_TZ)

    alerts = load_alerts()
    alerts_set = set(alerts)

    schedules = [
        (now.date(), today_schedule),
        (now.date() + timedelta(days=1), tomorrow_schedule),
    ]

    for schedule_date, schedule in schedules:
        if not schedule:
            continue

        intervals, _ = get_outage_intervals(schedule)

        for start, end in intervals:
            outage_datetime = datetime(
                schedule_date.year,
                schedule_date.month,
                schedule_date.day,
                start // 60,
                start % 60,
                tzinfo=KYIV_TZ,
            )

            minutes_left = (
                outage_datetime - now
            ).total_seconds() / 60

            if not (0 < minutes_left <= ALERT_MINUTES):
                continue

            alert_key = (
                f"{schedule_date.isoformat()}|{GROUP}|{start}"
            )

            if alert_key in alerts_set:
                continue

            message = (
                f"🔔 *Увага! Підгрупа {GROUP}*\n\n"
                "До планового відключення залишилося "
                "орієнтовно 10 хвилин.\n\n"
                f"🕒 Початок: *{format_time(start)}*"
            )

            send_message(message)

            alerts.append(alert_key)
            alerts_set.add(alert_key)
            save_alerts(alerts)

            print(
                f"Попередження надіслано: "
                f"{schedule_date} {format_time(start)}"
            )

    keep_from = now.date() - timedelta(days=2)
    filtered_alerts = []

    for alert in alerts:
        try:
            date_text = alert.split("|", 1)[0]
            alert_date = datetime.strptime(
                date_text, "%Y-%m-%d"
            ).date()

            if alert_date >= keep_from:
                filtered_alerts.append(alert)

        except (ValueError, IndexError):
            continue

    if filtered_alerts != alerts:
        save_alerts(filtered_alerts)


# =========================
# ОТРИМАННЯ ГРАФІКІВ ДТЕК
# =========================

async def check_schedule():
    total_started = time.perf_counter()

    print("=" * 45)
    print("Початок перевірки графіка ДТЕК")
    print(
        "Час Києва:",
        datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M:%S"),
    )
    print("=" * 45)

    try:
        cookies, csrf_token = await timed_step(
            "Отримання cookies і CSRF",
            get_cleared_cookies(SCHEDULE_URL),
            COOKIES_TIMEOUT,
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "application/json, text/javascript, */*; q=0.01"
            ),
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

                address = await timed_step(
                    "Пошук адреси та визначення підгрупи",
                    client.get_group_by_address(
                        city=CITY,
                        street=STREET,
                        house_number=HOUSE,
                    ),
                    ADDRESS_TIMEOUT,
                )

                group = str(
                    address.group_display_name
                    or address.group_id
                )

                print(f"Підгрупа, повернута ДТЕК: {group}")

                if GROUP not in group:
                    send_message(
                        "⚠️ Не вдалося підтвердити підгрупу 1.2.\n"
                        f"ДТЕК повернув: {group}"
                    )
                    return

                today_schedule = await timed_step(
                    "Отримання графіка на сьогодні",
                    client.get_today_schedule(
                        city=CITY,
                        street=STREET,
                        house_number=HOUSE,
                    ),
                    SCHEDULE_TIMEOUT,
                )

                tomorrow_schedule = await timed_step(
                    "Отримання графіка на завтра",
                    client.get_tomorrow_schedule(
                        CITY,
                        STREET,
                        HOUSE,
                    ),
                    SCHEDULE_TIMEOUT,
                )

        today_data = schedule_to_dict(today_schedule)
        tomorrow_data = schedule_to_dict(tomorrow_schedule)

        if today_data is None and tomorrow_data is None:
            print("Графіки на сьогодні та завтра недоступні.")
            return

        notify_before_outage(
            today_schedule,
            tomorrow_schedule,
        )

        today_date = datetime.now(KYIV_TZ).date()
        tomorrow_date = today_date + timedelta(days=1)

        current = {
            "date": today_date.isoformat(),
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

        lines = [
            f"*Підгрупа {GROUP}*",
            "",
        ]

        lines.extend(
            format_schedule(
                today_date.strftime("%d.%m.%Y"),
                today_data,
            )
        )

        lines.append("")

        lines.extend(
            format_schedule(
                tomorrow_date.strftime("%d.%m.%Y"),
                tomorrow_data,
            )
        )

        message = "\n".join(lines)

        await timed_step(
            "Надсилання графіка в Telegram",
            asyncio.to_thread(send_message, message),
            25,
        )

        STATE_FILE.write_text(
            json.dumps(
                current,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print("Графіки надіслано в Telegram.")

    finally:
        total_elapsed = time.perf_counter() - total_started
        print("=" * 45)
        print(
            f"Загальний час виконання: "
            f"{total_elapsed:.2f} сек."
        )
        print("=" * 45)


# =========================
# ЗАПУСК
# =========================

if __name__ == "__main__":
    try:
        asyncio.run(check_schedule())

    except Exception as error:
        print(
            f"Помилка: {type(error).__name__}: {error}"
        )
        raise
