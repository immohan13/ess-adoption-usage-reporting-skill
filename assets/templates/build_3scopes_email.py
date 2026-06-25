"""Build 3-section HTML email body: WF / MS / ALL ESS, with inline charts + tables + observations.

v2: reads bot-scope-{WF,MS}-v2.json, bot-scope-ALL-v2a.json, bot-scope-ALL-v2b.json,
bot-scope-ALL-daily-v2.json. Adds WoW%, 28d MAU, WAU + returning%, top-10
customer leaderboard, tenants-per-surface mini-table, and an active-tenants-per-day chart.
"""
from __future__ import annotations
import base64, json, re
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent
PNG_TREND   = ROOT / "usage-trend-3scopes.png"
PNG_TENANTS = ROOT / "active-tenants-daily.png"
PNG_SCHEMA  = ROOT / "tenants-by-schema.png"
PNG_REGIONS = ROOT / "regions.png"
WF        = ROOT / "bot-scope-WF-v2.json"
MS        = ROOT / "bot-scope-MS-v2.json"
ALL_A     = ROOT / "bot-scope-ALL-v2a.json"   # Windows + UserHealth
ALL_B     = ROOT / "bot-scope-ALL-v2b.json"   # Schema + TenantSurface + Top10
ALL_DAILY = ROOT / "bot-scope-ALL-daily-v2.json"
REGION_ROLLUP = ROOT / "region-rollup.json"
OUT       = ROOT / "email-3scopes.html"

# Customer tenant labels + leaderboard exclusions are loaded from a LOCAL,
# git-ignored config so that no customer data ever lives in source control.
#   1. Copy tenant-config.example.json -> tenant-config.local.json (same folder
#      as this script / your working `queries/` dir).
#   2. Fill in your real tenant GUID -> display-name map and exclusion list.
# tenant-config.local.json (and tenant-name-map.json) are listed in .gitignore.
KNOWN_TENANTS: dict[str, str] = {}
EXCLUDE_FROM_LEADERBOARD: set[str] = set()
# Display labels for the two flagship scopes in the Headline table / callouts.
# Overridable via tenant-config.local.json ("flagship_a_label"/"flagship_b_label").
FLAGSHIP_A = "Flagship customer A"
FLAGSHIP_B = "Flagship customer B"


def _load_tenant_config() -> None:
    """Populate KNOWN_TENANTS / EXCLUDE_FROM_LEADERBOARD from a local JSON file.

    Looks for tenant-config.local.json (preferred) or tenant-config.json next to
    this script. Schema:
        {
          "known_tenants": { "<tenant-guid>": "Display Name", ... },
          "exclude_from_leaderboard": [ "<tenant-guid>", ... ],
          "flagship_a_label": "...", "flagship_b_label": "..."
        }
    Silently no-ops if the file is absent so the script still runs (tenants then
    show as masked GUIDs and nothing is suppressed from the leaderboard).
    """
    global FLAGSHIP_A, FLAGSHIP_B
    for _name in ("tenant-config.local.json", "tenant-config.json"):
        _p = ROOT / _name
        if not _p.exists():
            continue
        try:
            _cfg = json.loads(_p.read_text(encoding="utf-8"))
        except Exception:
            return
        for _tid, _label in (_cfg.get("known_tenants") or {}).items():
            if _tid and _label:
                KNOWN_TENANTS[_tid.lower()] = _label
        for _tid in (_cfg.get("exclude_from_leaderboard") or []):
            if _tid:
                EXCLUDE_FROM_LEADERBOARD.add(_tid.lower())
        if _cfg.get("flagship_a_label"):
            FLAGSHIP_A = _cfg["flagship_a_label"]
        if _cfg.get("flagship_b_label"):
            FLAGSHIP_B = _cfg["flagship_b_label"]
        return


_load_tenant_config()

# Merge in the Graph-resolved name map (hand-curated KNOWN_TENANTS wins).
_NAME_MAP = ROOT / "tenant-name-map.json"
if _NAME_MAP.exists():
    try:
        for _entry in json.loads(_NAME_MAP.read_text(encoding="utf-8")):
            _tid = (_entry.get("tenantId") or "").lower()
            _name = _entry.get("displayName")
            if _tid and _name and _tid not in KNOWN_TENANTS:
                KNOWN_TENANTS[_tid] = _name
    except Exception:
        pass

# Tenant -> served GPT model map (produced by build_model_map.py). Used to add a
# "Model" column to the Top customers leaderboard. Loads gracefully if absent.
MODEL_MAP: dict[str, dict] = {}
_MODEL_MAP_FILE = ROOT / "tenant-model-map.json"
if _MODEL_MAP_FILE.exists():
    try:
        MODEL_MAP = {k.lower(): v for k, v in json.loads(
            _MODEL_MAP_FILE.read_text(encoding="utf-8")).items()}
    except Exception:
        MODEL_MAP = {}


def friendly_model(m: str) -> str:
    """Relabel any raw model ids that slipped through the model map's friendly()."""
    if not m:
        return m
    low = m.lower()
    if low.startswith("gpt-5-switcher"):
        return "GPT-5 (preview)"
    return m

# Per-tenant weekly split (cur 7d vs prev 7d, messages + users) from tenant-wow.json.
# Used to add WoW% columns to the Top customers leaderboard. Loads gracefully if absent.
TENANT_WEEKLY: dict[str, dict] = {}
_TENANT_WOW_FILE = ROOT / "tenant-wow.json"
if _TENANT_WOW_FILE.exists():
    try:
        TENANT_WEEKLY = {k.lower(): v for k, v in json.loads(
            _TENANT_WOW_FILE.read_text(encoding="utf-8")).get("tenant_weekly", {}).items()}
    except Exception:
        TENANT_WEEKLY = {}

