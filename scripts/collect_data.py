"""
Сбор данных из вкладки «Реестр» Google Sheets.
Запускается каждые 30 минут через GitHub Actions.
Сохраняет:
  - data/latest.json  — актуальный снапшот
  - data/history/YYYY-MM-DD_HH-MM.json — архив
"""

import csv
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

SHEET_ID = "19-j0TljNKAIOOyLu1CffXkUPOG--z7vmuiQlvtbwjsE"
SHEET_NAME = "Реестр"
MSK = timezone(timedelta(hours=3))


def fetch_sheet_csv():
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.request.quote(SHEET_NAME)}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_csv(raw_csv):
    rows = []
    reader = csv.DictReader(raw_csv.splitlines())
    for row in reader:
        # Нормализуем: убираем пустые ключи
        clean = {k.strip(): v.strip() for k, v in row.items() if k and k.strip()}
        if any(clean.values()):
            rows.append(clean)
    return rows


def compute_stats(rows):
    statuses = {}
    projects = {}
    employees = {}

    for row in rows:
        status = row.get("Статус", "").lower()
        project = row.get("Проект", "")
        employee = row.get("Сотрудник", "")

        statuses[status] = statuses.get(status, 0) + 1

        if project:
            projects[project] = projects.get(project, 0) + 1

        if employee:
            if employee not in employees:
                employees[employee] = {"total": 0, "завершено": 0, "в работе": 0, "ожидание": 0}
            employees[employee]["total"] += 1
            if status in employees[employee]:
                employees[employee][status] += 1

    return {
        "by_status": statuses,
        "by_project": projects,
        "by_employee": employees,
    }


def find_risks(rows):
    now = datetime.now(MSK)
    risks = []

    for row in rows:
        deadline_str = row.get("Дедлайн", "").strip()
        status = row.get("Статус", "").lower()
        project = row.get("Проект", "")
        employee = row.get("Сотрудник", "")
        task = row.get("Задача", "") or row.get("Задача/Роль", "")

        if status == "завершено":
            continue

        # Попытка разобрать дедлайн (форматы: DD.MM.YYYY или YYYY-MM-DD)
        deadline = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                deadline = datetime.strptime(deadline_str, fmt).replace(tzinfo=MSK)
                break
            except ValueError:
                continue

        if deadline:
            days_left = (deadline - now).days
            if days_left < 0:
                risks.append({
                    "type": "overdue",
                    "days": abs(days_left),
                    "project": project,
                    "employee": employee,
                    "task": task,
                    "deadline": deadline_str,
                })
            elif days_left <= 7:
                risks.append({
                    "type": "urgent",
                    "days": days_left,
                    "project": project,
                    "employee": employee,
                    "task": task,
                    "deadline": deadline_str,
                })

        if status == "ожидание":
            risks.append({
                "type": "blocked",
                "days": None,
                "project": project,
                "employee": employee,
                "task": task,
                "deadline": deadline_str,
            })

    return risks


def main():
    now_msk = datetime.now(MSK)
    timestamp = now_msk.strftime("%Y-%m-%dT%H:%M:%S+03:00")
    date_str = now_msk.strftime("%Y-%m-%d")
    time_str = now_msk.strftime("%H-%M")

    print(f"[{timestamp}] Загрузка данных из Google Sheets...")
    raw = fetch_sheet_csv()
    rows = parse_csv(raw)
    print(f"  Получено строк: {len(rows)}")

    stats = compute_stats(rows)
    risks = find_risks(rows)

    snapshot = {
        "timestamp": timestamp,
        "date": date_str,
        "time": time_str,
        "total_tasks": len(rows),
        "stats": stats,
        "risks": risks,
        "rows": rows,
    }

    os.makedirs("data/history", exist_ok=True)

    # Сохраняем архивный снапшот
    history_path = f"data/history/{date_str}_{time_str}.json"
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    # Обновляем latest.json
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  Риски: {len(risks)} (просрочено/срочно/заблокировано)")
    print(f"  Сохранено: data/latest.json и {history_path}")


if __name__ == "__main__":
    main()
