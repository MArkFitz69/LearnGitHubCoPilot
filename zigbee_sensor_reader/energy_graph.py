"""Selected-day line graph built from stored energy power samples."""

import json
from datetime import datetime, timedelta

from .energy_data import LONDON

BUCKET_SECONDS = 900
NAMES = ("grid", "solar", "battery", "eddi", "zappi")


def _pick(row):
    device = row["device"]
    if row["source"] == "duracell":
        return [("grid", "grid_power_w"), ("solar", "pv_power_w"), ("battery", "battery_power_w")]
    if ":eddi:" in device:
        return [("eddi", "device_power_w")]
    if ":zappi:" in device:
        return [("zappi", "device_power_w")]
    return []


def build_day_graph(rows, start, end, tariff):
    """Latest recorded power per 15-minute bucket; house power is derived.

    House = grid import + solar + battery discharge - eddi - zappi. Grid export is
    not measured, so this is only valid while nothing is exported.
    """
    total = int((end - start).total_seconds() // BUCKET_SECONDS)
    buckets = {name: {} for name in NAMES}
    for row in rows:
        index = int((datetime.fromisoformat(row["observed_at"]) - start).total_seconds()
                    // BUCKET_SECONDS)
        if not 0 <= index < total:
            continue
        metrics = json.loads(row["metrics"])
        for name, metric in _pick(row):
            if metrics.get(metric) is not None:
                buckets[name].setdefault(index, {})[row["device"]] = metrics[metric]
    values = {name: {i: sum(v.values()) for i, v in buckets[name].items()} for name in NAMES}
    house = {}
    for i in values["grid"]:
        if all(i in values[name] for name in NAMES):
            result = (values["grid"][i] + values["solar"][i] + values["battery"][i]
                      - values["eddi"][i] - values["zappi"][i])
            if result >= 0:
                house[i] = result
    specs = (
        ("Grid import (+) / export (-)", values["grid"], "#8ac9ff"),
        ("Solar", values["solar"], "#ffd166"),
        ("Battery discharge (+) / charge (-)", values["battery"], "#ff9f6b"),
        ("House (derived)", house, "#c9a0ff"),
        ("Eddi", values["eddi"], "#5de0ad"),
        ("Zappi", values["zappi"], "#ff91bc"),
    )
    everything = [v for _, data, _ in specs for v in data.values()]
    low = min([0] + everything)
    high = max([1] + everything)
    series = []
    for label, data, color in specs:
        segments, run = [], []
        for i in range(total):
            if i in data:
                run.append((
                    round(50 + 630 * i / total, 1),
                    round(270 - 240 * (data[i] - low) / (high - low), 1),
                    data[i],
                    (start + timedelta(seconds=BUCKET_SECONDS * i)).astimezone(LONDON).strftime("%H:%M"),
                ))
            elif run:
                segments.append(run)
                run = []
        if run:
            segments.append(run)
        series.append({
            "label": label, "color": color, "available": bool(data),
            "segments": [{
                "path": " ".join(("L" if n else "M") + f" {x} {y}" for n, (x, y, _, _) in enumerate(seg)),
                "points": [{"x": x, "y": y, "value": v, "time": t} for x, y, v, t in seg],
            } for seg in segments],
        })
    return {
        "series": series, "low": low, "high": high,
        "zero_y": 270 - 240 * (0 - low) / (high - low),
        "ticks": [{"y": 270 - 240 * f, "value": low + (high - low) * f}
                  for f in (0, 0.25, 0.5, 0.75, 1)],
        "hours": [{"x": 50 + 630 * h / 24, "label": f"{h:02d}:00"} for h in range(0, 25, 4)],
        "cheap": [{"x": 50 + 630 * item["left_pct"] / 100, "width": 630 * item["width_pct"] / 100}
                  for item in tariff["intervals"]],
        "has_data": any(item["available"] for item in series),
    }
