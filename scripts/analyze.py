"""
Ежедневный анализ данных (правило-based, без внешних API) + Telegram-уведомление.
Запускается в 9:00 МСК через GitHub Actions.
Сохраняет: reports/YYYY-MM-DD.json и reports/latest_report.json
"""

import json
import os
import glob
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

OVERLOAD_THRESHOLD = 5   # задач "в работе" — считаем перегрузкой


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
    return f"{r.get('Проект', '')}|||{get_task(r)}|||{r.get('Сотрудник', '')}"


def detect_changes(snapshots):
    if len(snapshots) < 2:
        return []

    first = {row_key(r): r for r in snapshots[0].get("rows", [])}
    last  = {row_key(r): r for r in snapshots[-1].get("rows", [])}

    changes = []

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
# Правило-based анализ
# ──────────────────────────────────────────────────────────────

def analyze_rules(latest, changes):
    stats     = latest.get("stats", {})
    risks     = latest.get("risks", [])
    team      = latest.get("team", [])
    by_status = stats.get("by_status", {})
    by_emp    = stats.get("by_employee", {})

    overdue = [r for r in risks if r.get("type") == "overdue"]
    urgent  = [r for r in risks if r.get("type") == "urgent"]
    total   = latest.get("total_tasks", 0)
    done    = by_status.get("завершено", 0)
    wip     = by_status.get("в работе", 0)

    # ── Перегруженные сотрудники ──────────────────────────────
    overloaded = []
    for name, emp_stats in sorted(by_emp.items(), key=lambda x: -x[1].get("в работе", 0)):
        wip_count = emp_stats.get("в работе", 0)
        if wip_count >= OVERLOAD_THRESHOLD:
            overloaded.append(f"{name} — {wip_count} задач в работе")

    # ── Риски для отчёта ─────────────────────────────────────
    risk_items = []
    for r in overdue[:10]:
        risk_items.append({
            "severity": "high",
            "description": f"Просрочено: {r.get('project', '')} / {r.get('employee', '')} — {r.get('task', '')} (дедлайн {r.get('deadline', '')}, просрочено на {r.get('days', 0)} дн.)",
            "recommendation": "Уточнить статус у ответственного, обновить срок или закрыть задачу.",
        })
    for r in urgent[:10]:
        risk_items.append({
            "severity": "medium",
            "description": f"Срочно ({r.get('days', 0)} дн.): {r.get('project', '')} / {r.get('employee', '')} — {r.get('task', '')} (дедлайн {r.get('deadline', '')})",
            "recommendation": "Проверить готовность, при необходимости перераспределить нагрузку.",
        })
    for name in overloaded:
        risk_items.append({
            "severity": "medium",
            "description": f"Перегрузка: {name}",
            "recommendation": "Рассмотреть перераспределение задач внутри команды.",
        })

    # ── Highlights ────────────────────────────────────────────
    highlights = []
    completed_today = [c for c in changes if c.get("type") == "status_change" and c.get("to") == "завершено"]
    if completed_today:
        highlights.append(f"За 24ч завершено задач: {len(completed_today)}")
    new_tasks = [c for c in changes if c.get("type") == "new_task"]
    if new_tasks:
        highlights.append(f"Добавлено новых задач: {len(new_tasks)}")
    deadline_shifts = [c for c in changes if c.get("type") == "deadline_change"]
    if deadline_shifts:
        highlights.append(f"Перенесено дедлайнов: {len(deadline_shifts)}")
    if done > 0 and total > 0:
        pct = round(done / total * 100)
        highlights.append(f"Общий прогресс: {done} из {total} задач завершено ({pct}%)")
    if not highlights:
        highlights.append("Существенных изменений за 24 часа не зафиксировано.")

    # ── Bottlenecks ───────────────────────────────────────────
    bottlenecks = []
    if overloaded:
        bottlenecks.append(f"Перегружены: {', '.join(n.split(' —')[0] for n in overloaded[:3])}")
    if len(overdue) > 3:
        bottlenecks.append(f"{len(overdue)} просроченных задач требуют закрытия или переноса")
    vacationers = [e["Сотрудник"] for e in team if e.get("Статус", "").lower() in ("отпуск", "больничный")]
    if vacationers:
        bottlenecks.append(f"Не в офисе: {', '.join(vacationers)}")
    if not bottlenecks:
        bottlenecks.append("Критических узких мест не обнаружено.")

    # ── Week focus ────────────────────────────────────────────
    if overdue:
        week_focus = f"Закрыть {len(overdue)} просроченных задач и не допустить перехода {len(urgent)} срочных в просрочку."
    elif urgent:
        week_focus = f"Обеспечить выполнение {len(urgent)} задач с дедлайном до конца недели."
    else:
        week_focus = "Поддерживать текущий темп, контролировать нагрузку команды."

    # ── Meeting agenda ────────────────────────────────────────
    agenda = []
    if overdue:
        projects_overdue = list({r.get("project", "") for r in overdue if r.get("project")})[:3]
        agenda.append(f"Просроченные задачи ({len(overdue)} шт.) — {', '.join(projects_overdue)}")
    if urgent:
        projects_urgent = list({r.get("project", "") for r in urgent if r.get("project")})[:3]
        agenda.append(f"Срочные задачи до конца недели ({len(urgent)} шт.) — {', '.join(projects_urgent)}")
    if overloaded:
        agenda.append(f"Перераспределение нагрузки: {', '.join(n.split(' —')[0] for n in overloaded[:2])}")
    if completed_today:
        agenda.append(f"Итоги: {len(completed_today)} задач закрыто за 24ч")
    if not agenda:
        agenda.append("Текущий статус проектов — плановое совещание")
    agenda = agenda[:5]

    # ── Summary ───────────────────────────────────────────────
    now_str = datetime.now(MSK).strftime("%d.%m.%Y")
    parts = [f"На {now_str}: {total} задач в реестре, {wip} в работе, {done} завершено."]
    if overdue:
        parts.append(f"Просрочено: {len(overdue)} задач.")
    if urgent:
        parts.append(f"Срочных (до 7 дней): {len(urgent)}.")
    if overloaded:
        parts.append(f"Перегружены: {', '.join(n.split(' —')[0] for n in overloaded[:3])}.")
    if len(changes) > 0:
        parts.append(f"За 24ч: {len(changes)} изменений.")
    summary = " ".join(parts)

    return {
        "summary":              summary,
        "risks":                risk_items,
        "highlights":           highlights,
        "bottlenecks":          bottlenecks,
        "week_focus":           week_focus,
        "overloaded_employees": overloaded,
        "meeting_agenda":       agenda,
    }


