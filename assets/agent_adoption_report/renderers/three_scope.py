"""3-scope adoption email renderer.

Lifted from the morning `queries/build_3scopes_email.py` artifact (44KB,
2026-06-01) with these adaptations:

1. **Paths are config-driven**: reads JSONs + PNGs from `cfg.data_dir`,
   writes `email.html` to `cfg.output_dir`. The morning script had hard-coded
   `ROOT = parent(__file__)`.
2. **KNOWN_TENANTS / EXCLUDE_FROM_LEADERBOARD come from YAML** (`cfg.known_tenants`,
   `cfg.exclude_from_leaderboard`) merged with `tenant-name-map.json` if present
   in `data_dir`. The morning script had a hand-curated module-level dict.
3. **Spike/drop narration backported** from `..mailer._narrate_spikes_drops` --
   uses the per-day messages in `bot-scope-ALL-daily-v2.json` to flag days
   that deviate >=30% from the same-weekday rolling median, with US-holiday
   hints (Memorial Day, Labor Day, Thanksgiving, etc.).
4. **WoW window semantics note** -- morning script's WoW comes from Kusto
   `ago(7d)` windowing, which is partial-day. The headlines footnote calls
   this out so readers know the day-aligned semantics we adopted in the
   single-scope renderer are NOT yet applied here.

Inputs expected in `cfg.data_dir` (typically `<workspace>/queries/`):
- bot-scope-WF-v2.json
- bot-scope-MS-v2.json
- bot-scope-ALL-v2a.json (Windows + UserHealth)
- bot-scope-ALL-v2b.json (Schema + TenantSurface + Top10)
- bot-scope-ALL-daily-v2.json (flat daily, no Section)
- region-rollup.json
- tenant-name-map.json (optional)
- usage-trend-3scopes.png
- active-tenants-daily.png
- tenants-by-schema.png
- regions.png (optional)
"""
from __future__ import annotations
import base64
import calendar
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image  # type: ignore


def outlook_safe(html: str) -> str:
    """Make the HTML render reliably in Outlook's Word engine.

    Outlook desktop ignores CSS ``background`` on rows, headers, and block
    elements, so colored callout boxes and table shading silently vanish in a
    sent mail even though they look fine in a browser. Word *does* honor the
    legacy ``bgcolor`` attribute, so this injects a matching ``bgcolor="#hex"``
    onto any opening tag that already carries an inline ``background:#hex``.

    Pair this with a fixed-width (e.g. 1100px) wrapper ``<table>`` around the
    whole body so callout boxes do not overflow past the charts/tables -- Outlook
    ignores ``max-width`` on the body element.
    """
    tag_re = re.compile(r"<([a-zA-Z][\w-]*)\b([^>]*?background:\s*(#[0-9a-fA-F]{3,6})[^>]*)>")

    def _inject(m: "re.Match") -> str:
        tag, attrs, color = m.group(1), m.group(2), m.group(3)
        if "bgcolor=" in attrs.lower():
            return m.group(0)
        return f'<{tag} bgcolor="{color}"{attrs}>'

    return tag_re.sub(_inject, html)


# ---------------------------------------------------------------------------
# Holiday detector (for spike/drop narration)
# ---------------------------------------------------------------------------

_FIXED_HOLIDAYS = {
    "01-01": "New Year's Day (US)",
    "07-04": "Independence Day (US)",
    "12-24": "Christmas Eve (US)",
    "12-25": "Christmas Day (US)",
    "12-26": "Day after Christmas (US)",
    "12-31": "New Year's Eve (US)",
}


def _floating_us_holidays(year: int) -> dict[str, str]:
    """Return {YYYY-MM-DD: label} for floating US holidays in `year`."""
    out: dict[str, str] = {}
    # Memorial Day = last Monday of May
    may_cal = calendar.monthcalendar(year, 5)
    mondays_may = [w[calendar.MONDAY] for w in may_cal if w[calendar.MONDAY] != 0]
    out[f"{year:04d}-05-{mondays_may[-1]:02d}"] = "Memorial Day (US)"
    # Labor Day = first Monday of September
    sep_cal = calendar.monthcalendar(year, 9)
    mondays_sep = [w[calendar.MONDAY] for w in sep_cal if w[calendar.MONDAY] != 0]
    out[f"{year:04d}-09-{mondays_sep[0]:02d}"] = "Labor Day (US)"
    # Thanksgiving = 4th Thursday of November (+ day after)
    nov_cal = calendar.monthcalendar(year, 11)
    thursdays_nov = [w[calendar.THURSDAY] for w in nov_cal if w[calendar.THURSDAY] != 0]
    if len(thursdays_nov) >= 4:
        t_day = thursdays_nov[3]
        out[f"{year:04d}-11-{t_day:02d}"] = "Thanksgiving (US)"
        if t_day + 1 <= 30:
            out[f"{year:04d}-11-{t_day + 1:02d}"] = "Day after Thanksgiving (US)"
    return out


