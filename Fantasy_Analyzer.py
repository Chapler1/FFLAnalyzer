#!/usr/bin/env python3
"""Fantasy Football Rivalry Analyzer"""

import os
import sys
import json
import argparse
import statistics
from datetime import date
from collections import defaultdict
from dotenv import load_dotenv
from espn_api.football import League

load_dotenv()

ESPN_S2    = os.getenv("ESPN_S2")
SWID       = os.getenv("SWID")
LEAGUE_ID  = int(os.getenv("LEAGUE_ID",  "182413"))
START_YEAR = int(os.getenv("START_YEAR", "2018"))
END_YEAR   = int(os.getenv("END_YEAR",   "2025"))

# Same-person accounts with different last names (old -> canonical)
MANUAL_ALIASES = {
    "Colin Arber": "Anthony Belardinelli",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_owner(team):
    try:
        o = team.owners[0]
        name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
        return name.title() if name else team.team_name
    except (AttributeError, IndexError, TypeError):
        return team.team_name


def resolve(name, aliases):
    return aliases.get(name, name)


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_seasons():
    print("Loading seasons...")
    seasons = {}
    for year in range(START_YEAR, END_YEAR + 1):
        try:
            league = League(league_id=LEAGUE_ID, year=year, espn_s2=ESPN_S2, swid=SWID)
            seasons[year] = league
            print(f"  + {year}")
        except Exception as e:
            print(f"  - {year}: {e}")
    return seasons


def detect_aliases(seasons):
    """Merge owners with same last name whose active years never overlap (same person, two accounts)."""
    owner_years = defaultdict(set)
    for year, league in seasons.items():
        for team in league.teams:
            owner_years[get_owner(team)].add(year)

    last_name_map = defaultdict(list)
    for owner in owner_years:
        parts = owner.split()
        last = parts[-1].lower() if parts else owner.lower()
        last_name_map[last].append(owner)

    aliases = {}
    for last, owners in last_name_map.items():
        if len(owners) < 2:
            continue
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                a, b = owners[i], owners[j]
                if not (owner_years[a] & owner_years[b]):
                    canonical = max([a, b], key=lambda o: (max(owner_years[o]), len(owner_years[o])))
                    old = b if canonical == a else a
                    aliases[old] = canonical
                    print(f"  Alias: '{old}' -> '{canonical}' "
                          f"(years {sorted(owner_years[old])} merged into {sorted(owner_years[canonical])})")

    for old, canonical in MANUAL_ALIASES.items():
        if old not in owner_years or canonical not in owner_years:
            print(f"  Manual alias skipped (name not found): '{old}' -> '{canonical}'")
            continue
        if owner_years[old] & owner_years[canonical]:
            print(f"  Manual alias skipped (overlapping years): '{old}' -> '{canonical}'")
            continue
        aliases[old] = canonical
        print(f"  Manual alias: '{old}' -> '{canonical}' "
              f"(years {sorted(owner_years[old])} merged into {sorted(owner_years[canonical])})")

    return aliases


# ── Data building ─────────────────────────────────────────────────────────────

def build_matchups(seasons, aliases):
    matchups = []
    seen = set()

    for year, league in seasons.items():
        reg_weeks = league.settings.reg_season_count

        for team in league.teams:
            for i, opponent in enumerate(team.schedule):
                if not hasattr(opponent, "team_id"):
                    continue

                key = (year, i, min(team.team_id, opponent.team_id), max(team.team_id, opponent.team_id))
                if key in seen:
                    continue
                seen.add(key)

                score_a = team.scores[i]     if i < len(team.scores)     else 0.0
                score_b = opponent.scores[i] if i < len(opponent.scores) else 0.0

                if score_a == 0 and score_b == 0:
                    continue

                week    = i + 1
                owner_a = resolve(get_owner(team),     aliases)
                owner_b = resolve(get_owner(opponent), aliases)

                matchups.append({
                    "year":       year,
                    "week":       week,
                    "is_playoff": week > reg_weeks,
                    "team_a":     owner_a,
                    "team_b":     owner_b,
                    "score_a":    score_a,
                    "score_b":    score_b,
                    "winner":     owner_a if score_a > score_b else owner_b if score_b > score_a else None,
                    "margin":     abs(score_a - score_b),
                })

    return matchups


def build_h2h(matchups):
    records = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "pa": 0.0})

    for m in matchups:
        a, b = m["team_a"], m["team_b"]
        if m["winner"] == a:
            records[(a, b)]["wins"]   += 1
            records[(b, a)]["losses"] += 1
        elif m["winner"] == b:
            records[(a, b)]["losses"] += 1
            records[(b, a)]["wins"]   += 1
        else:
            records[(a, b)]["ties"] += 1
            records[(b, a)]["ties"] += 1

        records[(a, b)]["pf"] += m["score_a"]
        records[(a, b)]["pa"] += m["score_b"]
        records[(b, a)]["pf"] += m["score_b"]
        records[(b, a)]["pa"] += m["score_a"]

    return records