# Per-tenant cadence (avg active days/week per weekly-active user) from tenant-cadence.json.
# Used to add a Cadence column to the Top customers leaderboard. Loads gracefully if absent.
TENANT_CADENCE: dict[str, float] = {}
_TENANT_CADENCE_FILE = ROOT / "tenant-cadence.json"
if _TENANT_CADENCE_FILE.exists():
    try:
        TENANT_CADENCE = {k.lower(): float(v) for k, v in json.loads(
            _TENANT_CADENCE_FILE.read_text(encoding="utf-8")).get("tenant_cadence", {}).items()}
    except Exception:
        TENANT_CADENCE = {}

SURFACES = ["Web client", "Copilot", "MCS Test Chat", "Others"]

def load_rows(p: Path) -> list[dict]:
    raw = json.loads(p.read_text(encoding="utf-8"))
    t = raw["Tables"][0]
    cols = [c["ColumnName"] for c in t["Columns"]]
    return [dict(zip(cols, r)) for r in t["Rows"]]


def windows_table(rows: list[dict], wf_share_from: dict | None = None) -> str:
    """If wf_share_from is provided (an ALL bucket lookup), add a flagship-share column."""
    head_cols = ["Window", "Surface", "Messages", "Conversations", "Users", "Msg share"]
    if wf_share_from is not None:
        head_cols.append("Share of ALL")
    head = "<tr style='background:#f3f3f3'>" + "".join(
        f"<th style='text-align:{ 'left' if c in ('Window','Surface') else 'right' };padding:6px 10px;border:1px solid #ddd'>{c}</th>"
        for c in head_cols
    ) + "</tr>"
    body = []
    for win in ["7d", "10d", "28d"]:
        wrows = [r for r in rows if r["Section"] == "Windows" and r["Window"] == win]
        total = next((r for r in wrows if r["Surface"] == "TOTAL"), None)
        if not total:
            continue
        for s in SURFACES + ["TOTAL"]:
            r = next((x for x in wrows if x["Surface"] == s), None)
            if not r:
                continue
            share = (r["messages"] / total["messages"] * 100) if total["messages"] else 0
            bold = "font-weight:700;background:#e8eef6" if s == "TOTAL" else ""
            cells = [
                f"<td style='padding:6px 10px;border:1px solid #ddd'>{win}</td>",
                f"<td style='padding:6px 10px;border:1px solid #ddd'>{'Total' if s=='TOTAL' else s}</td>",
                f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>",
                f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['conversations']:,}</td>",
                f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['users']:,}</td>",
                f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td>",
            ]
            if wf_share_from is not None:
                all_total = wf_share_from.get((win, s), None)
                if all_total and all_total > 0:
                    cells.append(f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']/all_total*100:.1f}%</td>")
                else:
                    cells.append("<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>-</td>")
            body.append(f"<tr style='{bold}'>" + "".join(cells) + "</tr>")
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
            + head + "".join(body) + "</table>")


SCHEMA_LABELS = {
    "msdyn_copilotforemployeeselfservicehr": "ESS HR",
    "msdyn_copilotforemployeeselfservicecore": "ESS Hub / Front Door",
    "msdyn_copilotforemployeeselfserviceit": "ESS IT",
    "msdyn_copilotforemployeeselfservicefacilities": "ESS Facilities",
    "msdyn_copilotforemployeeselfservice": "ESS Classic",
}


def schema_label(raw: str) -> str:
    return SCHEMA_LABELS.get((raw or "").strip().lower(), raw)


def schema_table(all_rows: list[dict]) -> str:
    srows = sorted([r for r in all_rows if r["Section"] == "Schema"],
                   key=lambda x: -x["messages"])
    total = sum(r["messages"] for r in srows) or 1
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>ESS agent</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg share</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Tenants</th></tr>")
    body = []
    for r in srows:
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'>{schema_label(r['Schema'])}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']/total*100:.1f}%</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['conversations']:,}</td></tr>"
        )
    body.append(
        f"<tr style='font-weight:700;background:#e8eef6'>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>Total</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'></td></tr>"
    )
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
            + head + "".join(body) + "</table>")


def all_total_lookup(all_rows: list[dict]) -> dict:
    """Returns {(window, surface): messages} for ALL bucket."""
    out = {}
    for r in all_rows:
        if r["Section"] == "Windows":
            key = (r["Window"], r["Surface"])
            out[key] = r["messages"]
    return out


# --- v2 helpers --------------------------------------------------------------

def window_total(rows: list[dict], win: str) -> dict | None:
    for r in rows:
        if r.get("Section") == "Windows" and r.get("Window") == win and r.get("Surface") == "TOTAL":
            return r
    return None


def wow(curr: int, prev: int) -> tuple[str, str]:
    """Returns (delta_pct_text, color) where color is green/red/gray."""
    if not prev:
        return ("n/a", "#666")
    pct = (curr - prev) / prev * 100.0
    color = "#2e7d32" if pct >= 0 else "#c62828"
    sign = "+" if pct >= 0 else ""
    return (f"{sign}{pct:.1f}%", color)


def user_health(rows: list[dict]) -> dict | None:
    """Decode UserHealth row: users field encodes returningPct * 10."""
    for r in rows:
        if r.get("Section") == "UserHealth":
            return {
                "wau":           r["messages"],
                "returning":     r["conversations"],
                "returning_pct": r["users"] / 10.0,
            }
    return None


def avg_daily_users(rows: list[dict]) -> float | None:
    """For WF/MS daily sections (have Section=='Daily'), avg users summed across surfaces per day."""
    per_day = {}
    for r in rows:
        if r.get("Section") == "Daily":
            d = r["Day"]
            per_day[d] = per_day.get(d, 0) + r["users"]
    if not per_day:
        return None
    return sum(per_day.values()) / len(per_day)


