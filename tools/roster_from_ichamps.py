"""Выгружает состав чемпионата iRChamps в demo-roster.json для демо-режима.

Демо-данные оверлеев берут состав из `demo-roster.json` в папке данных iRC.
Этот скрипт делает такой файл из базы сайта — чтобы на трансляции проверяли
вёрстку на своих пилотах, номерах и дивизионах, а не на выдуманных.

    python tools/roster_from_ichamps.py                    # список чемпионатов
    python tools/roster_from_ichamps.py 11                 # выгрузить чемпионат 11
    python tools/roster_from_ichamps.py 11 --db путь.db    # другая база

По умолчанию база ищется там же, где лежит проект сайта. На проде путь
задаётся через --db или переменную ICHAMPS_DB.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(r"C:\Users\ssper\ichamps\instance\dev.db")


def data_dir() -> Path:
    base = os.environ.get("IRC_DATA_DIR")
    if base:
        return Path(base)
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "iRC"


def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        sys.exit(f"база не найдена: {db}")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def list_championships(c: sqlite3.Connection) -> None:
    rows = c.execute("""
        select ch.id, ch.name, count(r.id) as drivers
        from championships ch
        left join championship_registrations r
               on r.championship_id = ch.id and r.is_active = 1
        group by ch.id order by drivers desc, ch.id""").fetchall()
    print(f"{'id':>4}  {'пилотов':>7}  название")
    for r in rows:
        print(f"{r['id']:>4}  {r['drivers']:>7}  {r['name']}")


def export(c: sqlite3.Connection, champ_id: int) -> list[dict]:
    rows = c.execute("""
        select driver_name, car_number, team_name, division_name, division_code
        from championship_registrations
        where championship_id = ? and is_active = 1
        order by division_code, car_number""", (champ_id,)).fetchall()
    out = []
    for i, r in enumerate(rows, start=1):
        name = (r["driver_name"] or "").strip()
        if not name:
            continue
        out.append({
            "driver_name": name,
            "car_number": (r["car_number"] or str(i)).strip(),
            "team_name": (r["team_name"] or "").strip(),
            "division_name": (r["division_name"] or r["division_code"]
                              or "RACE").strip(),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("championship", nargs="?", type=int,
                    help="id чемпионата; без него — список")
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("ICHAMPS_DB") or DEFAULT_DB))
    ap.add_argument("--out", type=Path, help="куда писать (по умолчанию папка данных iRC)")
    args = ap.parse_args()

    c = connect(args.db)
    if args.championship is None:
        list_championships(c)
        return 0

    drivers = export(c, args.championship)
    if not drivers:
        sys.exit(f"в чемпионате {args.championship} нет активных участников")

    name = c.execute("select name from championships where id=?",
                     (args.championship,)).fetchone()
    out = args.out or (data_dir() / "demo-roster.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "championship": name["name"] if name else str(args.championship),
        "drivers": drivers,
    }, ensure_ascii=False, indent=2), "utf-8")

    classes = sorted({d["division_name"] for d in drivers})
    print(f"выгружено: {len(drivers)} пилотов, дивизионы: {', '.join(classes)}")
    print(f"файл: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