# Which lineup slots a player at each position can fill
POSITION_SLOTS = {
    'QB':  {'QB', 'OP'},
    'RB':  {'RB', 'FLEX', 'RB/WR', 'RB/WR/TE', 'OP'},
    'WR':  {'WR', 'FLEX', 'WR/TE', 'RB/WR', 'RB/WR/TE', 'OP'},
    'TE':  {'TE', 'FLEX', 'WR/TE', 'RB/WR/TE', 'OP'},
    'K':   {'K'},
    'D/ST': {'D/ST'},
}

# Which player positions can fill each lineup slot
SLOT_ELIGIBLE = {
    'QB':       {'QB'},
    'RB':       {'RB'},
    'WR':       {'WR'},
    'TE':       {'TE'},
    'K':        {'K'},
    'D/ST':     {'D/ST'},
    'FLEX':     {'RB', 'WR', 'TE'},
    'RB/WR':    {'RB', 'WR'},
    'WR/TE':    {'WR', 'TE'},
    'RB/WR/TE': {'RB', 'WR', 'TE'},
    'OP':       {'QB', 'RB', 'WR', 'TE'},
}


def compute_optimal(lineup):
    """Greedy optimal lineup using position-based slot matching."""
    active = [p for p in lineup if p.slot_position not in ('IR',)]

    from collections import Counter
    slot_counts = Counter(p.slot_position for p in active if p.slot_position not in ('BE', 'IR'))
    slots = [s for s, n in slot_counts.items() for _ in range(n)]

    def flexibility(s):
        eligible = SLOT_ELIGIBLE.get(s, {s})
        return sum(1 for p in active if getattr(p, 'position', '') in eligible)

    slots_sorted = sorted(slots, key=flexibility)

    used  = set()
    total = 0.0
    for slot in slots_sorted:
        eligible = SLOT_ELIGIBLE.get(slot, {slot})
        best = max(
            (p for p in active
             if id(p) not in used and getattr(p, 'position', '') in eligible),
            key=lambda p: p.points,
            default=None,
        )
        if best:
            used.add(id(best))
            total += best.points
    return total


