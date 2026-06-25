"""HTML email builder for the adoption-report pipeline.

Produces a single self-contained email.html under cfg.output_dir, with charts
embedded as base64 PNG data URIs. No external image references -- safe to paste
into Outlook.
"""
from __future__ import annotations
import base64
import json
import re as _re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .config import ReportConfig

SURFACES = ["Copilot", "Web client", "Others"]


def _short_id(tid: str) -> str:
    if not tid:
        return "(empty)"
    if len(tid) < 24:
        return tid
    return f"{tid[:8]}-...-{tid[-12:]}"


def _is_bot_automation(r: dict) -> bool:
    return (r.get("users") or 0) <= 5 and (r.get("messages") or 0) >= 1000


def _encode_png(p: Path, colors: int = 96) -> str:
    img = Image.open(p).convert("P", palette=Image.ADAPTIVE, colors=colors)
    small = p.with_name(p.stem + "-small.png")
    img.save(small, optimize=True)
    return base64.b64encode(small.read_bytes()).decode("ascii")


def _load(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    t = doc["Tables"][0]
    cols = [c["ColumnName"] for c in t["Columns"]]
    return [dict(zip(cols, r)) for r in t["Rows"]]


def _window_total(rows: list[dict], window: str) -> dict | None:
    for r in rows:
        if r.get("Section") == "Windows" and r.get("Window") == window and r.get("Surface") == "TOTAL":
            return r
    return None


def _wow(curr: int, prev: int) -> tuple[str, str]:
    if not prev:
        return ("n/a", "#666")
    delta = (curr - prev) / prev * 100.0
    color = "#0a6e3d" if delta >= 0 else "#a51b1b"
    sign = "+" if delta >= 0 else ""
    return (f"{sign}{delta:.1f}%", color)


def _user_health(rows: list[dict]) -> dict | None:
    for r in rows:
        if r.get("Section") == "UserHealth":
            wau = int(r.get("messages") or 0)
            ret = int(r.get("conversations") or 0)
            pct = (ret / wau * 100.0) if wau else 0.0
            return {"wau": wau, "returned": ret, "returning_pct": pct}
    return None


def _avg_daily_users_flat(daily_rows: list[dict]) -> float:
    by_day: dict[str, int] = {}
    for r in daily_rows:
        d = r.get("Day")
        by_day[d] = by_day.get(d, 0) + int(r.get("users") or 0)
    if not by_day:
        return 0.0
    return sum(by_day.values()) / len(by_day)


# ---- Spike/drop narration ----------------------------------------------------

# A small, conservative US/global holiday hint table. Keyed by "MM-DD" so it
# works across years without needing per-year fixed dates. Floating holidays
# (Memorial Day, Thanksgiving, Labor Day) are computed below.
_FIXED_HOLIDAYS = {
    "01-01": "New Year's Day",
    "07-04": "US Independence Day",
    "12-24": "Christmas Eve",
    "12-25": "Christmas Day",
    "12-26": "Boxing Day",
    "12-31": "New Year's Eve",
}


def _floating_us_holidays(year: int) -> dict[str, str]:
    """Return {YYYY-MM-DD: label} for floating US federal holidays in `year`.

    Memorial Day  = last Monday of May
    Labor Day     = first Monday of September
    Thanksgiving  = fourth Thursday of November
    """
    from calendar import monthcalendar
    out: dict[str, str] = {}
    # Memorial Day -- last Monday of May (Monday = index 0)
    mondays_may = [w[0] for w in monthcalendar(year, 5) if w[0] != 0]
    if mondays_may:
        out[f"{year}-05-{mondays_may[-1]:02d}"] = "Memorial Day (US)"
    # Labor Day -- first Monday of September
    mondays_sep = [w[0] for w in monthcalendar(year, 9) if w[0] != 0]
    if mondays_sep:
        out[f"{year}-09-{mondays_sep[0]:02d}"] = "Labor Day (US)"
    # Thanksgiving -- fourth Thursday of November (Thursday = index 3)
    thursdays_nov = [w[3] for w in monthcalendar(year, 11) if w[3] != 0]
    if len(thursdays_nov) >= 4:
        out[f"{year}-11-{thursdays_nov[3]:02d}"] = "Thanksgiving (US)"
        if len(thursdays_nov) >= 4:
            # Day after Thanksgiving is a near-universal day off too.
            out[f"{year}-11-{thursdays_nov[3] + 1:02d}"] = "Day after Thanksgiving"
    return out


def _holiday_for(day: str) -> str | None:
    # day is "YYYY-MM-DD"
    y = int(day[:4])
    md = day[5:]
    if md in _FIXED_HOLIDAYS:
        return _FIXED_HOLIDAYS[md]
    return _floating_us_holidays(y).get(day)


def _daily_by_day(daily_rows: list[dict]) -> dict[str, dict]:
    """Sum across regions and surfaces -> {YYYY-MM-DD: {messages, users}}."""
    out: dict[str, dict] = {}
    for r in daily_rows:
        d = (r.get("Day") or "")[:10]
        # Require a date-shaped Day; skip stray non-date rows (e.g. a Kusto
        # diagnostic/'Exceptions' row) that would later break date parsing.
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        b = out.setdefault(d, {"messages": 0, "users": 0})
        b["messages"] += int(r.get("messages") or 0)
        b["users"]    += int(r.get("users")    or 0)
    return out


def _narrate_spikes_drops(daily_rows: list[dict], medium_days: int,
                          threshold_pct: float = 30.0) -> str:
    """Return an HTML <ul> of factual notes for days that deviate >= threshold%
    from a same-weekday rolling median, with holiday hints where applicable.

    Returns empty string if there's nothing notable.
    """
    by_day = _daily_by_day(daily_rows)
    if len(by_day) < 8:
        return ""

    # Sort days; drop today (partial) and any zero-traffic warm-up days.
    days = sorted(by_day.keys())
    today_iso = datetime.now(timezone.utc).date().isoformat()
    days = [d for d in days if d != today_iso and by_day[d]["messages"] > 0]
    if len(days) < 8:
        return ""

    # Weekday classifier
    from datetime import date as _date
    def weekday(d: str) -> int:
        return _date.fromisoformat(d).weekday()  # Mon=0, Sun=6

    notes: list[tuple[str, str]] = []  # (date, html-fragment)
    # Compare each day against the median of the same weekday in the window.
    # If we don't have at least 2 same-weekday peers (small window), fall back
    # to the 7-day trailing median of WEEKDAYS ONLY so heavy outliers still fire
    # without making every weekend look like a 'drop' against weekday medians.
    for i, d in enumerate(days):
        wd = weekday(d)
        peers = [by_day[x]["messages"] for x in days[:i] if weekday(x) == wd]
        basis = "same-weekday"
        if len(peers) < 2:
            if wd >= 5:
                # Sat/Sun with no same-weekday history -> nothing useful to say.
                continue
            peers = [by_day[x]["messages"] for x in days[max(0, i - 7):i] if weekday(x) < 5]
            basis = "weekday trailing"
        if len(peers) < 3:
            continue
        peers_sorted = sorted(peers)
        median = peers_sorted[len(peers_sorted) // 2]
        if median <= 0:
            continue
        curr = by_day[d]["messages"]
        delta = (curr - median) / median * 100.0
        if abs(delta) < threshold_pct:
            continue
        color = "#0a6e3d" if delta > 0 else "#a51b1b"
        sign = "+" if delta > 0 else ""
        hol = _holiday_for(d)
        weekday_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]
        cause = f" &mdash; <b>likely cause: {hol}</b>" if hol else ""
        notes.append((d,
            f"<li><b>{weekday_name} {d}</b>: {curr:,} msgs "
            f"(<b style='color:{color}'>{sign}{delta:.0f}%</b> vs {basis} median {median:,}){cause}</li>"
        ))

    if not notes:
        return ""
    # Most recent first, cap at 6 callouts
    notes.sort(key=lambda x: x[0], reverse=True)
    items = "".join(html for _, html in notes[:6])
    return (
        "<h3 style='margin:18px 0 6px;color:#333'>Spikes &amp; drops to flag</h3>"
        "<p style='font-size:12px;color:#666;margin:0 0 6px 0'>"
        f"Days in the last {medium_days} where traffic deviated &ge;{threshold_pct:.0f}% from the "
        "same-weekday rolling median. Holiday hints are heuristic (US federal calendar) and may not "
        "fit non-US tenants &mdash; treat as a starting point, not a final answer.</p>"
        f"<ul style='margin:0 0 8px 18px;padding:0;font-size:13px'>{items}</ul>"
    )


def _surface_mix(rows: list[dict], window: str) -> dict[str, float]:
    out = {s: 0 for s in SURFACES}
    total = 0
    for r in rows:
        if r.get("Section") == "Windows" and r.get("Window") == window and r.get("Surface") in SURFACES:
            out[r["Surface"]] = int(r["messages"])
            total += int(r["messages"])
    if not total:
        return {s: 0.0 for s in SURFACES}
    return {s: out[s] / total * 100.0 for s in SURFACES}


# ---- Renderers ---------------------------------------------------------------

def _kpi_row(label: str, msgs_long: int, mau: int, qau: int, qoq_pct: str, qoq_col: str,
             w: dict | None, uh: dict | None,
             accent: str, windows_lbl: dict) -> str:
    c_msgs = w["c"]["messages"] if w else 0
    p_msgs = w["p"]["messages"] if w else 0
    c_usrs = w["c"]["users"]    if w else 0
    p_usrs = w["p"]["users"]    if w else 0
    m_pct, m_col = _wow(c_msgs, p_msgs) if w else ("n/a", "#666")
    u_pct, u_col = _wow(c_usrs, p_usrs) if w else ("n/a", "#666")
    wau_mau = (uh["wau"] / mau * 100.0) if (uh and mau) else 0.0
    returning = uh["returning_pct"] if uh else 0.0
    return (
        f"<tr>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;border-left:4px solid {accent};font-weight:600'>{label}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{msgs_long:,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{mau:,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{qau:,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>"
        f"<b style='color:{qoq_col}'>{qoq_pct}</b></td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{c_msgs:,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>"
        f"<span style='color:#999;font-size:11px'>(vs {p_msgs:,})</span> "
        f"<b style='color:{m_col}'>{m_pct}</b></td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{c_usrs:,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>"
        f"<span style='color:#999;font-size:11px'>(vs {p_usrs:,})</span> "
        f"<b style='color:{u_col}'>{u_pct}</b></td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{(uh['wau'] if uh else 0):,}</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{wau_mau:.1f}%</td>"
        f"<td style='padding:6px 10px;text-align:right;border:1px solid #ddd'>{returning:.1f}%</td>"
        f"</tr>"
    )


def _region_table(rollup_path: Path) -> tuple[str, dict]:
    rollup = json.loads(rollup_path.read_text(encoding="utf-8"))
    regions = [r for r in rollup.get("regions", []) if r.get("messages_20d", 0) > 0]
    regions.sort(key=lambda r: -r["messages_20d"])
    if not regions:
        return "", rollup
    total_msgs    = sum(r["messages_20d"] for r in regions) or 1
    total_users   = sum(r["users_20d"] for r in regions)
    total_tenants = sum(r["tenants_20d"] for r in regions)
    total_7d      = sum(r["msgs_7d"] for r in regions)
    total_dau     = sum(r.get("daily_avg_users") or 0 for r in regions)
    head = ("<tr style='background:#1f4e79;color:#fff'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #1f4e79'>Region</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Tenants</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Users</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Messages</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Msgs short-win</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Avg DAU</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>% of msgs</th></tr>")
    body = []
    for r in regions:
        share = r["messages_20d"] / total_msgs * 100.0
        dau = r.get("daily_avg_users") or 0
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'><b>{r['region']}</b></td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['tenants_20d']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['users_20d']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages_20d']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['msgs_7d']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{dau:,.0f}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td></tr>"
        )
    body.append(
        f"<tr style='font-weight:700;background:#e8eef6'>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>Total ({len(regions)} regions)</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_tenants:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_users:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_msgs:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_7d:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_dau:,.0f}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td></tr>"
    )
    table = ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
             "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #1f4e79;"
             "width:100%;max-width:1100px'>" + head + "".join(body) + "</table>")
    return table, rollup