def avg_daily_users_flat(rows: list[dict]) -> float | None:
    """For ALL-daily-v2 (flat, no Section column)."""
    per_day = {}
    for r in rows:
        d = r["Day"]
        per_day[d] = per_day.get(d, 0) + r["users"]
    if not per_day:
        return None
    return sum(per_day.values()) / len(per_day)


def stickiness_block(label: str, wau: int, mau: int, dau_avg: float | None, returning: int, ret_pct: float) -> str:
    """Compact user-health card."""
    stick = (wau / mau * 100.0) if mau else 0.0
    dau_mau = (dau_avg / mau * 100.0) if (mau and dau_avg) else None
    dau_txt = f"{dau_avg:,.0f}" if dau_avg is not None else "n/a"
    dau_mau_txt = f"{dau_mau:.1f}%" if dau_mau is not None else "n/a"
    return (
        f"<div style='background:#f7f9fc;border-left:4px solid #1f4e79;padding:10px 14px;margin:10px 0;font-size:13px'>"
        f"<b>User health -- {label}</b><br/>"
        f"WAU (7d distinct users) <b>{wau:,}</b> &middot; "
        f"MAU (28d distinct users) <b>{mau:,}</b> &middot; "
        f"avg DAU (28d) <b>{dau_txt}</b><br/>"
        f"Stickiness WAU/MAU <b>{stick:.1f}%</b> &middot; "
        f"DAU/MAU <b>{dau_mau_txt}</b> &middot; "
        f"Returning users (7d users who also active in prev 7d) <b>{returning:,}</b> = <b>{ret_pct:.1f}%</b>"
        f"</div>"
    )


def short_id(tid: str) -> str:
    if not tid:
        return "(empty)"
    return f"{tid[:8]}-...-{tid[-12:]}"


def _is_bot_automation(r: dict) -> bool:
    """Heuristic: very high msg/user ratio with tiny user counts -- test harness / sandbox, not real adoption."""
    users = r.get("users") or 0
    msgs = r.get("messages") or 0
    return users <= 5 and msgs >= 1000


def _top10_render(rows: list[dict], sort_key: str, caption: str) -> str:
    """Render one Top-N table. `sort_key` is 'messages' or 'users'. `caption` shown above table."""
    rows = sorted(rows, key=lambda x: -(x.get(sort_key) or 0))
    total_msgs = sum(r["messages"] for r in rows) or 1
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>#</th>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Tenant</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Messages (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>WoW msgs</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Users (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>WoW users</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Msg / user</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Cadence</th>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Dominant surface</th>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>GPT model</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Share of msgs</th></tr>")

    def _wow_cell(cur: int, prev: int) -> str:
        """Format a per-tenant WoW delta cell (7d vs prev 7d)."""
        if prev == 0 and cur > 0:
            return "<span style='color:#2e7d32;font-weight:600'>new</span>"
        if prev == 0:
            return "&ndash;"
        txt, col = wow(cur, prev)
        return f"<span style='color:{col};font-weight:600'>{txt}</span>"

    body = []
    for i, r in enumerate(rows, 1):
        tid = (r.get("Surface") or "").strip()
        label = KNOWN_TENANTS.get(tid.lower(), "")
        try:
            dom_payload = json.loads(r.get("Schema") or "[0,\"\"]")
            dom_surface = dom_payload[1] if len(dom_payload) > 1 else ""
        except Exception:
            dom_surface = ""
        users = r["users"]
        mpu = (r["messages"] / users) if users else 0
        share = (r["messages"] / total_msgs * 100)
        # Show the customer name only. Fall back to a masked id only when the
        # tenant is unknown so the row is never blank.
        tenant_cell = f"<b>{label}</b>" if label else f"<code>{short_id(tid)}</code>"
        # GPT model served to this tenant (from tenant-model-map.json).
        _mm = MODEL_MAP.get(tid.lower())
        _model_txt = friendly_model(_mm["model"]) if _mm and _mm.get("model") else "&ndash;"
        # Color-code by model family: GPT-4.1 light red, GPT-5 purple.
        if _model_txt.startswith("GPT-4.1"):
            model_cell = f"<span style='color:#e57373;font-weight:600'>{_model_txt}</span>"
        elif _model_txt.startswith("GPT-5"):
            model_cell = f"<span style='color:#8e24aa;font-weight:600'>{_model_txt}</span>"
        else:
            model_cell = _model_txt
        # Per-tenant WoW deltas (7d vs prev 7d) from tenant-wow.json.
        _wk = TENANT_WEEKLY.get(tid.lower(), {})
        msg_wow_cell  = _wow_cell(_wk.get("cur_msgs", 0),  _wk.get("prev_msgs", 0))  if _wk else "&ndash;"
        user_wow_cell = _wow_cell(_wk.get("cur_users", 0), _wk.get("prev_users", 0)) if _wk else "&ndash;"
        # Per-tenant cadence (avg active days/week per weekly-active user) from tenant-cadence.json.
        _cad = TENANT_CADENCE.get(tid.lower())
        cad_cell = f"{_cad:.1f} d/wk" if _cad else "&ndash;"
        body.append(
            f"<tr><td style='padding:6px 10px;border:1px solid #ddd'>{i}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{tenant_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{r['messages']:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{msg_wow_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{users:,}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{user_wow_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{mpu:,.1f}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{cad_cell}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{dom_surface}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{model_cell}</td>"
            f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{share:.1f}%</td></tr>"
        )
    table = ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
             "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #ddd'>"
             + head + "".join(body) + "</table>")
    return f"<h4 style='margin:14px 0 6px 0;color:#1f4e79'>{caption}</h4>" + table


