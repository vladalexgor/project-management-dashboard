"""
Сбор данных из Google Sheets (вкладка Дашборд — матрица сотрудник × проект).
Парсит матрицу в отдельные задачи: извлекает проект, сотрудника, задачу, дедлайн, статус.

Запускается каждые 30 минут через GitHub Actions.
"""

import csv
import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

SHEET_ID = "19-j0TljNKAIOOyLu1CffXkUPOG--z7vmuiQlvtbwjsE"
MSK = timezone(timedelta(hours=3))

# Колонки, которые не являются проектами
NON_PROJECT_COLS = {"Сотрудник", "Статус"}

# Эмодзи → статус задачи
EMOJI_STATUS = {
    "🔴": "просрочено",
    "🟡": "в работе",
    "🟢": "завершено",
    "⚪️": "не начато",
    "⚪":  "не начато",
    "🟠": "в работе",
    "🔵": "в работе",
}

# Статус сотрудника → нормализованный
EMP_STATUS_MAP = {
    "в норме":  "в норме",
    "перегруз": "перегруз",
    "отпуск":   "отпуск",
    "больничный": "больничный",
}


def fetch_csv(gid=None):
    """Скачиваем CSV. gid=None → первая вкладка (Дашборд)."""
    if gid is not None:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8-sig")
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Не удалось загрузить CSV (gid={gid}): {e}")
            time.sleep(2 ** attempt)


def parse_matrix_csv(raw_csv):
    """Парсим матрицу Дашборда в список строк-словарей."""
    rows = []
    reader = csv.DictReader(raw_csv.splitlines())
    for row in reader:
        clean = {k.strip(): v.strip() for k, v in row.items() if k and k.strip()}
        if clean.get("Сотрудник"):
            rows.append(clean)
    return rows


def extract_deadline(text):
    """Извлекаем дату дедлайна из текста вида '...задача (DD.MM.YYYY)'."""
    m = re.search(r'\((\d{2}\.\d{2}\.\d{4})\)\s*$', text.strip())
    return m.group(1) if m else ""


def extract_emoji_status(text):
    """Извлекаем статус из первого эмодзи в тексте."""
    for emoji, status in EMOJI_STATUS.items():
        if emoji in text:
            return status
    return "не начато"


def clean_task_text(text):
    """Убираем номер, эмодзи и дедлайн в скобках из текста задачи."""
    # Убираем "1. " в начале
    text = re.sub(r'^\d+\.\s*', '', text.strip())
    # Убираем эмодзи
    for emoji in EMOJI_STATUS:
        text = text.replace(emoji, '')
    # Убираем дедлайн в конце
    text = re.sub(r'\s*\(\d{2}\.\d{2}\.\d{4}\)\s*$', '', text)
    return text.strip()


def parse_cell_tasks(cell_value, project, employee):
    """
    Парсим ячейку матрицы в список задач.
    Формат ячейки: '1. 🔴 Задача (25.03.2026)2. 🟡 Другая задача (01.07.2026)'
    """
    tasks = []
    if not cell_value.strip():
        return tasks

    # Разбиваем по паттерну "N. " (начало новой задачи)
    parts = re.split(r'(?=\d+\.\s)', cell_value.strip())
    for part in parts:
        part = part.strip()
        if not part:
            continue
        deadline   = extract_deadline(part)
        status     = extract_emoji_status(part)
        task_text  = clean_task_text(part)
        if task_text:
            tasks.append({
                "Проект":   project,
                "Сотрудник": employee,
                "Задача":   task_text,
                "Дедлайн":  deadline,
                "Статус":   status,
            })
    return tasks


def matrix_to_tasks(matrix_rows):
    """Разворачиваем матрицу в плоский список задач."""
    all_tasks = []
    for row in matrix_rows:
        employee = row.get("Сотрудник", "")
        for col, value in row.items():
            if col in NON_PROJECT_COLS or not value.strip():
                continue
            tasks = parse_cell_tasks(value, project=col, employee=employee)
            all_tasks.extend(tasks)
    return all_tasks


def extract_team(matrix_rows):
    """Извлекаем список сотрудников из матрицы."""
    team = []
    for row in matrix_rows:
        name   = row.get("Сотрудник", "")
        status = row.get("Статус", "")
        if name:
            team.append({"Сотрудник": name, "Статус": status})
    return team