def _schema_table(b_rows: list[dict]) -> str:
    rows = [r for r in b_rows if r.get("Section") == "Schema"]
    rows.sort(key=lambda r: -(r.get("messages") or 0))
    if not rows:
        return ""
    total = sum(r["messages"] for r in rows) or 1
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>BotSchemaName</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg share</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Tenants</th></tr>")
    body = []
    for r in rows:
        share = r["messages"] / total * 100.0
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'><code>{r['Schema']}</code></td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['conversations']:,}</td></tr>"
        )
    body.append(
        f"<tr style='font-weight:700;background:#f6f6f6'>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>Total</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'></td></tr>"
    )
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
            + head + "".join(body) + "</table>")


def _surface_table(b_rows: list[dict]) -> str:
    rows = sorted([r for r in b_rows if r.get("Section") == "TenantSurface"],
                  key=lambda r: -(r.get("conversations") or 0))
    if not rows:
        return ""
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Surface</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Distinct tenants</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Users</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg / user</th></tr>")
    body = []
    for r in rows:
        mpu = (r["messages"] / r["users"]) if r["users"] else 0
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'>{r['Surface']}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['conversations']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['users']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{mpu:,.1f}</td></tr>"
        )
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
            + head + "".join(body) + "</table>")


def _top_customer_table(b_rows: list[dict], cfg: ReportConfig) -> tuple[str, list[str]]:
    """Return (HTML, list-of-excluded-bullets)."""
    raw = [r for r in b_rows if r.get("Section") == "Top10"]
    if not raw:
        return "", []
    excluded: list[tuple[str, dict]] = []
    keep: list[dict] = []
    for r in raw:
        tid = (r.get("Surface") or "").strip().lower()
        if not tid:
            excluded.append(("empty tenant id", r)); continue
        if tid in cfg.exclude_from_leaderboard:
            excluded.append(("test / sandbox", r)); continue
        if _is_bot_automation(r):
            excluded.append(("bot/automation", r)); continue
        keep.append(r)
    keep.sort(key=lambda x: -(x.get("messages") or 0))
    keep = keep[: cfg.top_n_leaderboard]
    total_msgs = sum(r["messages"] for r in keep) or 1
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>#</th>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Tenant</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Users</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg / user</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Cadence</th>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Dominant surface</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Share</th></tr>")
    body = []
    for i, r in enumerate(keep, 1):
        tid = (r.get("Surface") or "").strip().lower()
        label = cfg.known_tenants.get(tid, "")
        users = r["users"]
        mpu = (r["messages"] / users) if users else 0
        share = r["messages"] / total_msgs * 100.0
        tenant_cell = f"<code>{_short_id(tid)}</code>"
        if label:
            tenant_cell = f"<b>{label}</b> &middot; {tenant_cell}"
        cad = r.get("cadence")
        cad_cell = f"{cad:.1f} d/wk" if cad else "&ndash;"
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'>{i}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{tenant_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{users:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{mpu:,.1f}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{cad_cell}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{r.get('Schema') or ''}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td></tr>"
        )
    table = ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
             "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
             + head + "".join(body) + "</table>")
    excl_bullets = []
    for reason, r in excluded:
        tid = (r.get("Surface") or "").strip().lower() or "(empty)"
        label = cfg.known_tenants.get(tid, "")
        tag = f"{label} &middot; <code>{_short_id(tid)}</code>" if label else f"<code>{_short_id(tid)}</code>"
        excl_bullets.append(f"{tag} &mdash; {r['messages']:,} msgs / {r['users']} users ({reason})")
    return table, excl_bullets