def build_lineup_data(seasons, aliases):
    print("\nFetching lineup/bench data (2019+)...")

    owner_stats       = defaultdict(lambda: {'starter_pts': 0.0, 'optimal_pts': 0.0, 'games': 0, 'blown_wins': 0})
    blunders          = []
    player_weeks      = defaultdict(lambda: defaultdict(int))
    player_seasons    = defaultdict(lambda: defaultdict(set))
    player_season_pts = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    player_pid        = {}
    final_rosters     = defaultdict(dict)

    for year, league in sorted(seasons.items()):
        if year < 2019:
            print(f"  {year}: skipped (box scores unavailable before 2019)")
            continue
        reg_weeks = league.settings.reg_season_count
        print(f"  {year}: ", end='', flush=True)

        for week in range(1, reg_weeks + 1):
            try:
                boxes = league.box_scores(week)
            except Exception:
                continue

            for box in boxes:
                if not box.home_team or not box.away_team:
                    continue
                for team, lineup, opp_score, opp_team in [
                    (box.home_team, box.home_lineup, box.away_score, box.away_team),
                    (box.away_team, box.away_lineup, box.home_score, box.home_team),
                ]:
                    if not lineup:
                        continue
                    owner = resolve(get_owner(team),     aliases)
                    opp   = resolve(get_owner(opp_team), aliases)

                    actual  = sum(p.points for p in lineup if p.slot_position not in ('BE', 'IR'))
                    optimal = compute_optimal(lineup)
                    left    = max(0.0, optimal - actual)

                    owner_stats[owner]['starter_pts'] += actual
                    owner_stats[owner]['optimal_pts'] += optimal
                    owner_stats[owner]['games']       += 1

                    for p in lineup:
                        name = getattr(p, 'name', None)
                        if p.slot_position == 'IR' or not name or name == 'None':
                            continue
                        player_weeks[owner][name]   += 1
                        player_seasons[owner][name].add(year)
                        player_season_pts[owner][year][name] += p.points
                        pid = getattr(p, 'playerId', 0)
                        if pid and pid > 0:
                            player_pid[name] = pid

                    # Snapshot final regular-season roster (last week only)
                    if week == reg_weeks and str(year) not in final_rosters[owner]:
                        sp_map = player_season_pts[owner][year]
                        final_rosters[owner][str(year)] = sorted(
                            [{'name': getattr(p,'name','?'), 'pos': getattr(p,'position','?'),
                              'slot': p.slot_position, 'pts': round(p.points, 2),
                              'season_pts': round(sp_map.get(getattr(p,'name','?'), 0.0), 2),
                              'pid': getattr(p, 'playerId', 0)}
                             for p in lineup
                             if getattr(p,'name',None) and getattr(p,'name','') != 'None'
                             and p.slot_position != 'IR'],
                            key=lambda x: (x['slot'] in ('BE',), x['slot'], -x['pts'])
                        )

                    would_win = opp_score > 0 and actual < opp_score and optimal > opp_score
                    if would_win:
                        owner_stats[owner]['blown_wins'] += 1

                    # Find the single worst individual benching decision this game
                    benched  = sorted([p for p in lineup if p.slot_position == 'BE'
                                       and getattr(p, 'name', None)],
                                      key=lambda p: p.points, reverse=True)
                    starters = [p for p in lineup if p.slot_position not in ('BE', 'IR')]
                    best_swap, best_gain = None, 0.0
                    for bp in benched:
                        eligible_slots = POSITION_SLOTS.get(getattr(bp, 'position', ''), set())
                        for sp in starters:
                            if sp.slot_position in eligible_slots:
                                gain = bp.points - sp.points
                                if gain > best_gain:
                                    best_gain = gain
                                    best_swap = {
                                        'benched':      bp.name,
                                        'started':      sp.name,
                                        'benched_pid':  getattr(bp, 'playerId', 0),
                                        'started_pid':  getattr(sp, 'playerId', 0),
                                        'benched_pts':  round(bp.points, 2),
                                        'started_pts':  round(sp.points, 2),
                                        'swap_gain':    round(gain,      2),
                                    }

                    if best_swap:
                        blunders.append({
                            'year': year, 'week': week,
                            'owner': owner, 'opp': opp,
                            'actual':    round(actual,    2),
                            'optimal':   round(optimal,   2),
                            'left':      round(left,      2),
                            'opp_score': round(opp_score, 2),
                            'would_win': would_win,
                            **best_swap,
                        })
            print('.', end='', flush=True)
        print()

    final_stats = {}
    for owner, s in owner_stats.items():
        g = s['games'] or 1
        final_stats[owner] = {
            'games':       s['games'],
            'starter_pts': round(s['starter_pts'], 2),
            'optimal_pts': round(s['optimal_pts'], 2),
            'pts_left':    round(s['optimal_pts'] - s['starter_pts'], 2),
            'avg_left':    round((s['optimal_pts'] - s['starter_pts']) / g, 2),
            'blown_wins':  s['blown_wins'],
        }

    loyalty = {}
    for owner in player_weeks:
        players = [
            {'name': name, 'weeks': weeks,
             'seasons': len(player_seasons[owner].get(name, set())),
             'pid': player_pid.get(name, 0)}
            for name, weeks in player_weeks[owner].items() if weeks >= 4
        ]
        loyalty[owner] = sorted(players, key=lambda x: x['weeks'], reverse=True)[:15]

    blunders.sort(key=lambda x: x['left'], reverse=True)
    return {
        'owner_stats':   final_stats,
        'blunders':      blunders[:300],
        'loyalty':       loyalty,
        'final_rosters': {owner: dict(years) for owner, years in final_rosters.items()},
    }


def build_points_history(seasons, aliases):
    history = defaultdict(dict)
    for year, league in seasons.items():
        ranked = sorted(league.teams, key=lambda t: t.points_for, reverse=True)
        for rank, team in enumerate(ranked, 1):
            owner = resolve(get_owner(team), aliases)
            history[owner][year] = {
                "pf":       team.points_for,
                "pa":       team.points_against,
                "wins":     team.wins,
                "losses":   team.losses,
                "standing": team.standing,
                "pts_rank": rank,
            }
    return history


def find_champions(seasons, aliases):
    """Find the actual champion each year from the WINNERS_BRACKET final game."""
    champions = {}
    for year, league in seasons.items():
        reg_weeks = league.settings.reg_season_count
        total_weeks = max(len(t.schedule) for t in league.teams) if league.teams else reg_weeks

        for week in range(total_weeks, reg_weeks, -1):
            try:
                boxes = league.box_scores(week)
                wb = [b for b in boxes
                      if getattr(b, "matchup_type", "") == "WINNERS_BRACKET"
                      and b.home_team and b.away_team
                      and (b.home_score > 0 or b.away_score > 0)]

                if len(wb) == 1:  # exactly one WINNERS_BRACKET game = championship
                    b = wb[0]
                    winner = b.home_team if b.home_score > b.away_score else b.away_team
                    champ = resolve(get_owner(winner), aliases)
                    champions[year] = champ
                    print(f"  {year} champion: {champ}")
                    break
            except Exception:
                pass

    return champions


