"""
Ежедневный анализ собранных данных через Claude API.
Запускается в 9:00 по Москве через GitHub Actions.
Сохраняет: reports/YYYY-MM-DD.json
"""

import json
import os
import glob
from datetime import datetime, timezone, timedelta
import anthropic

MSK = timezone(timedelta(hours=3))


def load_snapshots_for_today():
    """Загружаем все снапшоты за последние 24 часа."""
    now = datetime.now(MSK)
    date_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    files = (
        glob.glob(f"data/history/{date_str}_*.json")
        + glob.glob(f"data/history/{yesterday_str}_*.json")
    )
    files.sort()

    snapshots = []
    for f in files:
        with open(f, encoding="utf-8") as fp:
            snapshots.append(json.load(fp))

    return snapshots


def load_latest():
    try:
        with open("data/latest.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def detect_changes(snapshots):
    """Находим изменения между первым и последним снапшотом за день."""
    if len(snapshots) < 2:
        return []

    def row_key(r):
        # Разделитель ||| исключает коллизии при конкатенации
        # Fallback на "Задача/Роль" если столбец называется иначе
        task = r.get("Задача") or r.get("Задача/Роль", "")
        return f"{r.get('Проект', '')}|||{task}|||{r.get('Сотрудник', '')}"

    first = {row_key(r): r for r in snapshots[0].get("rows", [])}
    last = {row_key(r): r for r in snapshots[-1].get("rows", [])}

    changes = []
    for key, row in last.items():
        if key in first:
            old_status = first[key].get("Статус", "")
            new_status = row.get("Статус", "")
            if old_status != new_status:
                changes.append({
                    "type": "status_change",
                    "project": row.get("Проект", ""),
                    "employee": row.get("Сотрудник", ""),
                    "task": row.get("Задача", "") or row.get("Задача/Роль", ""),
                    "from": old_status,
                    "to": new_status,
                })
        else:
            changes.append({
                "type": "new_task",
                "project": row.get("Проект", ""),
                "employee": row.get("Сотрудник", ""),
                "task": row.get("Задача", "") or row.get("Задача/Роль", ""),
                "status": row.get("Статус", ""),
            })

    return changes


def build_context(latest, snapshots, changes):
    """Собираем контекст для Claude."""
    stats = latest.get("stats", {})
    risks = latest.get("risks", [])

    context = f"""
Дата анализа: {datetime.now(MSK).strftime('%d.%m.%Y')}
Всего задач в реестре: {latest.get('total_tasks', 0)}

СТАТУСЫ ЗАДАЧ:
{json.dumps(stats.get('by_status', {}), ensure_ascii=False, indent=2)}

ЗАГРУЗКА ПО СОТРУДНИКАМ:
{json.dumps(stats.get('by_employee', {}), ensure_ascii=False, indent=2)}

ЗАГРУЗКА ПО ПРОЕКТАМ:
{json.dumps(stats.get('by_project', {}), ensure_ascii=False, indent=2)}

РИСКИ ({len(risks)} шт.):
{json.dumps(risks[:30], ensure_ascii=False, indent=2)}

ИЗМЕНЕНИЯ ЗА ПОСЛЕДНИЕ 24 ЧАСА ({len(changes)} шт.):
{json.dumps(changes[:20], ensure_ascii=False, indent=2)}
""".strip()

    return context


def analyze_with_claude(context):
    """Отправляем данные в Claude и получаем анализ."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Ты — аналитик проектного бюро. Проанализируй данные реестра задач и подготовь краткий отчёт для руководителя к утренней планёрке.

ДАННЫЕ:
{context}

Подготовь отчёт в формате JSON со следующей структурой:
{{
  "summary": "2-3 предложения: ключевые итоги за день",
  "risks": [
    {{"severity": "high|medium", "description": "...", "recommendation": "..."}}
  ],
  "highlights": ["достижение 1", "достижение 2"],
  "bottlenecks": ["проблема 1", "проблема 2"],
  "week_focus": "На что обратить внимание на этой неделе (1 абзац)"
}}

Отвечай ТОЛЬКО валидным JSON, без markdown-обёртки."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=(
            "Ты — аналитик инженерного проектного бюро. "
            "Отвечай строго валидным JSON без markdown-обёртки, комментариев и лишнего текста. "
            "Используй только факты из предоставленных данных, не додумывай."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Защита от markdown-блока ```json ... ```
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Возвращаем деградированный ответ вместо краша
        print(f"  [WARN] Не удалось разобрать JSON от Claude: {e}")
        return {
            "summary": "Анализ недоступен — ошибка парсинга ответа Claude.",
            "risks": [],
            "highlights": [],
            "bottlenecks": [],
            "week_focus": raw[:500],
        }


def main():
    now_msk = datetime.now(MSK)
    date_str = now_msk.strftime("%Y-%m-%d")
    print(f"[{now_msk.isoformat()}] Запуск ежедневного анализа...")

    latest = load_latest()
    if not latest:
        print("  Нет данных в data/latest.json — пропускаем.")
        return

    snapshots = load_snapshots_for_today()
    print(f"  Загружено снапшотов: {len(snapshots)}")

    changes = detect_changes(snapshots)
    print(f"  Изменений за день: {len(changes)}")

    context = build_context(latest, snapshots, changes)
    print("  Отправка в Claude API...")

    analysis = analyze_with_claude(context)
    print("  Анализ получен.")

    report = {
        "date": date_str,
        "generated_at": now_msk.isoformat(),
        "snapshots_count": len(snapshots),
        "changes": changes,
        "analysis": analysis,
        "stats": latest.get("stats", {}),
        "risks": latest.get("risks", []),
        "total_tasks": latest.get("total_tasks", 0),
    }

    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/{date_str}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Обновляем latest_report.json для дашборда
    with open("reports/latest_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  Сохранено: {report_path}")
    print(f"  Итог: {analysis.get('summary', '')}")


if __name__ == "__main__":
    main()