# ---- Main entry --------------------------------------------------------------

def build(cfg: ReportConfig, chart_paths: dict[str, Path]) -> Path:
    a_rows = _load(cfg.output_dir / "report-windows.json")
    b_rows = _load(cfg.output_dir / "report-tables.json")
    daily  = _load(cfg.output_dir / "report-daily.json")
    rollup_path = cfg.output_dir / "region-rollup.json"

    short_lbl = cfg.windows.short_label
    medium_lbl = cfg.windows.medium_label
    long_lbl = cfg.windows.long_label
    quarter_lbl = cfg.windows.quarter_label
    prev_lbl = cfg.windows.prev_short_label
    prev_q_lbl = cfg.windows.prev_quarter_label

    c_short = _window_total(a_rows, short_lbl)
    p_short = _window_total(a_rows, prev_lbl)
    c_quarter = _window_total(a_rows, quarter_lbl) or {}
    p_quarter = _window_total(a_rows, prev_q_lbl) or {}
    medium_total = _window_total(a_rows, medium_lbl) or {}
    long_total   = _window_total(a_rows, long_lbl) or {}
    uh = _user_health(a_rows)
    mau = int(long_total.get("users") or 0)
    qau = int(c_quarter.get("users") or 0)
    qau_prev = int(p_quarter.get("users") or 0)
    qoq_pct, qoq_col = _wow(qau, qau_prev) if qau_prev else ("n/a", "#666")
    msgs_long = int(long_total.get("messages") or 0)

    w_pkg = None
    if c_short and p_short:
        w_pkg = {"c": c_short, "p": p_short}

    avg_dau = _avg_daily_users_flat(daily)
    surface_mix = _surface_mix(a_rows, medium_lbl)

    img_trend    = _encode_png(chart_paths["trend"],          colors=128)
    img_t_u      = _encode_png(chart_paths["tenants_users"],  colors=96)
    img_schema   = _encode_png(chart_paths["schema"],         colors=64)
    img_regions  = _encode_png(chart_paths["regions"],        colors=64)

    region_html, rollup = _region_table(rollup_path)
    regions_with_traffic = sum(1 for r in rollup.get("regions", []) if r.get("messages_20d", 0) > 0)
    distinct_tenants_total = sum(r["tenants_20d"] for r in rollup.get("regions", [])
                                 if r.get("messages_20d", 0) > 0)
    top_html, excl_bullets = _top_customer_table(b_rows, cfg)
    schema_html  = _schema_table(b_rows)
    surface_html = _surface_table(b_rows)

    kpi = _kpi_row(cfg.agent_name, msgs_long, mau, qau, qoq_pct, qoq_col,
                   w_pkg, uh, "#1f4e79", {})
    kpi_table = (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
        "<tr style='background:#1f4e79;color:#fff'>"
        f"<th style='text-align:left;padding:6px 10px;border:1px solid #1f4e79'>Scope</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>{long_lbl} msgs</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>{long_lbl} MAU</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>{quarter_lbl} QAU</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>QoQ users</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>{short_lbl} msgs</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>WoW msgs</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>{short_lbl} users</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>WoW users</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>WAU</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>WAU/MAU</th>"
        f"<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Returning %</th>"
        f"</tr>{kpi}</table>"
    )

    excl_block = ""
    if excl_bullets:
        excl_block = (
            "<p style='font-size:12px;color:#666;margin:6px 0 10px 0'>"
            "<i>Excluded from leaderboard: " + "; ".join(excl_bullets) + ".</i></p>"
        )

    region_chart = (
        f"<img src='data:image/png;base64,{img_regions}' alt='Regional footprint' "
        f"style='max-width:1100px;width:100%;height:auto;border:1px solid #ddd;margin:8px 0 14px 0'/>"
    )

    narration_html = _narrate_spikes_drops(daily, cfg.windows.medium_days)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cluster_list = " / ".join(c.name for c in cfg.kusto.clusters)

    body = f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;font-size:14px;line-height:1.4">