# ── CLI display ───────────────────────────────────────────────────────────────

def hr(width=72):
    print("-" * width)


def section(title):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def _game_row(m):
    flip  = m["winner"] != m["team_a"] and m["winner"] is not None
    w     = m["winner"] or "TIE"
    l     = m["team_a"] if flip else m["team_b"]
    ws    = m["score_b"] if flip else m["score_a"]
    ls    = m["score_a"] if flip else m["score_b"]
    return w, l, ws, ls


def print_h2h(records, min_games=2):
    section("HEAD-TO-HEAD RECORDS")
    seen, rows = set(), []
    for (a, b), rec in records.items():
        key = tuple(sorted([a, b]))
        if key in seen:
            continue
        seen.add(key)
        total = rec["wins"] + rec["losses"] + rec["ties"]
        if total < min_games:
            continue
        rec_b = records.get((b, a), {})
        rows.append((a, b, rec["wins"], rec_b.get("wins", 0), rec["ties"], total, rec["pf"], rec_b.get("pf", 0.0)))

    rows.sort(key=lambda r: r[5], reverse=True)
    print(f"\n{'Matchup':<42} {'W-L':>7} {'PF (A)':>9} {'PF (B)':>9}")
    hr()
    for a, b, aw, bw, ties, total, apf, bpf in rows:
        wl = f"{aw}-{bw}" + (f"-{ties}" if ties else "")
        print(f"{a} vs {b:<{40 - len(a)}} {wl:>7} {apf:>9.1f} {bpf:>9.1f}")


def print_closest(matchups, n=10):
    section(f"CLOSEST GAMES (Regular Season) — Top {n}")
    games = sorted([m for m in matchups if not m["is_playoff"]], key=lambda x: x["margin"])[:n]
    print(f"\n{'Yr':>4} {'Wk':>3}  {'Winner':<24} {'Loser':<24} {'Score':>14}  {'Margin':>7}")
    hr()
    for m in games:
        w, l, ws, ls = _game_row(m)
        print(f"{m['year']:>4} {m['week']:>3}  {w:<24} {l:<24} {ws:>6.2f}-{ls:<6.2f}  {m['margin']:>7.2f}")


def print_blowouts(matchups, n=10):
    section(f"BIGGEST BLOWOUTS (Regular Season) — Top {n}")
    games = sorted([m for m in matchups if not m["is_playoff"]], key=lambda x: x["margin"], reverse=True)[:n]
    print(f"\n{'Yr':>4} {'Wk':>3}  {'Winner':<24} {'Loser':<24} {'Score':>14}  {'Margin':>7}")
    hr()
    for m in games:
        w, l, ws, ls = _game_row(m)
        print(f"{m['year']:>4} {m['week']:>3}  {w:<24} {l:<24} {ws:>6.2f}-{ls:<6.2f}  {m['margin']:>7.2f}")


def print_points_history(history, seasons):
    section("POINTS SCORED BY SEASON")
    years   = sorted(seasons.keys())
    owners  = sorted(history.keys())
    totals  = {o: sum(s["pf"] for s in history[o].values()) for o in owners}

    print(f"\n{'Owner':<26}", end="")
    for y in years:
        print(f"  {y}", end="")
    print(f"  {'Total':>8}  {'Avg/Yr':>7}")
    hr()

    for owner in sorted(owners, key=lambda o: totals[o], reverse=True):
        row = history[owner]
        print(f"{owner:<26}", end="")
        for y in years:
            pts = row.get(y, {}).get("pf", 0.0)
            print(f"  {'     -' if not pts else f'{pts:>6.0f}'}", end="")
        avg = totals[owner] / len(row) if row else 0
        print(f"  {totals[owner]:>8.1f}  {avg:>7.1f}")

    print("\n  Top 5 Individual Seasons:")
    all_s = [(o, yr, d["pf"], d["pts_rank"]) for o, yrs in history.items() for yr, d in yrs.items()]
    for o, yr, pts, rank in sorted(all_s, key=lambda x: x[2], reverse=True)[:5]:
        print(f"    {o:<26} {yr}  {pts:>8.1f} pts  (#{rank} scorer that year)")

    print("\n  Most Consistent (3+ seasons):")
    for o, mean, stdev in sorted(
        [(o, statistics.mean(pts_list := [d["pf"] for d in yrs.values() if d["pf"] > 0]),
          statistics.stdev(pts_list))
         for o, yrs in history.items() if len([d for d in yrs.values() if d["pf"] > 0]) >= 3],
        key=lambda x: x[2]
    )[:5]:
        print(f"    {o:<26} avg {mean:>7.1f}  stdev {stdev:>6.1f}")