def top10_table(all_b_rows: list[dict]) -> str:
    """Two Top-N tables (by messages, by users) -- with anonymous/empty tenants,
    bot/automation rows, AND known test/sandbox tenants filtered out so leaderboards
    reflect real customer adoption."""
    raw = [r for r in all_b_rows if r.get("Section") == "Top10"]
    if not raw:
        return ""
    # Filter: drop empty tenant IDs (anonymous / studio test-canvas traffic with no principalTenantId),
    # known test/sandbox tenant IDs (from the configured exclude_from_leaderboard list),
    # and bot/automation rows (huge msg counts but <=5 users).
    excluded = []
    keep = []
    for r in raw:
        tid = (r.get("Surface") or "").strip().lower()
        if not tid:
            excluded.append(("empty tenant id", r))
            continue
        if tid in EXCLUDE_FROM_LEADERBOARD:
            excluded.append(("test / sandbox", r))
            continue
        if _is_bot_automation(r):
            excluded.append(("bot/automation", r))
            continue
        keep.append(r)
    # Cap to top 10 by messages (orchestrator now emits Top 25 so we have headroom).
    keep = sorted(keep, key=lambda x: -(x.get("messages") or 0))[:10]
    # Stash exclusion summary so callers can print it once.
    excl_summary = ""
    if excluded:
        bits = []
        for reason, r in excluded:
            tid = (r.get("Surface") or "").strip() or "(empty)"
            label = KNOWN_TENANTS.get(tid.lower(), "")
            # Name only; fall back to a masked id when the tenant is unknown.
            tag = f"{label}" if label else f"<code>{short_id(tid)}</code>"
            bits.append(f"{tag} &mdash; {r['messages']:,} msgs / {r['users']} users ({reason})")
        excl_summary = (
            "<p style='font-size:12px;color:#666;margin:6px 0 10px 0'>"
            "<i>Excluded from leaderboards: " + "; ".join(bits) + ". "
            "Empty tenant id = events with no <code>principalTenantId</code> populated "
            "(anonymous / Copilot Studio web-test canvas / debug sessions). "
            "Test / sandbox = internal test/QA tenants from the configured exclusion list &mdash; not real customer adoption. "
            "Bot/automation = &lt;=5 distinct users but &gt;=1,000 messages (test harnesses, not real adoption).</i></p>"
        )
    by_msgs  = _top10_render(keep[:], "messages", "Sorted by messages (28d)")
    by_users = _top10_render(keep[:], "users",    "Sorted by distinct users (28d)")
    return by_msgs + by_users


def top_customers_list(all_b_rows: list[dict], n: int = 5) -> list[dict]:
    """Return the top-n real customers (same filtering as the leaderboard) with
    name, 28d messages, WoW msgs%, and served GPT model -- for the summary section."""
    raw = [r for r in all_b_rows if r.get("Section") == "Top10"]
    keep = []
    for r in raw:
        tid = (r.get("Surface") or "").strip().lower()
        if not tid or tid in EXCLUDE_FROM_LEADERBOARD or _is_bot_automation(r):
            continue
        keep.append(r)
    keep = sorted(keep, key=lambda x: -(x.get("messages") or 0))[:n]
    out = []
    for r in keep:
        tid = (r.get("Surface") or "").strip().lower()
        name = KNOWN_TENANTS.get(tid, short_id(tid))
        _wk = TENANT_WEEKLY.get(tid, {})
        cur, prev = _wk.get("cur_msgs", 0), _wk.get("prev_msgs", 0)
        wow_txt, wow_col = wow(cur, prev) if prev else ("new", "#2e7d32")
        _mm = MODEL_MAP.get(tid)
        model = friendly_model(_mm["model"]) if _mm and _mm.get("model") else None
        out.append({"name": name, "msgs": r.get("messages", 0),
                    "wow_txt": wow_txt, "wow_col": wow_col, "model": model})
    return out


def tenant_surface_table(all_b_rows: list[dict]) -> str:
    """TenantSurface rows: Surface, messages, conversations (=tenant count), users."""
    rows = sorted(
        [r for r in all_b_rows if r.get("Section") == "TenantSurface"],
        key=lambda x: -x["users"],
    )
    if not rows:
        return ""
    head = ("<tr style='background:#f3f3f3'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #ddd'>Surface</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #ddd'>Distinct tenants (28d)</th>"
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


def encode_png(p: Path, colors: int = 128) -> str:
    img = Image.open(p).convert("P", palette=Image.ADAPTIVE, colors=colors)
    small = p.with_name(p.stem + "-small.png")
    img.save(small, optimize=True)
    return base64.b64encode(small.read_bytes()).decode("ascii")


# --- region rollup rendering -------------------------------------------------

def region_table(rollup: dict) -> str:
    """Per-region 20d summary: tenants, users, messages, 7d msgs, avg DAU, % of msgs."""
    rows = [r for r in rollup.get("regions", []) if r.get("messages_20d", 0) > 0]
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -r["messages_20d"])
    total_msgs = sum(r["messages_20d"] for r in rows) or 1
    total_users = sum(r["users_20d"] for r in rows)
    total_tenants = sum(r["tenants_20d"] for r in rows)
    total_7d = sum(r["msgs_7d"] for r in rows)
    total_dau = sum(r.get("daily_avg_users") or 0 for r in rows)
    head = ("<tr style='background:#1f4e79;color:#fff'>"
            "<th style='text-align:left;padding:6px 10px;border:1px solid #1f4e79'>Region</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Tenants (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Users (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Messages (28d)</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Msgs 7d</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>Avg DAU</th>"
            "<th style='text-align:right;padding:6px 10px;border:1px solid #1f4e79'>% of msgs</th></tr>")
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
        f"<tr style='font-weight:700;background:#e8eef6'>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>Total ({len(rows)} regions)</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_tenants:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_users:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_msgs:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_7d:,}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>{total_dau:,.0f}</td>"
        f"<td style='text-align:right;padding:6px 10px;border:1px solid #ddd'>100.0%</td></tr>"
    )
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #1f4e79;width:100%;max-width:1100px'>"
            + head + "".join(body) + "</table>")