# ──────────────────────────────────────────────────────────────
# Telegram уведомление
# ──────────────────────────────────────────────────────────────

def send_telegram(token, chat_id, report):
    analysis = report.get("analysis", {})
    risks    = report.get("risks", [])
    overdue  = [r for r in risks if r.get("type") == "overdue"]
    urgent   = [r for r in risks if r.get("type") == "urgent"]

    lines = [
        f"📊 *Утренний отчёт {report['date']}*",
        "",
        analysis.get("summary", ""),
        "",
        f"📌 Задач в реестре: *{report.get('total_tasks', '—')}*",
        f"🔴 Просрочено: *{len(overdue)}* | 🟡 Срочно: *{len(urgent)}*",
    ]

    if analysis.get("overloaded_employees"):
        lines.append("")
        lines.append("⚠️ *Перегрузка:*")
        for emp in analysis["overloaded_employees"][:4]:
            lines.append(f"  • {emp}")

    if analysis.get("meeting_agenda"):
        lines.append("")
        lines.append("📋 *Повестка планёрки:*")
        for i, item in enumerate(analysis["meeting_agenda"][:4], 1):
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
    print(f"[{now_msk.isoformat()}] Ежедневный анализ (rule-based)...")

    latest = load_latest()
    if not latest:
        print("  Нет data/latest.json — пропускаем.")
        return

    snapshots = load_snapshots_for_today()
    print(f"  Снапшотов за 24ч: {len(snapshots)}")

    changes = detect_changes(snapshots)
    print(f"  Изменений: {len(changes)}")

    analysis = analyze_rules(latest, changes)
    print(f"  Анализ готов: {analysis['summary'][:80]}...")

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

    tg_token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id:
        send_telegram(tg_token, tg_chat_id, report)
    else:
        print("  Telegram: токены не заданы — пропускаем")


if __name__ == "__main__":
    main()
