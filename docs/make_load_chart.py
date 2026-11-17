"""Draw the README load-test chart (light + dark SVG) from a Locust stats-history CSV.

    python docs/make_load_chart.py loadtest/results/<run>/stats_stats_history.csv docs

Two small multiples on one time axis - simulated users and requests per second - each with its own
y-axis (never two scales on one plot). Latency is reported in a table instead: Locust's history file
holds cumulative percentiles, which would wrongly suggest latency never recovered after the spike.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

STAGES = [(0, 60, "warm-up"), (60, 180, "ramp-up"), (180, 360, "sustained"), (360, 480, "spike"), (480, 600, "recovery")]
THEMES = {  # series colours validated with the dataviz palette checker (band, chroma, >= 3:1 on the surface)
    "light": {"surface": "#ffffff", "border": "#e4e4e7", "primary": "#18181b", "secondary": "#52525b", "muted": "#71717a",
              "grid": "#f0f0f2", "axis": "#d4d4d8", "series": "#7c3aed"},
    "dark": {"surface": "#18181b", "border": "#2e2e33", "primary": "#fafafa", "secondary": "#d4d4d8", "muted": "#a1a1aa",
             "grid": "#26262b", "axis": "#3f3f46", "series": "#8b5cf6"},
}
W, H = 880, 470
X0, X1 = 76, 848
SPAN = 600
YMAX = 300
PANELS = [("Simulated users", 1, 128, 232), ("Requests per second", 2, 286, 390)]
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def load(path: Path) -> tuple[list[tuple[int, float, float]], int, int, datetime]:
    with path.open() as handle:
        rows = [r for r in csv.DictReader(handle) if r["Name"] == "Aggregated"]
    t0 = int(rows[0]["Timestamp"])
    points = [(int(r["Timestamp"]) - t0, float(r["User Count"]), float(r["Requests/s"])) for r in rows]
    points = [p for p in points if p[0] <= SPAN]
    last = rows[-1]
    return points, int(last["Total Request Count"]), int(last["Total Failure Count"]), datetime.fromtimestamp(t0, tz=timezone.utc)


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def x(t: float) -> float:
    return X0 + (X1 - X0) * t / SPAN


def y(value: float, top: int, bottom: int) -> float:
    return bottom - (bottom - top) * min(value, YMAX) / YMAX


def text(x_: float, y_: float, body: str, colour: str, size: float = 12, weight: int = 400, anchor: str = "start", extra: str = "") -> str:
    return (f'<text x="{x_:.1f}" y="{y_:.1f}" fill="{colour}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" {extra}>{body}</text>')


def draw(points, requests: int, failures: int, started: datetime, theme: dict) -> str:
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
        f'aria-labelledby="t d" font-family="{FONT}">',
        "<title id=\"t\">10-minute Locust load test</title>",
        f'<desc id="d">Simulated users rise in stages from 50 to 300 and fall back to 100 over ten minutes; requests per second '
        f'follow the load, peaking near {max(p[2] for p in points):.0f}. {requests:,} requests, {plural(failures, "failure")}.</desc>',
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="14" fill="{theme["surface"]}" stroke="{theme["border"]}"/>',
        text(32, 46, "10-minute load test (Locust)", theme["primary"], 18, 600),
        text(32, 70, f"{requests:,} requests · {plural(failures, 'failure')} · up to 300 simulated users · {started:%d %b %Y}",
             theme["secondary"], 13),
    ]
    for boundary in (60, 180, 360, 480):  # stage boundaries, recessive
        out.append(f'<line x1="{x(boundary):.1f}" y1="{PANELS[0][2]}" x2="{x(boundary):.1f}" y2="{PANELS[1][3]}" '
                   f'stroke="{theme["grid"]}" stroke-width="1"/>')
    for title, column, top, bottom in PANELS:
        out.append(text(32, top - 14, title, theme["primary"], 13, 600))
        for tick in (0, 100, 200, 300):
            colour = theme["axis"] if tick == 0 else theme["grid"]
            out.append(f'<line x1="{X0}" y1="{y(tick, top, bottom):.1f}" x2="{X1}" y2="{y(tick, top, bottom):.1f}" stroke="{colour}" stroke-width="1"/>')
            out.append(text(X0 - 10, y(tick, top, bottom) + 4, str(tick), theme["muted"], 11, anchor="end",
                            extra='style="font-variant-numeric: tabular-nums"'))
        line = " ".join(f"{'M' if i == 0 else 'L'}{x(p[0]):.1f},{y(p[column], top, bottom):.1f}" for i, p in enumerate(points))
        area = f"{line} L{x(points[-1][0]):.1f},{bottom} L{x(points[0][0]):.1f},{bottom} Z"
        out.append(f'<path d="{area}" fill="{theme["series"]}" fill-opacity="0.10"/>')
        out.append(f'<path d="{line}" fill="none" stroke="{theme["series"]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        if column == 1:  # direct labels: each stage's name and user count, just above the line
            for start, end, name in STAGES:
                users = [p[1] for p in points if start <= p[0] < end][-1]  # the level the stage settles at
                out.append(text((x(start) + x(end)) / 2, y(users, top, bottom) - 8, f"{name} · {users:.0f}", theme["secondary"], 11, anchor="middle"))
        else:  # one direct label: the peak
            peak = max(points, key=lambda p: p[2])
            px, py = x(peak[0]), y(peak[2], top, bottom)
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="6" fill="{theme["surface"]}"/>')
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{theme["series"]}"/>')
            out.append(text(px, py - 12, f"peak {peak[2]:.0f} req/s", theme["secondary"], 11, anchor="middle"))
    for minute in range(0, 11, 2):
        label = f"{minute} min" if minute == 10 else str(minute)
        anchor = "end" if minute == 10 else "middle"
        out.append(text(x(minute * 60) + (8 if minute == 10 else 0), PANELS[1][3] + 20, label, theme["muted"], 11, anchor=anchor,
                        extra='style="font-variant-numeric: tabular-nums"'))
    out.append(text(32, H - 24, "Throwaway stress stack on one laptop: 4 web processes, 2 AI workers, a fake LLM with injected "
                    "provider failures. One run - not a performance guarantee.", theme["muted"], 11.5))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main(source: str, out_dir: str) -> None:
    points, requests, failures, started = load(Path(source))
    for name, theme in THEMES.items():
        target = Path(out_dir) / f"load-test-{name}.svg"
        target.write_text(draw(points, requests, failures, started, theme), encoding="utf-8")
        print("wrote", target)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "docs")