def region_top10_caveat(rollup: dict) -> str:
    """Annotate which regions each Top-10 tenant traffic came from."""
    top = rollup.get("top10_with_regions") or []
    if not top:
        return ""
    bits = []
    for r in top[:5]:  # short note
        tid = (r.get("Surface") or "").lower()
        label = KNOWN_TENANTS.get(tid, tid[:8])
        regs = r.get("_RegionMsgs") or {}
        if regs:
            dom = max(regs.items(), key=lambda kv: kv[1])
            bits.append(f"<b>{label}</b> in {dom[0]}")
    if not bits:
        return ""
    return ("<p style='font-size:12px;color:#666;margin:6px 0 0 0'>"
            "<i>Region of dominant traffic for top customers: " + " &middot; ".join(bits) + ".</i></p>")


def outlook_safe(html: str) -> str:
    """Make the HTML render reliably in Outlook's Word engine.

    Outlook (desktop) ignores CSS `background` on most elements and on table
    rows/headers, so colored callout boxes and header shading silently vanish.
    Word *does* honor the legacy `bgcolor` attribute on table/tr/td and the
    `background` on block elements when paired with bgcolor. This injects a
    matching `bgcolor="#hex"` onto any opening tag that carries an inline
    `background:#hex`, leaving everything else untouched.
    """
    tag_re = re.compile(r"<([a-zA-Z][\w-]*)\b([^>]*?background:\s*(#[0-9a-fA-F]{3,6})[^>]*)>")

    def _inject(m: re.Match) -> str:
        tag, attrs, color = m.group(1), m.group(2), m.group(3)
        if "bgcolor=" in attrs.lower():
            return m.group(0)
        return f"<{tag} bgcolor=\"{color}\"{attrs}>"

    return tag_re.sub(_inject, html)


