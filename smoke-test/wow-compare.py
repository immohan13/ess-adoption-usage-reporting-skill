import json
from pathlib import Path

src = Path(__file__).resolve().parent / "smoke-out" / "per-cluster-raw"
daily = []
for f in src.glob("*-daily.json"):
    doc = json.loads(f.read_text(encoding="utf-8"))
    if isinstance(doc, list):
        daily.extend(doc)
    elif isinstance(doc, dict) and doc.get("Tables"):
        t = doc["Tables"][0]
        cols = [c["ColumnName"] for c in t["Columns"]]
        for r in t["Rows"]:
            daily.append(dict(zip(cols, r)))

by_day = {}
for r in daily:
    day = r["Day"][:10]
    b = by_day.setdefault(day, {"messages": 0, "users": 0})
    b["messages"] += int(r.get("messages") or 0)
    b["users"]    += int(r.get("users")    or 0)

days = sorted(by_day.keys())
newest = days[-1]
print(f"Newest day in cache: {newest}")
print(f"Total days in cache: {len(days)}  ({days[0]} .. {days[-1]})")
print()
print(f"{'Day':<12} {'msgs':>10} {'users':>8}")
for d in days[-18:]:
    print(f"{d:<12} {by_day[d]['messages']:>10,} {by_day[d]['users']:>8,}")
print()

# Simulate the OLD math: ago(7d) = exactly 7*24h from "now", where "now" is when
# the email was generated. Cache was written around 2026-06-01 16:40 UTC.
# So old current 7d = last 7 days INCLUDING a partial today (~70% of day).
# Old prev 7d = days 8..14 ago.
#
# Simulate the NEW math: full UTC days, today excluded.
# Current 7d = the 7 complete days BEFORE today (i.e. days [newest-7..newest-1]).
# Prev 7d    = the 7 complete days BEFORE that.

if newest >= days[-8]:
    # OLD: current = last 7 days INCLUDING the boundary "today"
    # We don't have sub-day data, so simulate "partial today" as 70% of the value.
    old_today_msgs  = int(by_day[newest]["messages"] * 0.70)
    old_today_users = int(by_day[newest]["users"]    * 0.70)
    old_curr_msgs  = sum(by_day[d]["messages"] for d in days[-7:-1]) + old_today_msgs
    old_prev_msgs  = sum(by_day[d]["messages"] for d in days[-14:-7])
    old_curr_users = sum(by_day[d]["users"]    for d in days[-7:-1]) + old_today_users
    old_prev_users = sum(by_day[d]["users"]    for d in days[-14:-7])

    # NEW: current = 7 full days ENDING yesterday; prev = 7 full days before that
    new_curr_msgs  = sum(by_day[d]["messages"] for d in days[-8:-1])
    new_prev_msgs  = sum(by_day[d]["messages"] for d in days[-15:-8])
    new_curr_users = sum(by_day[d]["users"]    for d in days[-8:-1])
    new_prev_users = sum(by_day[d]["users"]    for d in days[-15:-8])

    print("Window content:")
    print(f"  OLD current = {days[-7]} .. {days[-2]} + ~70% of {newest}")
    print(f"  OLD prev    = {days[-14]} .. {days[-8]}")
    print(f"  NEW current = {days[-8]} .. {days[-2]}  (7 complete days, today excluded)")
    print(f"  NEW prev    = {days[-15]} .. {days[-9]}")
    print()
    print("Messages:")
    print(f"  OLD : curr={old_curr_msgs:,}  prev={old_prev_msgs:,}  WoW={((old_curr_msgs-old_prev_msgs)/old_prev_msgs*100):+.1f}%")
    print(f"  NEW : curr={new_curr_msgs:,}  prev={new_prev_msgs:,}  WoW={((new_curr_msgs-new_prev_msgs)/new_prev_msgs*100):+.1f}%")
    print()
    print("Users (sum across days, not dcount -- shape only):")
    print(f"  OLD : curr={old_curr_users:,}  prev={old_prev_users:,}  WoW={((old_curr_users-old_prev_users)/old_prev_users*100):+.1f}%")
    print(f"  NEW : curr={new_curr_users:,}  prev={new_prev_users:,}  WoW={((new_curr_users-new_prev_users)/new_prev_users*100):+.1f}%")
    print()
    print(f"Newest-day raw value: {by_day[newest]['messages']:,} msgs, {by_day[newest]['users']:,} users")
    print(f"  -> the 'partial today' clipping costs ~{by_day[newest]['messages']*0.3:,.0f} msgs")
    print(f"  -> as a share of current 7d window: {by_day[newest]['messages']*0.3/old_curr_msgs*100:.1f}%")
