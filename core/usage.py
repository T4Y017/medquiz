"""Gemini günlük istek sayacı. Ücretsiz katman kotaları gece yarısı (Pasifik saati) sıfırlanır."""
from __future__ import annotations

import datetime as dt

_db = None   # AppContext tarafından bağlanır


def attach(db) -> None:
    global _db
    _db = db


def _us_dst(d: dt.datetime) -> bool:
    """ABD yaz saati: Mart'ın 2. pazarı 02:00 – Kasım'ın 1. pazarı 02:00 (yerel)."""
    y = d.year
    march = dt.datetime(y, 3, 8 + (6 - dt.date(y, 3, 8).weekday()) % 7, 2)
    nov = dt.datetime(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7, 2)
    return march <= d.replace(tzinfo=None) < nov


def pacific_day(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    std = now.astimezone(dt.timezone(dt.timedelta(hours=-8)))
    off = -7 if _us_dst(std) else -8
    return now.astimezone(dt.timezone(dt.timedelta(hours=off))).strftime("%Y-%m-%d")


def record(model: str) -> None:
    if _db is not None:
        try:
            _db.inc_usage(pacific_day(), model)
        except Exception:  # noqa: BLE001
            pass


def note_limit(model: str, value: str) -> None:
    if _db is not None and value:
        try:
            _db.set_meta(f"daily_limit:{model}", str(value))
            _db.set_meta(f"exhausted:{model}", pacific_day())
        except Exception:  # noqa: BLE001
            pass


def today() -> dict[str, dict]:
    """{model: {"used": n, "limit": "20" | "", "exhausted": bool}}"""
    if _db is None:
        return {}
    day = pacific_day()
    out = {m: {"used": c, "limit": _db.get_meta(f"daily_limit:{m}"), "exhausted": False}
           for m, c in _db.get_usage(day).items()}
    for m in list(out) + []:
        out[m]["exhausted"] = _db.get_meta(f"exhausted:{m}") == day
    return out


def is_exhausted(model: str) -> bool:
    if _db is None:
        return False
    try:
        return _db.get_meta(f"exhausted:{model}") == pacific_day()
    except Exception:  # noqa: BLE001
        return False