def main():
    # ---- Load all v2 data sources --------------------------------------------------
    wf_rows  = load_rows(WF)
    ms_rows  = load_rows(MS)
    all_a    = load_rows(ALL_A)     # Windows + UserHealth
    all_b    = load_rows(ALL_B)     # Schema + TenantSurface + Top10
    all_daily = load_rows(ALL_DAILY)  # flat: Day,Surface,messages,conversations,users,tenants

    all_lookup = all_total_lookup(all_a)

    # ---- WoW computations -----------------------------------------------------------
    def scope_wow(rows):
        c = window_total(rows, "7d")
        p = window_total(rows, "prev7d")
        if not c or not p:
            return None
        m_pct, m_col = wow(c["messages"], p["messages"])
        u_pct, u_col = wow(c["users"], p["users"])
        return {
            "c": c, "p": p,
            "m_pct": m_pct, "m_col": m_col,
            "u_pct": u_pct, "u_col": u_col,
        }

    wf_w  = scope_wow(wf_rows)
    ms_w  = scope_wow(ms_rows)
    all_w = scope_wow(all_a)

    # ---- 28d MAU per scope ----------------------------------------------------------
    wf_mau  = (window_total(wf_rows,  "28d") or {}).get("users", 0)
    ms_mau  = (window_total(ms_rows,  "28d") or {}).get("users", 0)
    all_mau = (window_total(all_a,    "28d") or {}).get("users", 0)
    wf_msgs28  = (window_total(wf_rows,  "28d") or {}).get("messages", 0)
    ms_msgs28  = (window_total(ms_rows,  "28d") or {}).get("messages", 0)
    all_msgs28 = (window_total(all_a,    "28d") or {}).get("messages", 0)

    # ---- User health per scope ------------------------------------------------------
    wf_uh  = user_health(wf_rows)
    ms_uh  = user_health(ms_rows)
    all_uh = user_health(all_a)

    # ---- Average DAU per scope ------------------------------------------------------
    wf_dau  = avg_daily_users(wf_rows)
    ms_dau  = avg_daily_users(ms_rows)
    all_dau = avg_daily_users_flat(all_daily)

    # ---- Schemas ---------------------------------------------------------------------
    wf_schema_rows = [r for r in wf_rows if r.get("Section") == "Schema"]
    ms_schema_rows = [r for r in ms_rows if r.get("Section") == "Schema"]
    wf_schema = wf_schema_rows[0]["Schema"] if wf_schema_rows else "(unknown)"
    ms_schema = ms_schema_rows[0]["Schema"] if ms_schema_rows else "(unknown)"

    # ---- Charts (inline base64) -----------------------------------------------------
    img_trend   = encode_png(PNG_TREND,   colors=128)
    img_tenants = encode_png(PNG_TENANTS, colors=96)
    img_schema  = encode_png(PNG_SCHEMA,  colors=64)
    img_regions = encode_png(PNG_REGIONS, colors=64) if PNG_REGIONS.exists() else None

    # ---- Region rollup ---------------------------------------------------------------
    region_data = json.loads(REGION_ROLLUP.read_text(encoding="utf-8")) if REGION_ROLLUP.exists() else {"regions": []}
    region_rows = [r for r in region_data.get("regions", []) if r.get("messages_20d", 0) > 0]
    tbl_regions = region_table(region_data) if region_rows else ""
    regions_with_traffic = len(region_rows)
    distinct_tenants_total = sum(r["tenants_20d"] for r in region_rows) if region_rows else 0
    us_row = next((r for r in region_rows if r["region"] == "US"), None)
    us_msg_share = (us_row["messages_20d"] / max(sum(r["messages_20d"] for r in region_rows), 1) * 100.0) if us_row else 0.0
    us_tenant_share = (us_row["tenants_20d"] / max(distinct_tenants_total, 1) * 100.0) if us_row else 0.0
    nonus_tenants = distinct_tenants_total - (us_row["tenants_20d"] if us_row else 0)
    nonus_msgs = sum(r["messages_20d"] for r in region_rows) - (us_row["messages_20d"] if us_row else 0)
    nonus_msg_share = 100.0 - us_msg_share

    # ---- New / spiking customers this week vs last week (per-tenant 7d vs prev7d) -----
    # Reads tenant-wow.json (produced by the WoW fan-out). Falls back gracefully so the
    # email still builds if the file is missing.
    _twow = ROOT / "tenant-wow.json"
    tenant_growth_line = (
        "<b>Customer momentum:</b> week-over-week per-customer movement is being refreshed "
        "and will appear here next run."
    )
    new_cust_named: list[str] = []   # non-Contoso new-customer names (for the What's-new section)
    new_cust_count = 0
    if _twow.exists():
        try:
            _tw = json.loads(_twow.read_text(encoding="utf-8"))
            _new = int(_tw.get("new_tenants", 0))
            _spike = int(_tw.get("spiking_tenants", 0))
            _names = _tw.get("new_tenant_names", []) or []
            _contoso_n = sum(1 for n in _names if n.strip().lower() == "contoso")
            _named = [n for n in _names if n.strip().lower() != "contoso"]
            new_cust_named = _named
            new_cust_count = _new
            _name_html = ", ".join(
                f"<span style='color:#6a1b9a;font-weight:600'>{n}</span>" for n in _named)
            _name_bit = (" (" + _name_html + ")") if _named else ""
            _contoso_note = (
                f" <span style='color:#e67e00;font-weight:600'>Note: {_contoso_n} of the {_new} were &ldquo;Contoso&rdquo; internal test tenants.</span>"
                if _contoso_n else "")
            _new_cust = "customer" if _new == 1 else "customers"
            _was = "was" if _new == 1 else "were"
            _exist_cust = "customer" if _spike == 1 else "customers"
            tenant_growth_line = (
                f"<b>Customer momentum:</b> <b style='color:#2e7d32'>{_new} new {_new_cust}</b> "
                f"started using ESS this week who {_was} not active last week{_name_bit} "
                f"<span style='color:#2e7d32;font-weight:600'>&mdash; a positive growth signal</span>, and "
                f"<b style='color:#1565c0'>{_spike} existing {_exist_cust} grew usage by 50%+ "
                f"week over week (by messages)</b>.{_contoso_note}"
            )
        except Exception:
            pass

    # ---- Tables (kept: schema, top10, tenants-per-surface; per-scope windows tables dropped) -
    tbl_schema   = schema_table(all_b)
    tbl_top10    = top10_table(all_b)
    tbl_surface  = tenant_surface_table(all_b)

    # ---- Surface-mix % per scope (20d) for observations ------------------------------
    def surface_mix_28d(rows):
        m = {s: 0 for s in SURFACES}
        total = 0
        for r in rows:
            if r.get("Section") == "Windows" and r.get("Window") == "28d" and r.get("Surface") in SURFACES:
                m[r["Surface"]] = r["messages"]
                total += r["messages"]
        if not total:
            return {s: 0.0 for s in SURFACES}
        return {s: m[s] / total * 100.0 for s in SURFACES}

    wf_mix = surface_mix_28d(wf_rows)
    ms_mix = surface_mix_28d(ms_rows)
    all_mix = surface_mix_28d(all_a)

    # ---- Share of ALL by scope (7d and 28d) ------------------------------------------
    def share_of_all(scope_total_msgs, all_total_msgs):
        return (scope_total_msgs / all_total_msgs * 100.0) if all_total_msgs else 0.0

    all_msgs7  = (window_total(all_a, "7d") or {}).get("messages", 0)
    wf_msgs7   = (window_total(wf_rows, "7d") or {}).get("messages", 0)
    ms_msgs7   = (window_total(ms_rows, "7d") or {}).get("messages", 0)
    wf_share7  = share_of_all(wf_msgs7, all_msgs7)
    ms_share7  = share_of_all(ms_msgs7, all_msgs7)
    wf_share28 = share_of_all(wf_msgs28, all_msgs28)
    ms_share28 = share_of_all(ms_msgs28, all_msgs28)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ---- KPI summary table (one row per scope, uniform columns) --------------------
    def kpi_row(label, msgs28, mau, w, uh, accent):
        c_msgs7 = w["c"]["messages"] if w else 0
        p_msgs7 = w["p"]["messages"] if w else 0
        c_usrs7 = w["c"]["users"]    if w else 0
        p_usrs7 = w["p"]["users"]    if w else 0
        m_pct, m_col = (w["m_pct"], w["m_col"]) if w else ("n/a", "#666")
        u_pct, u_col = (w["u_pct"], w["u_col"]) if w else ("n/a", "#666")
        wau   = uh["wau"] if uh else 0
        retp  = uh["returning_pct"] if uh else 0.0
        stick = (wau / mau * 100.0) if mau else 0.0
        cell  = "padding:8px 12px;border:1px solid #ddd;text-align:right"
        cellL = "padding:8px 12px;border:1px solid #ddd;text-align:left"
        return (
            f"<tr>"
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
            f"</tr>"
        )

    kpi_head_cells = [
        ("Scope",          "left"),
        ("28d msgs",       "right"),
        ("28d MAU",        "right"),
        ("7d msgs",        "right"),
        ("WoW msgs",       "right"),
        ("7d users",       "right"),
        ("WoW users",      "right"),
        ("WAU",            "right"),
        ("WAU/MAU",        "right"),
        ("Returning %",    "right"),
    ]
    kpi_head = "<tr style='background:#1f4e79;color:#fff'>" + "".join(
        f"<th style='text-align:{a};padding:8px 12px;border:1px solid #1f4e79;font-weight:600;font-size:12px'>{n}</th>"
        for n, a in kpi_head_cells
    ) + "</tr>"

    kpi_table = (
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;border:1px solid #1f4e79;width:100%;max-width:1100px'>"
        + kpi_head
        + kpi_row("All ESS",     all_msgs28, all_mau, all_w, all_uh, "#1f4e79")
        + kpi_row(FLAGSHIP_A,     wf_msgs28,  wf_mau,  wf_w,  wf_uh,  "#c0504d")
        + kpi_row(FLAGSHIP_B,     ms_msgs28,  ms_mau,  ms_w,  ms_uh,  "#4f81bd")
        + "</table>"
    )

    kpi_legend = (
        "<p style='font-size:12px;color:#666;margin:6px 0 0 0'>"
        "<b>WoW</b> = current 7 days vs previous 7 days. "
        "<b>WAU/MAU</b> = stickiness (weekly active as % of monthly active). "
        "<b>Returning %</b> = share of this week's users who were also active in the prior week. "
        "Green = up, red = down. <i>Week-over-week (WoW) is the reliable trend metric today; ESS bot telemetry retains ~30 days of history, so longer-range quarterly metrics are not yet available.</i>"
        "</p>"
    )


    # User-health cards
    uh_wf  = stickiness_block(FLAGSHIP_A, wf_uh["wau"],  wf_mau,  wf_dau,
                               wf_uh["returning"],  wf_uh["returning_pct"])  if wf_uh  else ""
    uh_ms  = stickiness_block(FLAGSHIP_B,  ms_uh["wau"],  ms_mau,  ms_dau,
                               ms_uh["returning"],  ms_uh["returning_pct"])  if ms_uh  else ""
    uh_all = stickiness_block("All ESS",    all_uh["wau"], all_mau, all_dau,
                               all_uh["returning"], all_uh["returning_pct"]) if all_uh else ""

    # ---- WoW magnitudes for the headline callout (kept in sync with the KPI table) ---
    def _wow_pct(w):
        if w and w["p"].get("messages"):
            return (w["c"]["messages"] - w["p"]["messages"]) / w["p"]["messages"] * 100.0
        return 0.0
    wf_wow_mag = abs(_wow_pct(wf_w))      # Flagship A WoW message change (magnitude)
    wf_wow_pct = _wow_pct(wf_w)           # Flagship A WoW message change (signed)
    ms_wow_pct = _wow_pct(ms_w)           # Flagship B WoW message change (signed)
    ms_wow_sign = "+" if ms_wow_pct >= 0 else "&minus;"
    wf_c_msgs = wf_w["c"]["messages"] if wf_w else 0
    wf_p_msgs = wf_w["p"]["messages"] if wf_w else 0
    wf_c_usrs = wf_w["c"]["users"] if wf_w else 0
    wf_p_usrs = wf_w["p"]["users"] if wf_w else 0

    # ---- "What's new this week" summary -------------------------------------------
    # Top customers w/ WoW trend, new customer names, and the model mix across customers.
    _topc = top_customers_list(all_b, 5)
    top_cust_html = ", ".join(
        f"<b>{c['name']}</b> (<span style='color:{c['wow_col']};font-weight:600'>{c['wow_txt']}</span>)"
        for c in _topc
    ) if _topc else "&mdash;"

    if new_cust_named:
        _nc = ", ".join(f"<span style='color:#6a1b9a;font-weight:600'>{n}</span>" for n in new_cust_named)
        new_cust_html = f"{_nc}"
        if new_cust_count > len(new_cust_named):
            new_cust_html += f" (+{new_cust_count - len(new_cust_named)} internal test tenants)"
    else:
        new_cust_html = "none new this week"

    # Model mix across all customers with a known served model.
    _model_counts: dict[str, int] = {}
    for _v in MODEL_MAP.values():
        _m = friendly_model((_v or {}).get("model"))
        if _m:
            _model_counts[_m] = _model_counts.get(_m, 0) + 1
    _model_color = {
        "GPT-4.1": "#e57373",
        "GPT-5 chat": "#8e24aa",
        "GPT-4.1 mini": "#e57373",
    }
    def _mcol(m):
        if m.startswith("GPT-4.1"): return "#e57373"
        if m.startswith("GPT-5"):   return "#8e24aa"
        return "#444"
    model_mix_html = ", ".join(
        f"<span style='color:{_mcol(m)};font-weight:600'>{m}</span> ({n})"
        for m, n in sorted(_model_counts.items(), key=lambda x: -x[1])
    ) if _model_counts else "&mdash;"

    # ---- Leadership model-adoption summary (call-weighted share + adopter counts) ---
    # Classify by raw model_id so newer "internal preview" labels (e.g. GPT-5.x) are
    # counted in the right family even before they get a friendly display name.
    def _fam(mid: str) -> str:
        mid = (mid or "").lower()
        if mid.startswith("gpt-41"):
            return "4.1"
        if mid.startswith("gpt-5"):
            return "5"
        if mid.startswith("gpt-35"):
            return "3.5"
        return "other"
    _tot_calls = _g41_calls = _g5_calls = 0.0
    _n_dom5 = _n_any5 = 0
    for _v in MODEL_MAP.values():
        _mid = (_v or {}).get("model_id")
        if not _mid:
            continue
        if _fam(_mid) == "5":
            _n_dom5 += 1
        _c = float((_v or {}).get("calls") or 0)
        _has5 = False
        for _m_id, _sh in (_v or {}).get("mix", []):
            _f = _fam(_m_id)
            _tot_calls += _c * _sh
            if _f == "4.1":
                _g41_calls += _c * _sh
            elif _f == "5":
                _g5_calls += _c * _sh
                if _sh > 0:
                    _has5 = True
        if _has5:
            _n_any5 += 1
    _g41_share = (100.0 * _g41_calls / _tot_calls) if _tot_calls else 0.0
    _g5_share = (100.0 * _g5_calls / _tot_calls) if _tot_calls else 0.0
    model_headline_html = (
        f"GPT-4.1 remains the default, serving <b>~{_g41_share:.0f}% of all model calls</b>. "
        f"<b>{_n_dom5}</b> customers now run a "
        f"<span style='color:#8e24aa;font-weight:600'>GPT-5</span>-class model as their primary and "
        f"<b>{_n_any5}</b> have GPT-5 in their mix (~{_g5_share:.0f}% of calls)."
    )

    whats_new = f"""
<div style="margin:10px 0 4px 0;padding:12px 16px;background:#f4f8fc;border:1px solid #d6e4f0;border-radius:6px">
<p style="margin:0 0 8px 0;font-weight:700;color:#1f4e79;font-size:15px">&#128226; What&rsquo;s new this week</p>
<ul style="margin:0 0 0 18px;padding:0">
<li style="margin-bottom:5px"><b>Top customers &amp; trend (WoW msgs):</b> {top_cust_html}.</li>
<li style="margin-bottom:5px"><b>New customers:</b> {new_cust_html}.</li>
<li style="margin-bottom:5px"><b>Models customers are running:</b> {model_mix_html}.</li>
</ul>
</div>"""

    body = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;margin:0;padding:0">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="1100" style="width:1100px;max-width:1100px;border-collapse:collapse">