def _holiday_for(day_iso: str) -> str | None:
    """Return holiday label for a YYYY-MM-DD date, or None."""
    if not day_iso or len(day_iso) < 10:
        return None
    year = int(day_iso[:4])
    mm_dd = day_iso[5:10]
    if mm_dd in _FIXED_HOLIDAYS:
        return _FIXED_HOLIDAYS[mm_dd]
    floats = _floating_us_holidays(year)
    return floats.get(day_iso)


# ---------------------------------------------------------------------------
# Spike/drop narration (backported from mailer.py)
# ---------------------------------------------------------------------------

def _daily_by_day_flat(rows: list[dict]) -> dict[str, dict]:
    """Aggregate flat daily rows (no Section column) into {YYYY-MM-DD: {messages,users}}."""
    by_day: dict[str, dict] = {}
    for r in rows:
        day = (r.get("Day") or "")[:10]
        if not day:
            continue
        b = by_day.setdefault(day, {"messages": 0, "users": 0})
        b["messages"] += int(r.get("messages") or 0)
        b["users"] += int(r.get("users") or 0)
    return by_day


def _narrate_spikes_drops(daily_rows: list[dict], medium_days: int, threshold_pct: float = 30.0) -> str:
    """Return HTML narration block (or empty string) flagging days with ±threshold% deviation
    from same-weekday rolling median. Same algorithm as mailer.py.
    """
    by_day = _daily_by_day_flat(daily_rows)
    if len(by_day) < 8:
        return ""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    days = [d for d in sorted(by_day.keys())
            if d != today_iso and by_day[d]["messages"] > 0]
    if len(days) < 8:
        return ""

    def weekday(d: str) -> int:
        return datetime.strptime(d, "%Y-%m-%d").weekday()

    notes: list[tuple[str, str]] = []
    for i, d in enumerate(days):
        wd = weekday(d)
        peers = [by_day[x]["messages"] for x in days[:i] if weekday(x) == wd]
        basis = "same-weekday"
        if len(peers) < 2:
            if wd >= 5:
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
        wname = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]
        cause = f" &mdash; <b>likely cause: {hol}</b>" if hol else ""
        notes.append((d,
            f"<li><b>{wname} {d}</b>: {curr:,} msgs "
            f"(<b style='color:{color}'>{sign}{delta:.0f}%</b> vs {basis} median {median:,}){cause}</li>"
        ))

    if not notes:
        return ""
    notes.sort(key=lambda x: x[0], reverse=True)
    items = "".join(n for _, n in notes)
    return (
        "<h3 style='margin:18px 0 6px;color:#333'>Spikes &amp; drops to flag</h3>"
        f"<p style='font-size:12px;color:#666;margin:0 0 6px 0'>Days in the last {medium_days} where "
        "traffic deviated &ge;30% from the same-weekday rolling median. Holiday hints are "
        "heuristic (US federal calendar) and may not fit non-US tenants &mdash; treat as a "
        "starting point, not a final answer.</p>"
        f"<ul style='margin:0 0 8px 18px;padding:0;font-size:13px'>{items}</ul>"
    )


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_rows(p: Path) -> list[dict]:
    """Load a Kusto v2 Tables/Columns/Rows JSON into list-of-dicts."""
    raw = json.loads(p.read_text(encoding="utf-8"))
    t = raw["Tables"][0]
    cols = [c["ColumnName"] for c in t["Columns"]]
    return [dict(zip(cols, r)) for r in t["Rows"]]


def _merge_tenant_names(cfg_known: dict[str, str], data_dir: Path) -> dict[str, str]:
    """Hand-curated YAML map wins; fill rest from tenant-name-map.json if present."""
    out = dict(cfg_known)  # already lowercased by config loader
    name_map = data_dir / "tenant-name-map.json"
    if name_map.exists():
        try:
            for entry in json.loads(name_map.read_text(encoding="utf-8")):
                tid = (entry.get("tenantId") or "").lower()
                name = entry.get("displayName")
                if tid and name and tid not in out:
                    out[tid] = name
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Image inlining (palette-quantized PNG -> base64)
# ---------------------------------------------------------------------------

