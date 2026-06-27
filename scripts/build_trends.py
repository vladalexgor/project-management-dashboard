"""
Агрегирует снапшоты из data/history/ в data/trends.json.
Запускается после каждого сбора данных.
"""

import json
import glob
import os
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))


def main():
    files = sorted(glob.glob("data/history/*.json"))
    if not files:
        print("Нет снапшотов для трендов.")
        return

    # Берём по одному снапшоту на день (последний за день)
    by_date = {}
    for path in files:
        fname = os.path.basename(path)           # 2026-06-27_13-36.json
        date  = fname[:10]                        # 2026-06-27
        by_date[date] = path                      # перезаписываем → последний за день

    days = []
    for date in sorted(by_date)[-30:]:           # последние 30 дней
        try:
            with open(by_date[date], encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue

        s = snap.get("stats", {})
        bs = s.get("by_status", {})
        risks = snap.get("risks", [])

        days.append({
            "date":      date,
            "total":     snap.get("total_tasks", 0),
            "done":      bs.get("завершено", 0),
            "wip":       bs.get("в работе", 0),
            "waiting":   bs.get("ожидание", 0),
            "overdue":   len([r for r in risks if r.get("type") == "overdue"]),
            "urgent":    len([r for r in risks if r.get("type") == "urgent"]),
            "employees": {
                name: v["total"]
                for name, v in s.get("by_employee", {}).items()
            },
            "projects": s.get("by_project", {}),
        })

    # Топ-сотрудники по максимальной нагрузке за период
    all_emps = set()
    for d in days:
        all_emps.update(d["employees"].keys())

    emp_series = {
        emp: [d["employees"].get(emp, 0) for d in days]
        for emp in sorted(all_emps)
    }

    trends = {
        "generated_at": datetime.now(MSK).isoformat(),
        "days":         days,
        "emp_series":   emp_series,
        "dates":        [d["date"] for d in days],
    }

    with open("data/trends.json", "w", encoding="utf-8") as f:
        json.dump(trends, f, ensure_ascii=False, indent=2)

    print(f"Тренды: {len(days)} дней, {len(all_emps)} сотрудников → data/trends.json")


if __name__ == "__main__":
    main()