<tr><td style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">
<p>Hi everyone,</p>
<p>Here&rsquo;s this week&rsquo;s <b>ESS Adoption &amp; Usage report</b> &mdash; one read on how ESS is
being adopted and used across all our customers. If this email was forwarded to you and you&rsquo;d like to be
included going forward, please join
<a href="https://idwebelements.microsoft.com/GroupManagement.aspx?Group=essadoption&amp;Operation=join">ESS Adoption Reports</a>.</p>

{whats_new}

<p style="margin:10px 0 14px 0;padding:8px 14px;background:#fffce6;border-left:4px solid #f0c000;border-radius:4px">
&#128172; <b>We&rsquo;d love your input:</b> what data would make this report more useful to you?
Reply with the metrics, cuts, or customers you&rsquo;d like to see and we&rsquo;ll fold them into future editions.</p>

<h3 style="color:#1f4e79;margin:18px 0 8px">Headline</h3>
{kpi_table}

<ul style="margin:14px 0 12px 22px;padding:0">
<li style="margin-bottom:8px;padding:8px 12px;background:#e8f5e9;border-left:4px solid #2e7d32;border-radius:4px;list-style:none;margin-left:-22px"><b style="color:#1b5e20">{FLAGSHIP_A} moved {'+' if wf_wow_pct >= 0 else '&minus;'}{abs(wf_wow_pct):.0f}% week-over-week.</b> Weekly active users went from ~{wf_p_usrs:,} to ~{wf_c_usrs:,} and messages from ~{wf_p_msgs:,} to ~{wf_c_msgs:,}. {FLAGSHIP_B} changed <b>{ms_wow_sign}{abs(ms_wow_pct):.0f}%</b> over the same period. <i>Tip: replace this highlight with your own week-specific narrative.</i></li>
<li>{tenant_growth_line}</li>
<li><b>~{all_msgs28:,} employee messages and ~{all_mau:,} monthly active users across ~{distinct_tenants_total:,} customers in the last 28 days</b> &mdash; spanning <b>{regions_with_traffic} geographies</b> (Americas, Europe and rest of world). ESS is live and at scale globally.</li>
<li><b>A small number of customers drive most of the usage.</b> {FLAGSHIP_B} and {FLAGSHIP_A} together account for a large share of the last 28 days of messages; a long tail of ~{max(distinct_tenants_total - 2, 100)}+ smaller customers makes up the rest.</li>
<li><b>Microsoft 365 Copilot is the most popular way employees reach ESS.</b> Embedded web experiences and Teams / other channels make up the remainder &mdash; see the surface footprint table below for the exact split.</li>
<li><b>Models in use:</b> {model_headline_html}</li>
</ul>