# ── Export ────────────────────────────────────────────────────────────────────

def export_data(seasons, aliases, matchups, h2h, history, champions, lineup_data=None):
    seen, h2h_list = set(), []
    for (a, b), rec in h2h.items():
        key = tuple(sorted([a, b]))
        if key in seen:
            continue
        seen.add(key)
        rec_b = h2h.get((b, a), {})
        total = rec["wins"] + rec["losses"] + rec["ties"]
        h2h_list.append({
            "owner_a": a, "owner_b": b,
            "a_wins":  rec["wins"],
            "b_wins":  rec_b.get("wins", 0),
            "ties":    rec["ties"],
            "total":   total,
            "a_pf":    round(rec["pf"], 2),
            "b_pf":    round(rec_b.get("pf", 0.0), 2),
        })

    clean_matchups = [{
        "year":       m["year"],
        "week":       m["week"],
        "is_playoff": m["is_playoff"],
        "team_a":     m["team_a"],
        "team_b":     m["team_b"],
        "score_a":    round(m["score_a"], 2),
        "score_b":    round(m["score_b"], 2),
        "winner":     m["winner"],
        "margin":     round(m["margin"], 2),
    } for m in matchups]

    season_rankings = [
        {"owner": o, "year": yr, "pf": round(d["pf"], 2), "pa": round(d["pa"], 2),
         "wins": d["wins"], "losses": d["losses"], "standing": d["standing"], "pts_rank": d["pts_rank"]}
        for o, yrs in history.items() for yr, d in yrs.items()
    ]

    data = {
        "meta": {
            "league_id":      LEAGUE_ID,
            "seasons":        sorted(seasons.keys()),
            "generated":      str(date.today()),
            "total_matchups": len(clean_matchups),
            "aliases_merged": aliases,
            "champions":      {str(yr): name for yr, name in champions.items()},
        },
        "owners": sorted(history.keys()),
        "h2h":    h2h_list,
        "matchups": clean_matchups,
        "points_history": {
            o: {str(yr): {"pf": round(d["pf"],2), "pa": round(d["pa"],2),
                          "wins": d["wins"], "losses": d["losses"],
                          "standing": d["standing"], "pts_rank": d["pts_rank"]}
                for yr, d in yrs.items()}
            for o, yrs in history.items()
        },
        "season_rankings": season_rankings,
        "lineups": lineup_data,
    }

    os.makedirs("docs", exist_ok=True)
    out = os.path.join("docs", "data.js")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"const LEAGUE_DATA = {json.dumps(data, indent=2)};\n")

    print(f"\nExported -> {out}")
    print(f"  {len(clean_matchups)} matchups | {len(history)} owners | {len(seasons)} seasons")
    print(f"  Owners: {', '.join(sorted(history.keys()))}")
    if aliases:
        print(f"  Merged aliases: {aliases}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fantasy Football Rivalry Analyzer")
    parser.add_argument("--export", action="store_true", help="Export to docs/data.js for the web app")
    args = parser.parse_args()

    if not ESPN_S2 or not SWID:
        print("ERROR: ESPN_S2 and SWID must be set in .env")
        sys.exit(1)

    seasons = fetch_seasons()
    if not seasons:
        print("No seasons loaded. Check credentials and LEAGUE_ID.")
        sys.exit(1)

    print(f"\nLoaded {len(seasons)} season(s): {min(seasons)}–{max(seasons)}")

    print("\nDetecting player aliases...")
    aliases = detect_aliases(seasons)
    if not aliases:
        print("  None detected.")

    matchups = build_matchups(seasons, aliases)
    print(f"Parsed {len(matchups)} matchups")

    h2h     = build_h2h(matchups)
    history = build_points_history(seasons, aliases)

    if args.export:
        print("\nFinding champions from playoff brackets...")
        champions = find_champions(seasons, aliases)
        lineup_data = build_lineup_data(seasons, aliases)
        export_data(seasons, aliases, matchups, h2h, history, champions, lineup_data)
    else:
        print_h2h(h2h)
        print_closest(matchups)
        print_blowouts(matchups)
        print_points_history(history, seasons)
        print()


if __name__ == "__main__":
    main()
