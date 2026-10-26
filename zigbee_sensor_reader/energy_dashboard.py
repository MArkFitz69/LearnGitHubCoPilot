"""Read-only energy views. Page requests never poll devices or migrate the DB."""

from datetime import date, datetime, timedelta
import json

from flask import Blueprint, jsonify, render_template, request

from .energy_data import DAILY_METRICS, LIVE_METRICS, LONDON, day_bounds, tariff_context, utc_now

energy = Blueprint("energy", __name__)


def device_name(source, device):
    if source == "duracell":
        return "Duracell inverter / Dura16"
    if ":zappi:" in device:
        return f"Zappi {device.rsplit(':', 1)[-1]}"
    if ":eddi:" in device:
        return f"Eddi {device.rsplit(':', 1)[-1]}"
    return "myenergi " + device.rsplit(":", 1)[-1]


def snapshot(conn, day, now=None):
    now = now or utc_now()
    start, end = day_bounds(day)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    ready = {"energy_samples", "energy_daily", "energy_health"} <= tables
    health = [dict(row) for row in conn.execute("SELECT * FROM energy_health")] if ready else []
    latest = conn.execute("""
        SELECT s.* FROM energy_samples s
        JOIN (
            SELECT source, device, MAX(observed_at) AS latest FROM energy_samples
            GROUP BY source, device
        ) last ON s.source=last.source AND s.device=last.device AND s.observed_at=last.latest
        ORDER BY s.source, s.device
    """).fetchall() if ready else []
    devices = {(row["source"], row["device"]) for row in latest}
    live = []
    for row in latest:
        values, qualities = json.loads(row["metrics"]), json.loads(row["quality"])
        stale = (now - datetime.fromisoformat(row["observed_at"])).total_seconds() > 1800
        for metric, value in values.items():
            if metric not in LIVE_METRICS:
                continue
            label, unit = LIVE_METRICS[metric]
            live.append({
                "source": row["source"], "device": device_name(row["source"], row["device"]),
                "metric": metric, "label": label, "value": value, "unit": unit,
                "quality": qualities[metric], "stale": stale,
                "observed_at": row["observed_at"],
            })
    missing_live = []
    for metric, (label, unit) in LIVE_METRICS.items():
        if not any(item["metric"] == metric for item in live):
            missing_live.append({"label": label, "unit": unit})

    first_day = day - timedelta(days=6)
    rows = conn.execute("""
        SELECT * FROM energy_daily WHERE day>=? AND day<=? ORDER BY day, source, device, metric
    """, (first_day.isoformat(), day.isoformat())).fetchall() if ready else []
    counters = {(r["source"], r["device"], r["day"], r["metric"]): dict(r) for r in rows}
    devices.update((r["source"], r["device"]) for r in rows)

    def daily_card(source, device, metric, selected):
        row = counters.get((source, device, selected.isoformat(), metric))
        verified = row is not None and row["quality"] == "verified"
        return {
            "source": source, "device": device_name(source, device), "metric": metric,
            "label": DAILY_METRICS[metric],
            "value": row["value"] if verified else None, "unit": "kWh",
            "plot_value": row["value"] if row else None, "verified": verified,
            "quality": row["quality"] if row else "unavailable",
            "coverage": row["coverage"] if row else "unavailable",
            "reason": row["reason"] if row else "No verified device counter/history received",
            "updated_at": row["updated_at"] if row else None,
        }

    daily = [
        daily_card("duracell", "inverter", metric, day)
        for metric in DAILY_METRICS if metric != "device_kwh"
    ]
    history_devices = sorted(
        (source, device) for source, device in devices
        if device.startswith(("myenergi:zappi:", "myenergi:eddi:"))
    )
    for kind, label in (("eddi", "Eddi consumed"), ("zappi", "Zappi delivered")):
        matching = [(s, d) for s, d in history_devices if f":{kind}:" in d]
        if not matching:
            daily.append({
                "label": label, "device": "myenergi", "source": "myenergi",
                "metric": "device_kwh", "value": None, "unit": "kWh",
                "quality": "unavailable", "coverage": "unavailable",
                "reason": "No device energy history collected", "updated_at": None,
            })
        for source, device in matching:
            card = daily_card(source, device, "device_kwh", day)
            card["label"] = label
            daily.append(card)

    summary = []
    for offset in range(7):
        selected = first_day + timedelta(days=offset)
        cells = [
            daily_card("duracell", "inverter", metric, selected)
            for metric in DAILY_METRICS if metric != "device_kwh"
        ]
        for kind in ("eddi", "zappi"):
            matches = [
                daily_card(s, d, "device_kwh", selected)
                for s, d in history_devices if f":{kind}:" in d
            ]
            available = matches and all(item["value"] is not None for item in matches)
            plottable = bool(matches) and all(item["plot_value"] is not None for item in matches)
            cells.append({
                "plot_value": sum(item["plot_value"] for item in matches) if plottable else None,
                "verified": bool(available),
                "value": sum(item["value"] for item in matches) if available else None,
                "coverage": "complete" if available and all(
                    item["coverage"] == "complete" for item in matches
                ) else "partial",
            })
        summary.append({"day": selected.isoformat(), "cells": cells})

    daily_graph = {"series": [], "days": [
        {"day": row["day"], "label": date.fromisoformat(row["day"]).strftime("%d %b"),
         "x": 90 + index * 90}
        for index, row in enumerate(summary)
    ]}
    for label, index, color, width in (
        ("Grid import", 0, "#8ac9ff", 64),
        ("Solar generation", 1, "#ffd166", 52),
        ("House consumption", None, "#c9a0ff", 40),
        ("Eddi consumption", 4, "#5de0ad", 28),
        ("Zappi delivered", 5, "#ff91bc", 16),
    ):
        daily_graph["series"].append({
            "label": label, "color": color, "width": width,
            "bars": [{
                **day_item,
                "value": row["cells"][index]["plot_value"] if index is not None else None,
                "verified": row["cells"][index]["verified"] if index is not None else False,
                "coverage": row["cells"][index]["coverage"] if index is not None else "unavailable",
            } for day_item, row in zip(daily_graph["days"], summary)],
        })
    maximum = max((
        bar["value"] for series_item in daily_graph["series"]
        for bar in series_item["bars"] if bar["value"] is not None
    ), default=0)
    daily_graph["maximum"] = max(maximum, 1)
    for series_item in daily_graph["series"]:
        series_item["available"] = any(bar["value"] is not None for bar in series_item["bars"])
        for bar in series_item["bars"]:
            bar["height"] = 240 * bar["value"] / daily_graph["maximum"] if bar["value"] is not None else 0

    trend_rows = conn.execute("""
        SELECT * FROM energy_samples WHERE observed_at>=? AND observed_at<?
        ORDER BY observed_at
    """, (start.isoformat(), end.isoformat())).fetchall() if ready else []
    series = {}
    for row in trend_rows:
        x = 100 * (datetime.fromisoformat(row["observed_at"]) - start) / (end - start)
        for metric, value in json.loads(row["metrics"]).items():
            if metric not in LIVE_METRICS:
                continue
            key = (row["source"], row["device"], metric)
            series.setdefault(key, []).append({"x": x, "value": value, "time": row["observed_at"]})
    charts = []
    for (source, device, metric), points in series.items():
        low = min(0, min(p["value"] for p in points))
        high = max(p["value"] for p in points)
        high = max(high, low + 1)
        # One point is a circle; gaps over 30 min break the line.
        segments, segment = [], []
        previous = None
        for point in points:
            if previous and (
                datetime.fromisoformat(point["time"]) - datetime.fromisoformat(previous)
            ).total_seconds() > 1800:
                segments.append(" ".join(segment))
                segment = []
            segment.append(f"{point['x'] * 6:.2f},{100 - (point['value'] - low) / (high - low) * 90:.2f}")
            previous = point["time"]
        if segment:
            segments.append(" ".join(segment))
        charts.append({
            "label": LIVE_METRICS[metric][0], "unit": LIVE_METRICS[metric][1],
            "source": device_name(source, device), "low": low, "high": high,
            "segments": segments, "points": points,
        })
    return {
        "day": day.isoformat(), "timezone": "Europe/London",
        "generated_at": now.isoformat(timespec="seconds"), "ready": ready,
        "live": live, "missing_live": missing_live, "daily": daily,
        "summary": summary, "daily_graph": daily_graph, "charts": charts, "health": health,
        "tariff": tariff_context(now, day),
    }


def requested_day():
    value = request.args.get("date")
    if value is None:
        return utc_now().astimezone(LONDON).date()
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD")
    if not date(2000, 1, 1) <= day <= utc_now().astimezone(LONDON).date():
        raise ValueError("Choose a date from 2000-01-01 through today")
    return day


def read_snapshot():
    from .web_server import get_db
    conn = get_db()
    try:
        return snapshot(conn, requested_day())
    finally:
        conn.close()


@energy.route("/api/energy")
def api_energy():
    try:
        return jsonify(read_snapshot())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@energy.route("/energy")
def dashboard():
    try:
        return render_template("energy.html", **read_snapshot())
    except ValueError as exc:
        return str(exc), 400