<h2 style="color:#1f4e79;margin:0 0 4px 0">{cfg.agent_name} -- adoption and usage</h2>
<p style="color:#666;margin:0 0 14px 0;font-size:12px">
Auto-generated {ts}. Source: <code>TraceEvents.{cfg.kusto.activity_name}</code>
across {regions_with_traffic} {cfg.kusto.database} regions out of {len(cfg.kusto.clusters)} queried.
</p>

<h3 style="color:#1f4e79;margin:18px 0 8px">Headlines</h3>
{kpi_table}
<p style="font-size:11px;color:#777;margin:6px 0 14px 0">
WoW = current {short_lbl} vs previous {short_lbl} (both aligned to whole UTC days, today excluded so
the in-progress day doesn't drag down the comparison). QoQ = current {quarter_lbl} vs previous {quarter_lbl}.
WAU/MAU = stickiness. Returning % = share of this period's users who were also active in the prior
period. Green = up, red = down.
</p>

<ul>
<li><b>{msgs_long:,} end-user messages</b> and <b>{mau:,} {long_lbl} active users</b> across
<b>{distinct_tenants_total:,} distinct customer tenants</b> in {regions_with_traffic} regions.</li>
<li>Surface mix ({medium_lbl} msgs): Copilot <b>{surface_mix['Copilot']:.1f}%</b> &middot;
Web client <b>{surface_mix['Web client']:.1f}%</b> &middot; Others <b>{surface_mix['Others']:.1f}%</b>.
Avg DAU over the {medium_lbl} window: <b>{avg_dau:,.0f}</b>.</li>
<li>Returning-user rate: <b>{(uh['returning_pct'] if uh else 0):.1f}%</b>
(WAU/MAU stickiness: <b>{((uh['wau']/mau*100.0) if uh and mau else 0):.1f}%</b>).</li>
</ul>

<h2 style="color:#1f4e79;margin-top:24px">Daily trend -- messages and DAU, last {cfg.windows.medium_days} days</h2>
<img src="data:image/png;base64,{img_trend}" alt="Daily trend"
     style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>
{narration_html}

<h2 style="color:#1f4e79;margin-top:24px">Active tenants and users, last {cfg.windows.medium_days} days</h2>
<img src="data:image/png;base64,{img_t_u}" alt="Daily active tenants and users"
     style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>

<h3 style="margin:18px 0 6px;color:#333">Tenants per schema</h3>
<img src="data:image/png;base64,{img_schema}" alt="Tenants by schema"
     style="max-width:800px;width:100%;height:auto;border:1px solid #ddd"/>

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Regional footprint</h2>
{region_html}
<p style="font-size:12px;color:#666;margin:6px 0 10px 0">
Per-region rollup across the {len(cfg.kusto.clusters)} regional Kusto clusters.
Tenants are routed to one home cluster, so message/user/tenant counts are additive across regions.
</p>
{region_chart}

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Top customers, schema and surface footprint</h2>

<h3 style="margin:18px 0 6px">Top {cfg.top_n_leaderboard} customers by message volume ({medium_lbl})</h3>
{top_html}
<p style="font-size:12px;color:#666;margin:6px 0 10px 0">
<i>Cadence</i> = average active days per week per weekly-active user (sum of daily distinct users over
the last {short_lbl} &divide; {short_lbl} distinct users); higher = users come back on more days.
</p>
{excl_block}

<h3 style="margin:18px 0 6px">Schema split ({medium_lbl})</h3>
{schema_html}

<h3 style="margin:18px 0 6px">Surface footprint -- tenants, messages, users ({medium_lbl})</h3>
{surface_html}

<p style="color:#666;font-size:12px;margin-top:24px">
Source: <code>TraceEvents.{cfg.kusto.activity_name}</code> &middot;
merged across <code>{cluster_list}</code> &middot;
DB <code>{cfg.kusto.database}</code> &middot;
schemas: {", ".join(f"<code>{s}</code>" for s in cfg.schemas)}.
<br/><i>Cross-region totals: messages and conversations are summed; user and tenant counts are summed
without cross-cluster dedup -- this overcounts only in the rare case where the same user or tenant
has traffic landing in more than one home cluster.</i>
</p>
</body></html>"""

    out = cfg.output_dir / "email.html"
    out.write_text(body, encoding="utf-8")
    return out
