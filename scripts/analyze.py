"""
Ежедневный анализ данных через Claude API + Telegram-уведомление.
Запускается в 9:00 МСК через GitHub Actions.
Сохраняет: reports/YYYY-MM-DD.json и reports/latest_report.json
"""

import json
import os
import glob
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
import anthropic

MSK = timezone(timedelta(hours=3))


# ──────────────────────────────────────────────────────────────
# Загрузка данных
# ──────────────────────────────────────────────────────────────

def load_snapshots_for_today():
    now = datetime.now(MSK)
    dates = [
        now.strftime("%Y-%m-%d"),
        (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    files = []
    for d in dates:
        files += glob.glob(f"data/history/{d}_*.json")
    files.sort()
    snapshots = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                snapshots.append(json.load(fp))
        except Exception:
            pass
    return snapshots


def load_latest():
    try:
        with open("data/latest.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# ──────────────────────────────────────────────────────────────
# Обнаружение изменений
# ──────────────────────────────────────────────────────────────

def get_task(row):
    return row.get("Задача") or row.get("Задача/Роль", "")


def row_key(r):
    """Надёжный ключ с разделителем — исключает коллизии."""
    return f"{r.get('Проект', '')}|||{get_task(r)}|||{r.get('Сотрудник', '')}"


def detect_changes(snapshots):
    if len(snapshots) < 2:
        return []

    first = {row_key(r): r for r in snapshots[0].get("rows", [])}
    last  = {row_key(r): r for r in snapshots[-1].get("rows", [])}

    changes = []

    # Новые и изменившиеся задачи
    for key, row in last.items():
        if key in first:
            old_s = first[key].get("Статус", "")
            new_s = row.get("Статус", "")
            if old_s != new_s:
                changes.append({
                    "type": "status_change",
                    "project":  row.get("Проект", ""),
                    "employee": row.get("Сотрудник", ""),
                    "task":     get_task(row),
                    "from":     old_s,
                    "to":       new_s,
                })
            # Отслеживаем перенос дедлайна — ранний red flag
            old_d = first[key].get("Дедлайн", "")
            new_d = row.get("Дедлайн", "")
            if old_d and new_d and old_d != new_d:
                changes.append({
                    "type": "deadline_change",
                    "project":  row.get("Проект", ""),
                    "employee": row.get("Сотрудник", ""),
                    "task":     get_task(row),
                    "from":     old_d,
                    "to":       new_d,
                })
        else:
            changes.append({
                "type": "new_task",
                "project":  row.get("Проект", ""),
                "employee": row.get("Сотрудник", ""),
                "task":     get_task(row),
                "status":   row.get("Статус", ""),
            })

    # Удалённые задачи
    for key in first:
        if key not in last:
            r = first[key]
            changes.append({
                "type": "removed_task",
                "project":  r.get("Проект", ""),
                "employee": r.get("Сотрудник", ""),
                "task":     get_task(r),
            })

    return changes


# ──────────────────────────────────────────────────────────────
# Claude API анализ
# ──────────────────────────────────────────────────────────────

def build_context(latest, changes):
    stats   = latest.get("stats", {})
    risks   = latest.get("risks", [])
    team    = latest.get("team", [])
    total_r = len(risks)

    # Выводим полное количество рисков чтобы Claude не думал что их 30
    risk_note = f"(показано {min(30, total_r)} из {total_r})" if total_r > 30 else f"({total_r} шт.)"

    return f"""
Дата: {datetime.now(MSK).strftime('%d.%m.%Y, %A')}
Задач в реестре: {latest.get('total_tasks', 0)}
Сотрудников: {latest.get('total_employees', 0)}
Проектов: {latest.get('total_projects', 0)}

СТАТУСЫ:
{json.dumps(stats.get('by_status', {}), ensure_ascii=False)}

ЗАГРУЗКА СОТРУДНИКОВ:
{json.dumps(stats.get('by_employee', {}), ensure_ascii=False, indent=2)}

ЗАГРУЗКА ПО ПРОЕКТАМ:
{json.dumps(stats.get('by_project', {}), ensure_ascii=False)}

РИСКИ {risk_note}:
{json.dumps(risks[:30], ensure_ascii=False, indent=2)}

ИЗМЕНЕНИЯ ЗА 24 ЧАСА ({len(changes)} шт.):
{json.dumps(changes[:25], ensure_ascii=False, indent=2)}

КОМАНДА:
{json.dumps(team, ensure_ascii=False, indent=2)}
""".strip()


def analyze_with_claude(context):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=(
            "Ты — аналитик инженерного проектного бюро. "
            "Отвечай строго валидным JSON без markdown-обёртки и лишнего текста. "
            "Опирайся только на предоставленные данные."
        ),
        messages=[{
            "role": "user",
            "content": f"""Проанализируй данные реестра и подготовь отчёт к утренней планёрке.

ДАННЫЕ:
{context}

Верни JSON:
{{
  "summary": "3-4 предложения: ключевые итоги, главные риски, тренд",
  "risks": [{{"severity":"high|medium","description":"...","recommendation":"..."}}],
  "highlights": ["..."],
  "bottlenecks": ["..."],
  "week_focus": "На что сфокусироваться на этой неделе",
  "overloaded_employees": ["имя — X задач"],
  "meeting_agenda": ["пункт 1","пункт 2","пункт 3"]
}}"""
        }],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [WARN] Ошибка парсинга JSON от Claude: {e}")
        return {
            "summary": "Анализ недоступен — ошибка парсинга ответа.",
            "risks": [], "highlights": [], "bottlenecks": [],
            "week_focus": raw[:300],
            "overloaded_employees": [],
            "meeting_agenda": [],
        }


# ──────────────────────────────────────────────────────────────
# Telegram уведомление
# ──────────────────────────────────────────────────────────────

def send_telegram(token, chat_id, report):
    analysis = report.get("analysis", {})
    risks    = report.get("risks", [])
    overdue  = [r for r in risks if r.get("type") == "overdue"]
    urgent   = [r for r in risks if r.get("type") == "urgent"]
    blocked  = [r for r in risks if r.get("type") == "blocked"]

    lines = [
        f"📊 *Утренний отчёт {report['date']}*",
        "",
        analysis.get("summary", ""),
        "",
        f"📌 Задач в реестре: *{report.get('total_tasks', '—')}*",
        f"🔴 Просрочено: *{len(overdue)}* | 🟡 Срочно: *{len(urgent)}* | ⏸ Заблокировано: *{len(blocked)}*",
    ]

    if analysis.get("overloaded_employees"):
        lines.append("")
        lines.append("⚠️ *Перегрузка:*")
        for emp in analysis["overloaded_employees"][:4]:
            lines.append(f"  • {emp}")

    if analysis.get("meeting_agenda"):
        lines.append("")
        lines.append("📋 *Повестка планёрки:*")
        for i, item in enumerate(analysis["meeting_agenda"][:3], 1):
            lines.append(f"  {i}. {item}")

    lines.append("")
    lines.append("🔗 [Открыть дашборд](https://vladalexgor.github.io/project-management-dashboard/)")

    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("  Telegram: сообщение отправлено ✓")
            else:
                print(f"  Telegram: ошибка — {result}")
    except Exception as e:
        print(f"  Telegram: не удалось отправить — {e}")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    now_msk  = datetime.now(MSK)
    date_str = now_msk.strftime("%Y-%m-%d")
    print(f"[{now_msk.isoformat()}] Ежедневный анализ...")

    latest = load_latest()
    if not latest:
        print("  Нет data/latest.json — пропускаем.")
        return

    snapshots = load_snapshots_for_today()
    print(f"  Снапшотов за 24ч: {len(snapshots)}")

    changes = detect_changes(snapshots)
    print(f"  Изменений: {len(changes)}")

    context  = build_context(latest, changes)
    print("  Отправка в Claude...")
    analysis = analyze_with_claude(context)
    print(f"  Готово: {analysis.get('summary', '')[:80]}...")

    report = {
        "date":            date_str,
        "generated_at":    now_msk.isoformat(),
        "snapshots_count": len(snapshots),
        "changes":         changes,
        "analysis":        analysis,
        "stats":           latest.get("stats", {}),
        "risks":           latest.get("risks", []),
        "total_tasks":     latest.get("total_tasks", 0),
        "total_employees": latest.get("total_employees", 0),
        "total_projects":  latest.get("total_projects", 0),
        "team":            latest.get("team", []),
        "projects":        latest.get("projects", []),
    }

    os.makedirs("reports", exist_ok=True)
    for path in (f"reports/{date_str}.json", "reports/latest_report.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Сохранено: reports/{date_str}.json")

    # Telegram
    tg_token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id:
        send_telegram(tg_token, tg_chat_id, report)
    else:
        print("  Telegram: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — пропускаем")


if __name__ == "__main__":
    main()