def extract_projects(matrix_rows):
    """Извлекаем список проектов из заголовков колонок."""
    if not matrix_rows:
        return []
    projects = []
    for col in matrix_rows[0].keys():
        if col not in NON_PROJECT_COLS:
            # Пытаемся извлечь дедлайн из названия колонки
            m = re.search(r'\((\d{2}\.\d{2}\.\d{4})\)', col)
            deadline = m.group(1) if m else ""
            name = re.sub(r'\s*\(.*?\)\s*$', '', col).strip()
            projects.append({
                "Название объекта": name,
                "Дедлайн проекта": deadline,
                "Полное название": col,
            })
    return projects


def normalize_task_status(status):
    """Нормализуем статус для статистики."""
    mapping = {
        "завершено":  "завершено",
        "в работе":   "в работе",
        "не начато":  "ожидание",
        "просрочено": "просрочено",
    }
    return mapping.get(status, status)


def compute_stats(task_rows):
    statuses, projects, employees = {}, {}, {}
    known = {"завершено", "в работе", "ожидание", "просрочено"}

    for row in task_rows:
        status   = normalize_task_status(row.get("Статус", ""))
        project  = row.get("Проект", "")
        employee = row.get("Сотрудник", "")

        statuses[status] = statuses.get(status, 0) + 1

        if project:
            projects[project] = projects.get(project, 0) + 1

        if employee:
            if employee not in employees:
                employees[employee] = {
                    "total": 0, "завершено": 0,
                    "в работе": 0, "ожидание": 0,
                    "просрочено": 0, "прочее": 0,
                }
            employees[employee]["total"] += 1
            key = status if status in known else "прочее"
            employees[employee][key] += 1

    return {"by_status": statuses, "by_project": projects, "by_employee": employees}


def find_risks(task_rows):
    now = datetime.now(MSK)
    risks = []
    seen = set()  # дедупликация

    for row in task_rows:
        deadline_str = row.get("Дедлайн", "").strip()
        status       = row.get("Статус", "")
        project      = row.get("Проект", "")
        employee     = row.get("Сотрудник", "")
        task         = row.get("Задача", "")

        if status == "завершено":
            continue

        key = f"{project}|{employee}|{task}"
        if key in seen:
            continue

        entry = dict(project=project, employee=employee, task=task, deadline=deadline_str)

        # Просрочено по эмодзи (даже без разбора даты)
        if status == "просрочено":
            risks.append({**entry, "type": "overdue", "days": None})
            seen.add(key)
            continue

        # Разбираем дату дедлайна
        deadline = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
            try:
                deadline = datetime.strptime(deadline_str, fmt)
                break
            except ValueError:
                continue

        if deadline:
            days_left = (deadline.date() - now.date()).days
            if days_left < 0:
                risks.append({**entry, "type": "overdue", "days": abs(days_left)})
                seen.add(key)
            elif days_left <= 7:
                risks.append({**entry, "type": "urgent", "days": days_left})
                seen.add(key)

    return risks


def main():
    now_msk   = datetime.now(MSK)
    timestamp = now_msk.strftime("%Y-%m-%dT%H:%M:%S+03:00")
    date_str  = now_msk.strftime("%Y-%m-%d")
    time_str  = now_msk.strftime("%H-%M")

    print(f"[{timestamp}] Загрузка данных (матрица Дашборд)...")

    raw    = fetch_csv(gid=None)          # первая вкладка = Дашборд
    matrix = parse_matrix_csv(raw)
    print(f"  Строк в матрице: {len(matrix)} (сотрудников)")

    tasks    = matrix_to_tasks(matrix)
    team     = extract_team(matrix)
    projects = extract_projects(matrix)

    print(f"  Извлечено задач: {len(tasks)}")

    stats = compute_stats(tasks)
    risks = find_risks(tasks)

    snapshot = {
        "timestamp":       timestamp,
        "date":            date_str,
        "time":            time_str,
        "collection_status": "ok",
        "using_api":       False,
        "total_tasks":     len(tasks),
        "total_employees": len(team),
        "total_projects":  len(projects),
        "stats":           stats,
        "risks":           risks,
        "rows":            tasks,
        "team":            team,
        "projects":        projects,
    }

    os.makedirs("data/history", exist_ok=True)
    hist = f"data/history/{date_str}_{time_str}.json"
    for path in (hist, "data/latest.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  Задач: {len(tasks)} | Сотрудников: {len(team)} | Объектов: {len(projects)}")
    print(f"  Рисков: {len(risks)} | Сохранено: {hist}")


if __name__ == "__main__":
    main()