<h2 style="color:#1f4e79;margin-top:24px">Daily trend</h2>
<img src="data:image/png;base64,{img_trend}" alt="ESS usage by surface (left) and DAU (right) - 28d" style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Top customers</h2>
{tbl_top10}

<h2 style="color:#1f4e79;margin-top:24px">Active Tenants and Users</h2>
<h3 style="margin:14px 0 6px;color:#333">Daily active tenants &amp; users</h3>
<img src="data:image/png;base64,{img_tenants}" alt="Daily active tenants and users per day" style="max-width:1100px;width:100%;height:auto;border:1px solid #ddd"/>

<h3 style="margin:18px 0 6px;color:#333">Tenants per ESS agent (cumulative 28d)</h3>
<img src="data:image/png;base64,{img_schema}" alt="Tenants by ESS schema" style="max-width:800px;width:100%;height:auto;border:1px solid #ddd"/>

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h3 style="margin:18px 0 6px">ESS agents</h3>
{tbl_schema}

<h3 style="margin:18px 0 6px">Surface footprint</h3>
{tbl_surface}

<hr style="margin:28px 0;border:none;border-top:2px solid #1f4e79">
<h2 style="color:#1f4e79;margin-bottom:6px">Regional footprint</h2>
{tbl_regions}
{"<img src='data:image/png;base64," + img_regions + "' alt='ESS message volume by region (28d)' style='max-width:1100px;width:100%;height:auto;border:1px solid #ddd;margin:8px 0 14px 0'/>" if img_regions else ""}

<p>Thanks,<br/>ESS Team</p>
</td></tr>
</table>
</body></html>"""

    body = outlook_safe(body)
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
