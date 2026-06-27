"""
Сбор данных из вкладок Google Sheets: Реестр, Команда, Объекты.
Использует Google Sheets API v4 — читает ВСЕ строки включая скрытые фильтром.
Запускается каждые 30 минут через GitHub Actions.

Требует секрет GOOGLE_CREDENTIALS в GitHub → JSON сервисного аккаунта.
Инструкция: docs/google-setup.md
"""

import csv
import io
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

SHEET_ID = "19-j0TljNKAIOOyLu1CffXkUPOG--z7vmuiQlvtbwjsE"
MSK = timezone(timedelta(hours=3))

TABS = {
    "registry": "Реестр",
    "team":     "Команда",
    "projects": "Объекты",
}


# ──────────────────────────────────────────────────────────────
# Получение данных
# ──────────────────────────────────────────────────────────────

def fetch_via_api(sheet_name, creds_json):
    """
    Читает вкладку через Google Sheets API v4.
    Возвращает все строки, ИГНОРИРУЯ фильтры и скрытые строки.
    """
    import google.oauth2.service_account as sa
    import googleapiclient.discovery as disco

    creds_data = json.loads(creds_json)
    credentials = sa.Credentials.from_service_account_info(
        creds_data,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = disco.build("sheets", "v4", credentials=credentials, cache_discovery=False)

    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SHEET_ID,
            range=sheet_name,                # читаем всю вкладку по имени
            valueRenderOption="FORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )
        .execute()
    )

    values = result.get("values", [])
    if len(values) < 2:
        return []

    headers = [h.strip() for h in values[0]]
    rows = []
    for raw_row in values[1:]:
        # Дополняем строку пустыми значениями до длины заголовка
        padded = raw_row + [""] * (len(headers) - len(raw_row))
        row = {headers[i]: padded[i].strip() for i in range(len(headers))}
        if any(row.values()):
            rows.append(row)
    return rows


def fetch_via_csv(sheet_name):
    """
    Запасной вариант: CSV-экспорт без авторизации.
    ВНИМАНИЕ: уважает фильтры — скрытые строки не включаются.
    """
    encoded = urllib.parse.quote(sheet_name)
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&sheet={encoded}"
    )
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8-sig")
            reader = csv.DictReader(raw.splitlines())
            rows = []
            for row in reader:
                clean = {k.strip(): v.strip() for k, v in row.items() if k and k.strip()}
                if any(clean.values()):
                    rows.append(clean)
            return rows
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"CSV-экспорт «{sheet_name}» недоступен: {e}")
            time.sleep(2 ** attempt)


def load_tab(sheet_name, creds_json=None):
    if creds_json:
        print(f"  [{sheet_name}] → API v4 (все строки, включая скрытые)")
        return fetch_via_api(sheet_name, creds_json)
    else:
        print(f"  [{sheet_name}] → CSV-экспорт (⚠️ фильтры учитываются)")
        return fetch_via_csv(sheet_name)


# ──────────────────────────────────────────────────────────────
# Нормализация и анализ
# ──────────────────────────────────────────────────────────────

STATUS_SYNONYMS = {
    "готово": "завершено", "выполнено": "завершено",
    "done": "завершено", "complete": "завершено", "✅": "завершено",
    "в процессе": "в работе", "in progress": "в работе",
    "blocked": "ожидание", "заблокировано": "ожидание", "ждём": "ожидание",
}
KNOWN_STATUSES = {"завершено", "в работе", "ожидание"}


def normalize_status(s):
    return STATUS_SYNONYMS.get(s.lower().strip(), s.lower().strip())


def get_task(row):
    return row.get("Задача") or row.get("Задача/Роль", "")


def compute_stats(rows):
    statuses, projects, employees = {}, {}, {}
    for row in rows:
        status = normalize_status(row.get("Статус", ""))
        project = row.get("Проект", "")
        employee = row.get("Сотрудник", "")

        statuses[status] = statuses.get(status, 0) + 1
        if project:
            projects[project] = projects.get(project, 0) + 1
        if employee:
            if employee not in employees:
                employees[employee] = {
                    "total": 0, "завершено": 0,
                    "в работе": 0, "ожидание": 0, "прочее": 0,
                }
            employees[employee]["total"] += 1
            key = status if status in KNOWN_STATUSES else "прочее"
            employees[employee][key] += 1

    return {"by_status": statuses, "by_project": projects, "by_employee": employees}


def find_risks(rows):
    now = datetime.now(MSK)
    risks = []
    for row in rows:
        deadline_str = row.get("Дедлайн", "").strip()
        status = normalize_status(row.get("Статус", ""))
        if status == "завершено":
            continue

        project  = row.get("Проект", "")
        employee = row.get("Сотрудник", "")
        task     = get_task(row)

        deadline = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
            try:
                deadline = datetime.strptime(deadline_str, fmt)
                break
            except ValueError:
                continue

        entry = dict(project=project, employee=employee, task=task, deadline=deadline_str)
        if deadline:
            days_left = (deadline.date() - now.date()).days
            if days_left < 0:
                risks.append({**entry, "type": "overdue", "days": abs(days_left)})
            elif days_left <= 7:
                risks.append({**entry, "type": "urgent",  "days": days_left})
            elif status == "ожидание":
                risks.append({**entry, "type": "blocked", "days": None})
        elif status == "ожидание":
            risks.append({**entry, "type": "blocked", "days": None})

    return risks


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    now_msk = datetime.now(MSK)
    timestamp = now_msk.strftime("%Y-%m-%dT%H:%M:%S+03:00")
    date_str  = now_msk.strftime("%Y-%m-%d")
    time_str  = now_msk.strftime("%H-%M")

    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        # Устанавливаем google-api-python-client если есть credentials
        os.system("pip install -q google-api-python-client google-auth")

    print(f"[{timestamp}] Загрузка данных...")
    if not creds_json:
        print("  ⚠️  GOOGLE_CREDENTIALS не задан — используется CSV (скрытые строки не видны)")

    tab_data = {}
    for key, name in TABS.items():
        try:
            tab_data[key] = load_tab(name, creds_json)
            print(f"    → {len(tab_data[key])} строк")
        except Exception as e:
            print(f"    [WARN] {e}")
            tab_data[key] = []

    registry = tab_data["registry"]
    stats  = compute_stats(registry)
    risks  = find_risks(registry)

    snapshot = {
        "timestamp":       timestamp,
        "date":            date_str,
        "time":            time_str,
        "collection_status": "ok",
        "using_api":       bool(creds_json),
        "total_tasks":     len(registry),
        "total_employees": len(tab_data["team"]),
        "total_projects":  len(tab_data["projects"]),
        "stats":           stats,
        "risks":           risks,
        "rows":            registry,
        "team":            tab_data["team"],
        "projects":        tab_data["projects"],
    }

    os.makedirs("data/history", exist_ok=True)
    hist = f"data/history/{date_str}_{time_str}.json"
    for path in (hist, "data/latest.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  Задач: {len(registry)} | Сотрудников: {len(tab_data['team'])} | Объектов: {len(tab_data['projects'])}")
    print(f"  Рисков: {len(risks)} | Сохранено: {hist}")


if __name__ == "__main__":
    main()