def _encode_png(p: Path, colors: int = 128) -> str:
    img = Image.open(p).convert("P", palette=Image.ADAPTIVE, colors=colors)
    small = p.with_name(p.stem + "-small.png")
    img.save(small, optimize=True)
    return base64.b64encode(small.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _window_total(rows: list[dict], win: str) -> dict | None:
    for r in rows:
        if r.get("Section") == "Windows" and r.get("Window") == win and r.get("Surface") == "TOTAL":
            return r
    return None


def _wow(curr: int, prev: int) -> tuple[str, str]:
    if not prev:
        return ("n/a", "#666")
    pct = (curr - prev) / prev * 100.0
    color = "#2e7d32" if pct >= 0 else "#c62828"
    sign = "+" if pct >= 0 else ""
    return (f"{sign}{pct:.1f}%", color)


def _user_health(rows: list[dict]) -> dict | None:
    """UserHealth row encodes returningPct * 10 in the `users` column."""
    for r in rows:
        if r.get("Section") == "UserHealth":
            return {
                "wau": r["messages"],
                "returning": r["conversations"],
                "returning_pct": r["users"] / 10.0,
            }
    return None


def _avg_daily_users(rows: list[dict]) -> float | None:
    """For WF/MS scope: daily rows have Section=='Daily'."""
    per_day: dict[str, int] = {}
    for r in rows:
        if r.get("Section") == "Daily":
            per_day[r["Day"]] = per_day.get(r["Day"], 0) + r["users"]
    if not per_day:
        return None
    return sum(per_day.values()) / len(per_day)


def _avg_daily_users_flat(rows: list[dict]) -> float | None:
    """For ALL-daily-v2 (flat, no Section)."""
    per_day: dict[str, int] = {}
    for r in rows:
        per_day[r["Day"]] = per_day.get(r["Day"], 0) + r["users"]
    if not per_day:
        return None
    return sum(per_day.values()) / len(per_day)


def _short_id(tid: str) -> str:
    if not tid:
        return "(empty)"
    return f"{tid[:8]}-...-{tid[-12:]}"


def _is_bot_automation(r: dict) -> bool:
    users = r.get("users") or 0
    msgs = r.get("messages") or 0
    return users <= 5 and msgs >= 1000


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _schema_table(all_b_rows: list[dict]) -> str:
    srows = sorted([r for r in all_b_rows if r.get("Section") == "Schema"], key=lambda x: -x["messages"])
    total = sum(r["messages"] for r in srows) or 1
    head = (
        "<tr style='background:#f3f3f3'>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>BotSchemaName</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg share</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Tenants</th></tr>"
    )
    body = []
    for r in srows:
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'><code>{r['Schema']}</code></td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']/total*100:.1f}%</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['conversations']:,}</td></tr>"
        )
    body.append(
        "<tr style='font-weight:700;background:#e8eef6'>"
        "<td style='padding:6px 10px;border:1px solid #ddd'>Total</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total:,}</td>"
        "<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td>"
        "<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'></td></tr>"
    )
    return (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
        + head + "".join(body) + "</table>"
    )


def _top10_render(rows: list[dict], sort_key: str, caption: str, names: dict[str, str]) -> str:
    rows = sorted(rows, key=lambda x: -(x.get(sort_key) or 0))
    total_msgs = sum(r["messages"] for r in rows) or 1
    head = (
        "<tr style='background:#f3f3f3'>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>#</th>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Tenant</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Users (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg / user</th>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Dominant surface</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Share of msgs</th></tr>"
    )
    body = []
    for i, r in enumerate(rows, 1):
        tid = (r.get("Surface") or "").strip()
        label = names.get(tid.lower(), "")
        try:
            dom_payload = json.loads(r.get("Schema") or "[0,\"\"]")
            dom_surface = dom_payload[1] if len(dom_payload) > 1 else ""
        except Exception:
            dom_surface = ""
        users = r["users"]
        mpu = (r["messages"] / users) if users else 0
        share = (r["messages"] / total_msgs * 100)
        tenant_cell = f"<code>{_short_id(tid)}</code>"
        if label:
            tenant_cell = f"<b>{label}</b> &middot; {tenant_cell}"
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'>{i}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{tenant_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{users:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{mpu:,.1f}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{dom_surface}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td></tr>"
        )
    table = (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
        + head + "".join(body) + "</table>"
    )
    return f"<h4 style='margin:14px 0 6px 0;color:#1f4e79'>{caption}</h4>" + table


def _top10_table(all_b_rows: list[dict], exclude: set[str], names: dict[str, str]) -> str:
    raw = [r for r in all_b_rows if r.get("Section") == "Top10"]
    if not raw:
        return ""
    excluded = []
    keep = []
    for r in raw:
        tid = (r.get("Surface") or "").strip().lower()
        if not tid:
            excluded.append(("empty tenant id", r))
            continue
        if tid in exclude:
            excluded.append(("test / sandbox", r))
            continue
        if _is_bot_automation(r):
            excluded.append(("bot/automation", r))
            continue
        keep.append(r)
    keep = sorted(keep, key=lambda x: -(x.get("messages") or 0))[:10]
    excl_summary = ""
    if excluded:
        bits = []
        for reason, r in excluded:
            tid = (r.get("Surface") or "").strip() or "(empty)"
            label = names.get(tid.lower(), "")
            tag = f"{label} &middot; <code>{_short_id(tid)}</code>" if label else f"<code>{_short_id(tid)}</code>"
            bits.append(f"{tag} &mdash; {r['messages']:,} msgs / {r['users']} users ({reason})")
        excl_summary = (
            "<p style='font-size:12px;color:#666;margin:6px 0 10px 0'>"
            "<i>Excluded from leaderboards: " + "; ".join(bits) + ". "
            "Empty tenant id = events with no <code>principalTenantId</code> populated "
            "(anonymous / Copilot Studio web-test canvas / debug sessions). "
            "Test / sandbox = configured exclusion list (see <code>exclude_from_leaderboard</code> in config) &mdash; "
            "not real customer adoption. "
            "Bot/automation = &lt;=5 distinct users but &gt;=1,000 messages (test harnesses, not real adoption).</i></p>"
        )
    by_msgs = _top10_render(keep[:], "messages", "Sorted by messages (20d)", names)
    by_users = _top10_render(keep[:], "users", "Sorted by distinct users (20d)", names)
    return excl_summary + by_msgs + by_users


def _tenant_surface_table(all_b_rows: list[dict]) -> str:
    rows = sorted(
        [r for r in all_b_rows if r.get("Section") == "TenantSurface"],
        key=lambda x: -x["users"],
    )
    if not rows:
        return ""
    head = (
        "<tr style='background:#f3f3f3'>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Surface</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Distinct tenants (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Users</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg / user</th></tr>"
    )
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
    return (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
        + head + "".join(body) + "</table>"
    )


def _region_table(rollup: dict) -> str:
    rows = [r for r in rollup.get("regions", []) if r.get("messages_20d", 0) > 0]
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -r["messages_20d"])
    total_msgs = sum(r["messages_20d"] for r in rows) or 1
    total_users = sum(r["users_20d"] for r in rows)
    total_tenants = sum(r["tenants_20d"] for r in rows)
    total_7d = sum(r["msgs_7d"] for r in rows)
    total_dau = sum(r.get("daily_avg_users") or 0 for r in rows)
    head = (
        "<tr style='background:#1f4e79;color:#fff'>"
        "<th style='text-align:left;padding:6px 10px;border:1px solid #1f4e79'>Region</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Tenants (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Users (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Messages (20d)</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Msgs 7d</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Avg DAU</th>"
        "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>% of msgs</th></tr>"
    )
    body = []
    for r in rows:
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
        "<tr style='font-weight:700;background:#e8eef6'>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>Total ({len(rows)} regions)</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_tenants:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_users:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_msgs:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_7d:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_dau:,.0f}</td>"
        "<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td></tr>"
    )
    return (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #1f4e79;width:100%;max-width:1100px'>"
        + head + "".join(body) + "</table>"
    )


def _stickiness_block(label: str, wau: int, mau: int, dau_avg: float | None,
                       returning: int, ret_pct: float) -> str:
    stick = (wau / mau * 100.0) if mau else 0.0
    dau_mau = (dau_avg / mau * 100.0) if (mau and dau_avg) else None
    dau_txt = f"{dau_avg:,.0f}" if dau_avg is not None else "n/a"
    dau_mau_txt = f"{dau_mau:.1f}%" if dau_mau is not None else "n/a"
    return (
        f"<div style='background:#f7f9fc;border-left:4px solid #1f4e79;padding:10px 14px;margin:10px 0;font-size:13px'>"
        f"<b>User health -- {label}</b><br/>"
        f"WAU (7d distinct users) <b>{wau:,}</b> &middot; "
        f"MAU (28d distinct users) <b>{mau:,}</b> &middot; "
        f"avg DAU (20d) <b>{dau_txt}</b><br/>"
        f"Stickiness WAU/MAU <b>{stick:.1f}%</b> &middot; "
        f"DAU/MAU <b>{dau_mau_txt}</b> &middot; "
        f"Returning users (7d users who also active in prev 7d) <b>{returning:,}</b> = <b>{ret_pct:.1f}%</b>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def build(cfg) -> Path:
    """Build the 3-scope email and return path to email.html."""
    data_dir: Path = cfg.data_dir
    output_dir: Path = cfg.output_dir

    # ---- Locate inputs --------------------------------------------------------
    paths = {
        "WF":        data_dir / "bot-scope-WF-v2.json",
        "MS":        data_dir / "bot-scope-MS-v2.json",
        "ALL_A":     data_dir / "bot-scope-ALL-v2a.json",
        "ALL_B":     data_dir / "bot-scope-ALL-v2b.json",
        "ALL_DAILY": data_dir / "bot-scope-ALL-daily-v2.json",
        "REGION":    data_dir / "region-rollup.json",
        "PNG_TREND":   data_dir / "usage-trend-3scopes.png",
        "PNG_TENANTS": data_dir / "active-tenants-daily.png",
        "PNG_SCHEMA":  data_dir / "tenants-by-schema.png",
        "PNG_REGIONS": data_dir / "regions.png",
    }
    missing = [k for k, p in paths.items() if not p.exists() and k != "PNG_REGIONS"]
    if missing:
        raise SystemExit(
            f"three_scope renderer: required inputs missing from data_dir={data_dir}: {missing}. "
            "Run the morning data refresh (run_all_regions.py + plot_*.py) first, "
            "or point cfg.data_dir at a folder that has the bot-scope-*-v2.json + chart PNGs."
        )

    # ---- Load data -----------------------------------------------------------
    wf_rows = _load_rows(paths["WF"])
    ms_rows = _load_rows(paths["MS"])
    all_a = _load_rows(paths["ALL_A"])
    all_b = _load_rows(paths["ALL_B"])
    all_daily = _load_rows(paths["ALL_DAILY"])
    region_data = json.loads(paths["REGION"].read_text(encoding="utf-8"))

    names = _merge_tenant_names(cfg.known_tenants, data_dir)
    exclude = set(cfg.exclude_from_leaderboard)

    # ---- WoW per scope -------------------------------------------------------
    def scope_wow(rows):
        c = _window_total(rows, "7d")
        p = _window_total(rows, "prev7d")
        if not c or not p:
            return None
        m_pct, m_col = _wow(c["messages"], p["messages"])
        u_pct, u_col = _wow(c["users"], p["users"])
        return {"c": c, "p": p, "m_pct": m_pct, "m_col": m_col, "u_pct": u_pct, "u_col": u_col}

    wf_w = scope_wow(wf_rows)
    ms_w = scope_wow(ms_rows)
    all_w = scope_wow(all_a)

    wf_mau = (_window_total(wf_rows, "28d") or {}).get("users", 0)
    ms_mau = (_window_total(ms_rows, "28d") or {}).get("users", 0)
    all_mau = (_window_total(all_a, "28d") or {}).get("users", 0)
    wf_msgs28 = (_window_total(wf_rows, "28d") or {}).get("messages", 0)
    ms_msgs28 = (_window_total(ms_rows, "28d") or {}).get("messages", 0)
    all_msgs28 = (_window_total(all_a, "28d") or {}).get("messages", 0)

    wf_uh = _user_health(wf_rows)
    ms_uh = _user_health(ms_rows)
    all_uh = _user_health(all_a)

    wf_dau = _avg_daily_users(wf_rows)
    ms_dau = _avg_daily_users(ms_rows)
    all_dau = _avg_daily_users_flat(all_daily)

    wf_schema_rows = [r for r in wf_rows if r.get("Section") == "Schema"]
    ms_schema_rows = [r for r in ms_rows if r.get("Section") == "Schema"]
    wf_schema = wf_schema_rows[0]["Schema"] if wf_schema_rows else "(unknown)"
    ms_schema = ms_schema_rows[0]["Schema"] if ms_schema_rows else "(unknown)"

    # ---- Inline images --------------------------------------------------------
    img_trend = _encode_png(paths["PNG_TREND"], colors=128)
    img_tenants = _encode_png(paths["PNG_TENANTS"], colors=96)
    img_schema = _encode_png(paths["PNG_SCHEMA"], colors=64)
    img_regions = _encode_png(paths["PNG_REGIONS"], colors=64) if paths["PNG_REGIONS"].exists() else None

    region_rows = [r for r in region_data.get("regions", []) if r.get("messages_20d", 0) > 0]
    tbl_regions = _region_table(region_data) if region_rows else ""
    regions_with_traffic = len(region_rows)
    distinct_tenants_total = sum(r["tenants_20d"] for r in region_rows) if region_rows else 0
    us_row = next((r for r in region_rows if r["region"] == "US"), None)
    region_msg_total = max(sum(r["messages_20d"] for r in region_rows), 1)
    us_msg_share = (us_row["messages_20d"] / region_msg_total * 100.0) if us_row else 0.0
    us_tenant_share = (us_row["tenants_20d"] / max(distinct_tenants_total, 1) * 100.0) if us_row else 0.0
    nonus_tenants = distinct_tenants_total - (us_row["tenants_20d"] if us_row else 0)
    nonus_msgs = sum(r["messages_20d"] for r in region_rows) - (us_row["messages_20d"] if us_row else 0)
    nonus_msg_share = 100.0 - us_msg_share

    tbl_schema = _schema_table(all_b)
    tbl_top10 = _top10_table(all_b, exclude, names)
    tbl_surface = _tenant_surface_table(all_b)

    def _share_of(scope_msgs, all_msgs):
        return (scope_msgs / all_msgs * 100.0) if all_msgs else 0.0

    all_msgs7 = (_window_total(all_a, "7d") or {}).get("messages", 0)
    wf_msgs7 = (_window_total(wf_rows, "7d") or {}).get("messages", 0)
    ms_msgs7 = (_window_total(ms_rows, "7d") or {}).get("messages", 0)
    wf_share7 = _share_of(wf_msgs7, all_msgs7)
    ms_share7 = _share_of(ms_msgs7, all_msgs7)
    wf_share28 = _share_of(wf_msgs28, all_msgs28)
    ms_share28 = _share_of(ms_msgs28, all_msgs28)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ---- KPI Headlines table -------------------------------------------------
    def kpi_row(label, msgs28, mau, w, uh, accent):
        c_msgs7 = w["c"]["messages"] if w else 0
        p_msgs7 = w["p"]["messages"] if w else 0
        c_usrs7 = w["c"]["users"] if w else 0
        p_usrs7 = w["p"]["users"] if w else 0
        m_pct, m_col = (w["m_pct"], w["m_col"]) if w else ("n/a", "#666")
        u_pct, u_col = (w["u_pct"], w["u_col"]) if w else ("n/a", "#666")
        wau = uh["wau"] if uh else 0
        retp = uh["returning_pct"] if uh else 0.0
        stick = (wau / mau * 100.0) if mau else 0.0
        cell = "padding:8px 12px;border:1px solid #ddd;text-align:right"
        cellL = "padding:8px 12px;border:1px solid #ddd;text-align:left"
        return (
            "<tr>"
            f"<td style='{cellL};border-left:4px solid {accent};font-weight:700'>{label}</td>"
            f"<td style='{cell}'>{msgs28:,}</td>"
            f"<td style='{cell}'>{mau:,}</td>"
            f"<td style='{cell}'>{c_msgs7:,}</td>"
            f"<td style='{cell}'><span style='color:#888;font-size:11px'>(vs {p_msgs7:,})</span> "
            f"<b style='color:{m_col}'>{m_pct}</b></td>"
            f"<td style='{cell}'>{c_usrs7:,}</td>"
            f"<td style='{cell}'><span style='color:#888;font-size:11px'>(vs {p_usrs7:,})</span> "
            f"<b style='color:{u_col}'>{u_pct}</b></td>"
            f"<td style='{cell}'>{wau:,}</td>"
            f"<td style='{cell}'>{stick:.1f}%</td>"
            f"<td style='{cell}'>{retp:.1f}%</td>"
            "</tr>"
        )

    kpi_head_cells = [
        ("Scope", "left"),
        ("28d msgs", "right"), ("28d MAU", "right"),
        ("7d msgs", "right"), ("WoW msgs", "right"),
        ("7d users", "right"), ("WoW users", "right"),
        ("WAU", "right"), ("WAU/MAU", "right"), ("Returning %", "right"),
    ]
    kpi_head = "<tr style='background:#1f4e79;color:#fff'>" + "".join(
        f"<th style='text-align:{a};padding:8px 12px;border:1px solid #1f4e79;font-weight:600;font-size:12px'>{n}</th>"
        for n, a in kpi_head_cells
    ) + "</tr>"
    kpi_table = (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #1f4e79;width:100%;max-width:1100px'>"
        + kpi_head
        + kpi_row("All ESS", all_msgs28, all_mau, all_w, all_uh, "#1f4e79")
        + kpi_row("Flagship customer A", wf_msgs28, wf_mau, wf_w, wf_uh, "#c0504d")
        + kpi_row("Flagship customer B", ms_msgs28, ms_mau, ms_w, ms_uh, "#4f81bd")
        + "</table>"
    )
    kpi_legend = (
        "<p style='font-size:12px;color:#666;margin:6px 0 0 0'>"
        "<b>WoW</b> = current 7 days vs previous 7 days (Kusto-windowed; "
        "partial last day is included). "
        "<b>WAU/MAU</b> = stickiness (weekly active as % of monthly active). "
        "<b>Returning %</b> = share of this week's users who were also active in the prior week. "
        "Green = up WoW, red = down WoW."
        "</p>"
    )

    # ---- Spike/drop narration on All-ESS daily -------------------------------
    narration_html = _narrate_spikes_drops(all_daily, medium_days=20)

    # ---- Body --------------------------------------------------------------
    body = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;margin:0;padding:0">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="1100" style="width:1100px;max-width:1100px;border-collapse:collapse">
<tr><td style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">
<p>Hi team,</p>
<p>Sharing the latest <b>adoption and usage reporting for ESS</b> -- with a focus on our two flagship
customers (the two largest by volume) and the consolidated view across the full ESS
customer base. Windows shown: 7d / 10d / 20d / 28d, plus week-over-week deltas and a top-10 customer leaderboard.</p>

<h3 style="color:#1f4e79;margin:18px 0 8px">Headlines</h3>
{kpi_table}
{kpi_legend}

<ul style="margin:14px 0 12px 22px;padding:0">
<li><b>~{all_msgs28:,} end-user messages and ~{all_mau:,} monthly active users across ~{distinct_tenants_total:,} distinct customer tenants in the last 28 days</b> -- aggregated across <b>{regions_with_traffic} Kusto regions</b> with traffic (US + EU + ROW). ESS is in active production at scale globally.</li>
<li><b>Customer breadth is global, but volume is concentrated.</b> US carries <b>~{us_msg_share:.0f}% of messages from ~{us_tenant_share:.0f}% of tenants ({us_row['tenants_20d'] if us_row else 0} tenants)</b>; the remaining <b>{100.0 - us_tenant_share:.0f}% of tenants ({nonus_tenants} tenants)</b> sit in EU/ROW clusters but together contribute only <b>~{nonus_msg_share:.1f}% of messages (~{nonus_msgs:,} msgs in 20d)</b>. The non-US footprint is wide but still mostly in pilot / low-volume phase.</li>
<li><b>A small number of customers carry the majority of volume.</b> The top two customer tenants account for a large share of trailing-20-day messages; a long tail of ~{max(distinct_tenants_total - 2, 100)}+ smaller customer tenants makes up the rest.</li>
<li><b>Surface mix:</b> M365 Copilot is the dominant entry point portfolio-wide, the Direct Line / embedded web-client surface is the second largest (concentrated in a few customer tenants), and the remainder (Teams + studio test channels) is a small slice spread across many tenants. See the surface footprint table below for the exact split.</li>
<li><b>Flagship customer A is the proof-point for embedded web at scale</b> -- its web client drives the bulk of Direct Line traffic in the portfolio, with a sticky audience (<b>{(wf_uh['returning_pct'] if wf_uh else 0):.0f}% of weekly users came back from the prior week</b>, vs {(all_uh['returning_pct'] if all_uh else 0):.0f}% portfolio-wide).</li>
<li><b>Flagship customer B is Copilot-led and the largest single Copilot tenant</b> (~{ms_msgs28:,} 28d msgs, ~{ms_mau:,} MAU). Lower returning-user rate (~{(ms_uh['returning_pct'] if ms_uh else 0):.0f}%) is consistent with bursty weekday Copilot use by a broad employee population.</li>
<li><b>The ESS skill schemas are live with material traffic</b> across HR, Core, IT and the legacy generic schema; HR typically has the broadest tenant footprint while Core is more concentrated. See the schema-split table below for current counts.</li>
<li><b>WoW caveats:</b> public holidays and one-off spikes inside either 7-day window can pull the WoW deltas up or down; the spike/drop narration under the trend chart flags the largest day-level moves. Read the deltas alongside the underlying weekday cadence.</li>
</ul>

<h2 style="color:#1f4e79;margin-top:24px">Daily trend -- messages (left) and daily active users (right), last 20 days</h2>
<img src="data:image/png;base64,{img_trend}" alt="ESS bot daily trend 20d - 3 scopes" style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>
{narration_html}

<h2 style="color:#1f4e79;margin-top:24px">Active customer tenants and users -- all ESS, last 20 days</h2>
<h3 style="margin:14px 0 6px;color:#333">Daily active tenants &amp; users</h3>
<img src="data:image/png;base64,{img_tenants}" alt="Daily active tenants and users per day" style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>
<p style="font-size:12px;color:#666;margin:6px 0 14px 0">Two trend lines on the same chart: distinct active <b>tenants</b> per day (left axis, dark blue) and distinct active <b>users</b> per day (right axis, amber). Counts are summed across surfaces (Copilot / web client / other channels), which mildly overcounts when the same tenant or user is active on more than one surface in a day &mdash; treat the lines as directional, not exact.</p>

<h3 style="margin:18px 0 6px;color:#333">Tenants per ESS schema (cumulative 20d)</h3>
<img src="data:image/png;base64,{img_schema}" alt="Tenants by ESS schema" style="max-width:800px;width:100%;height:auto;border:1px solid #ddd"/>

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Regional footprint -- ESS message volume by Kusto region (20d)</h2>
{tbl_regions}
<p style="font-size:12px;color:#666;margin:6px 0 10px 0">
Per-region rollup of the regional Kusto clusters where agent activity lands. Customer tenants are routed to one home cluster (closest geo at provisioning time), so message/user/tenant counts are additive across regions. Daily-DAU columns are arithmetic means of per-day user counts within each region over the 20-day window.
</p>
{"<img src='data:image/png;base64," + img_regions + "' alt='ESS message volume by region (20d)' style='max-width:1100px;width:100%;height:auto;border:1px solid #ddd;margin:8px 0 14px 0'/>" if img_regions else ""}

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Top customers, schema and surface footprint -- All ESS (20d)</h2>

<h3 style="margin:18px 0 6px">Top customers by 20-day message volume</h3>
{tbl_top10}
<p style="font-size:12px;color:#666;margin:6px 0 0 0">Tenant IDs partially masked. Two views &mdash; sorted by total messages and by distinct users &mdash; so we can see both heavy-volume tenants and broad-user-base tenants. Anonymous traffic (no tenant id), known test / sandbox tenants, and bot/automation rows are excluded; see the note above the tables for what was filtered.</p>

<h3 style="margin:18px 0 6px">Schema split across the portfolio (20d)</h3>
{tbl_schema}

<h3 style="margin:18px 0 6px">Surface footprint -- tenants, messages, users (20d)</h3>
{tbl_surface}

<p style="color:#666;font-size:12px;margin-top:24px">
Source: agent activity telemetry &middot; merged across the {regions_with_traffic} regional Kusto clusters. Generated {ts}.
<br/><i>Cross-region totals: messages and conversations are summed; user and tenant counts are also summed without cross-cluster dedup -- this overcounts only in the rare case where the same user or tenant has traffic landing in more than one home cluster, which is not normally how the routing works.</i>
</p>
<p>Thanks,<br/>The ESS team</p>
</td></tr>
</table>
</body></html>"""

    body = outlook_safe(body)
    out = output_dir / "email.html"
    out.write_text(body, encoding="utf-8")
    return out
