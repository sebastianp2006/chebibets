#!/usr/bin/env python3
"""Genera las líneas de FantasyBet para la liga de ESPN.

Junta proyecciones de ESPN, RotoWire (vía la API pública de Sleeper) y CBS Sports,
calcula momios para matchups, props de jugadores y futuros, escribe site/data.json
y, si hay credenciales de Supabase, sincroniza los mercados y liquida boletos.
"""
import json, math, os, random, re, sys, time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

LEAGUE = os.environ.get("ESPN_LEAGUE_ID", "1954666429")
SEASON = int(os.environ.get("SEASON", "2026"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
SLOT = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K", 23: "FLEX"}
SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "DST", "K"]
ABBR_FIX = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR", "LVR": "LV", "OAK": "LV"}
# Desviación de una semana: base + fracción de la proyección
SD = {"QB": (4.0, .25), "RB": (3.0, .40), "WR": (3.0, .45), "TE": (2.5, .45), "K": (4.0, 0), "DST": (5.0, 0)}
VIG2 = 0.045   # margen de la casa en mercados de dos lados
VIG1 = 0.09    # margen en mercados de un solo lado (TD, escaleras, futuros)
TEAM_WEEK_SD = 24.0
SIMS = 20000


def get(url, headers=None, raw=False, tries=3):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=h), timeout=40) as r:
                b = r.read()
                return b.decode("utf-8", "replace") if raw else json.loads(b)
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(f"{url}: {e}")
            time.sleep(2 + i * 3)


def espn(views, week=None, flt=None):
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
           f"/segments/0/leagues/{LEAGUE}?" + "&".join(f"view={v}" for v in views))
    if week:
        url += f"&scoringPeriodId={week}"
    return get(url, {"x-fantasy-filter": json.dumps(flt)} if flt else None)


def norm(name):
    n = name.lower().replace("’", "'")
    n = re.sub(r"[.'\-]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def abbr(a):
    a = (a or "").upper()
    return ABBR_FIX.get(a, a)


# ---------- matemática de momios ----------

def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def to_dec(p, vig):
    p = min(max(p, 0.004), 0.985)
    # En mercados de un solo lado el margen pesa más en los momios largos que en los favoritos
    q = p * (1 + 2 * vig * (1 - p)) if vig == VIG1 else p * (1 + vig)
    return round(max(1.01, min(1 / min(q, 0.99), 250.0)), 3)


def poisson_ge(lam, k):
    s = sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
    return 1 - s


def half(x):
    return math.floor(x) + 0.5


# ---------- fuentes de proyección ----------

def src_sleeper(week):
    """Proyecciones de RotoWire publicadas por la API abierta de Sleeper."""
    q = "&".join(f"position[]={p}" for p in ["QB", "RB", "WR", "TE", "K", "DEF"])
    rows = get(f"https://api.sleeper.app/projections/nfl/{SEASON}/{week}?season_type=regular&{q}")
    out = {}
    for r in rows:
        p, s = r.get("player") or {}, r.get("stats") or {}
        pos = p.get("position")
        if "pts_ppr" not in s:
            continue
        if pos == "DEF":
            key = ("dst", abbr(r.get("team") or p.get("team")))
        else:
            key = (norm(f"{p.get('first_name','')} {p.get('last_name','')}"), pos)
        out[key] = {"pts": s["pts_ppr"], "td": s.get("rush_td", 0) + s.get("rec_td", 0),
                    "ptd": s.get("pass_td", 0)}
    return out


def src_cbs(week):
    out = {}
    for pos in ["QB", "RB", "WR", "TE", "K", "DST"]:
        url = f"https://www.cbssports.com/fantasy/football/stats/{pos}/{SEASON}/{week}/projections/ppr/"
        try:
            s = get(url, raw=True)
        except RuntimeError as e:
            print("CBS", pos, e, file=sys.stderr)
            continue
        head = s[s.find("<thead"):s.find("</thead>")]
        cols = []
        for th in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S):
            parts = [x.strip() for x in re.sub(r"<[^>]+>", "|", th).split("|") if x.strip()]
            if len(parts) >= 2 and parts[0] not in ("Player", "Team"):
                cols.append(parts[-1].lower())
        body = s[s.find("<tbody"):s.find("</tbody>")]
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            nums = [float(x) if x.strip() not in ("—", "-", "") else 0.0 for x in
                    re.findall(r'TableBase-bodyTd--number[^"]*"\s*>\s*([^<]*?)\s*<', tr)]
            if len(nums) < 2:
                continue
            vals = dict(zip(cols, nums)) if len(cols) == len(nums) else {}
            fpts = vals.get("fantasy points", nums[-2])
            if pos == "DST":
                m = re.search(r'/nfl/teams/([A-Z]+)/', tr)
                if m:
                    out[("dst", abbr(m.group(1)))] = {"pts": fpts}
                continue
            m = re.search(r'CellPlayerName--long.*?<a[^>]*>([^<]+)</a>', tr, re.S)
            if not m:
                continue
            td = sum(v for k, v in vals.items() if "touchdown" in k and "pass" not in k)
            ptd = sum(v for k, v in vals.items() if "touchdown" in k and "pass" in k)
            out[(norm(m.group(1)), pos)] = {"pts": fpts, "td": td, "ptd": ptd}
    return out


# ---------- datos de ESPN ----------

def nfl_games(week):
    d = get(f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
            f"?seasontype=2&week={week}&dates={SEASON}")
    games = {}
    for e in d.get("events", []):
        c = e["competitions"][0]
        st = e["status"]
        state = st["type"]["state"]
        per, clock = st.get("period") or 0, st.get("clock") or 0
        if state == "post":
            frac = 1.0
        elif state == "in":
            frac = min(1.0, ((min(per, 4) - 1) * 900 + (900 - clock if per <= 4 else 900)) / 3600)
        else:
            frac = 0.0
        teams = [abbr(t["team"]["abbreviation"]) for t in c["competitors"]]
        for t in c["competitors"]:
            me = abbr(t["team"]["abbreviation"])
            opp = [x for x in teams if x != me][0]
            games[me] = {"opp": ("vs " if t["homeAway"] == "home" else "@ ") + opp,
                         "kick": e["date"], "state": state, "frac": frac,
                         "detail": st["type"].get("shortDetail", "")}
    return games


def pro_teams():
    d = get(f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}?view=proTeamSchedules")
    return {t["id"]: abbr(t["abbrev"]) for t in d["settings"]["proTeams"]}


def team_name(t):
    return t.get("name") or f"{t.get('location','')} {t.get('nickname','')}".strip()


def player_sd(pos, mean):
    a, b = SD.get(pos, (4, .4))
    return a + b * mean


# ---------- construcción de una semana ----------

def build_week(week, current_week, teams, proteams):
    box = espn(["mMatchupScore", "mBoxscore"], week,
               {"schedule": {"filterMatchupPeriodIds": {"value": [week]}}})
    games = nfl_games(week)
    sources = {}
    if week >= current_week:
        for name, fn in [("RotoWire", src_sleeper), ("CBS", src_cbs)]:
            try:
                sources[name] = fn(week)
            except Exception as e:
                print("fuente", name, "falló:", e, file=sys.stderr)

    matchups, players, markets = [], {}, []
    for m in box["schedule"]:
        if m["matchupPeriodId"] != week or "away" not in m:
            continue
        side = {}
        for hs in ("home", "away"):
            t = m[hs]
            lineup = []
            for e in (t.get("rosterForCurrentScoringPeriod") or {}).get("entries", []):
                slot = SLOT.get(e["lineupSlotId"])
                p = e["playerPoolEntry"]["player"]
                pos = POS.get(p.get("defaultPositionId"), "?")
                pro = proteams.get(p.get("proTeamId"), "FA")
                g = games.get(pro)
                proj_espn, act, tds = None, None, {}
                for s in p.get("stats", []):
                    if s.get("scoringPeriodId") != week:
                        continue
                    if s.get("statSourceId") == 1:
                        proj_espn = s.get("appliedTotal", 0)
                        st = s.get("stats", {})
                        tds["espn"] = (st.get("25", 0) + st.get("43", 0), st.get("4", 0))
                    elif s.get("statSourceId") == 0:
                        act = s.get("appliedTotal", 0)
                        st = s.get("stats", {})
                        tds["act"] = (st.get("25", 0) + st.get("43", 0), st.get("4", 0))
                key = ("dst", pro) if pos == "DST" else (norm(p["fullName"]), pos)
                src = {}
                tdl, ptdl = [], []
                if proj_espn is not None:
                    src["ESPN"] = round(proj_espn, 2)
                    tdl.append(tds["espn"][0]); ptdl.append(tds["espn"][1])
                for sname, sd in sources.items():
                    if key in sd:
                        src[sname] = round(sd[key]["pts"], 2)
                        tdl.append(sd[key].get("td", 0)); ptdl.append(sd[key].get("ptd", 0))
                out = (p.get("injuryStatus") in ("OUT", "INJURY_RESERVE", "SUSPENSION")) or g is None
                cons = 0.0 if out or not src else sum(src.values()) / len(src)
                spread = (max(src.values()) - min(src.values())) if len(src) > 1 else 0
                sd_full = math.hypot(player_sd(pos, cons), spread / 2) if cons > 0 else 0.5
                state = g["state"] if g else "bye"
                frac = g["frac"] if g else 1.0
                actual = act or 0.0
                mean = actual + cons * (1 - frac)
                sd_now = sd_full * math.sqrt(max(1 - frac, 0))
                pl = {"id": p["id"], "name": p["fullName"], "pos": pos, "slot": slot or "BN",
                      "nfl": pro, "opp": g["opp"] if g else "BYE", "kick": g["kick"] if g else None,
                      "state": state, "detail": g["detail"] if g else "Bye",
                      "inj": p.get("injuryStatus") if p.get("injuryStatus") not in (None, "ACTIVE") else None,
                      "src": src, "proj": round(cons, 2), "actual": round(actual, 2),
                      "mean": round(mean, 2), "sd": round(sd_now, 2),
                      "td": round(sum(tdl) / len(tdl), 3) if tdl else 0,
                      "ptd": round(sum(ptdl) / len(ptdl), 3) if ptdl else 0,
                      "act_td": tds.get("act", (0, 0))[0], "act_ptd": tds.get("act", (0, 0))[1],
                      "played": act is not None, "team": t["teamId"]}
                players[p["id"]] = pl       # la banca también se liquida si alguien ya le apostó
                if slot:
                    lineup.append(pl)
            lineup.sort(key=lambda x: SLOT_ORDER.index(x["slot"]))
            mean = sum(x["mean"] for x in lineup)
            var = sum(x["sd"] ** 2 for x in lineup)
            side[hs] = {"team": t["teamId"], "lineup": lineup, "mean": mean, "sd": math.sqrt(var),
                        "live": t.get("totalPointsLive", t.get("totalPoints", 0)),
                        "final": t.get("totalPoints", 0), "espnProj": t.get("totalProjectedPointsLive")}
        h, a = side["home"], side["away"]
        states = [x["state"] for x in h["lineup"] + a["lineup"]]
        if all(s in ("post", "bye") for s in states):
            gstate = "closed"
        elif any(s == "in" for s in states):
            gstate = "suspended"
        else:
            gstate = "open"
        diff_sd = max(math.hypot(h["sd"], a["sd"]), 0.5)
        p_home = 1 - phi((0 - (h["mean"] - a["mean"])) / diff_sd)
        line = half(h["mean"] - a["mean"])                     # local favorito si es positivo
        p_home_cover = 1 - phi((line - (h["mean"] - a["mean"])) / diff_sd)
        tot = half(h["mean"] + a["mean"])
        p_over = 1 - phi((tot - (h["mean"] + a["mean"])) / diff_sd)
        winner = m.get("winner")
        decided = winner in ("HOME", "AWAY", "TIE")
        hf, af = h["final"], a["final"]
        gid = m["id"]
        grp = f"{week}:g{gid}"
        hn, an = teams[h["team"]]["name"], teams[a["team"]]["name"]

        def res(cond, push=False):
            return None if not decided else ("push" if push else ("win" if cond else "loss"))
        mk = [
            (f"{week}:ml:{gid}:h", grp, f"{hn} gana", p_home, VIG2, res(winner == "HOME", winner == "TIE")),
            (f"{week}:ml:{gid}:a", grp, f"{an} gana", 1 - p_home, VIG2, res(winner == "AWAY", winner == "TIE")),
            (f"{week}:sp:{gid}:h", grp, f"{hn} {-line:+.1f}", p_home_cover, VIG2, res(hf - af > line)),
            (f"{week}:sp:{gid}:a", grp, f"{an} {line:+.1f}", 1 - p_home_cover, VIG2, res(hf - af < line)),
            (f"{week}:tot:{gid}:o", f"{week}:t{gid}", f"Over {tot} pts ({hn} vs {an})", p_over, VIG2, res(hf + af > tot)),
            (f"{week}:tot:{gid}:u", f"{week}:t{gid}", f"Under {tot} pts ({hn} vs {an})", 1 - p_over, VIG2, res(hf + af < tot)),
        ]
        for mid, g_, label, p, vig, r in mk:
            markets.append(dict(id=mid, week=week, grp=g_, label=label, dec=to_dec(p, vig),
                                prob=round(p, 4), status=gstate if r is None else "closed", result=r))
        matchups.append({"id": gid, "home": h, "away": a, "state": gstate, "line": line, "total": tot,
                         "pHome": round(p_home, 4), "winner": winner})

    for pid, pl in players.items():
        markets += player_markets(week, pl, teams)
    markets += week_extremes(week, matchups, teams)
    return {"matchups": matchups, "markets": markets, "sources": sorted(["ESPN", *sources])}


def week_extremes(week, matchups, teams):
    """Equipo con más y con menos puntos de la semana, simulando a los 10 equipos juntos."""
    sides = [s for m in matchups for s in (m["home"], m["away"])]
    if not sides:
        return []
    states = [m["state"] for m in matchups]
    status = "closed" if all(s == "closed" for s in states) else "suspended" if "suspended" in states else "open"
    decided = all(m["winner"] in ("HOME", "AWAY", "TIE") for m in matchups)
    hi = {s["team"]: 0 for s in sides}
    lo = dict(hi)
    rnd = random.Random(week * 104729)
    for _ in range(SIMS):
        pts = {s["team"]: rnd.gauss(s["mean"], max(s["sd"], 0.01)) for s in sides}
        hi[max(pts, key=pts.get)] += 1
        lo[min(pts, key=pts.get)] += 1
    final = {s["team"]: s["final"] for s in sides}
    top, bottom = max(final.values()), min(final.values())
    out = []
    for kind, cnt, name, target in (("hi", hi, "más puntos", top), ("lo", lo, "menos puntos", bottom)):
        for t, c in cnt.items():
            p = c / SIMS
            r = ("win" if final[t] == target else "loss") if decided else None
            if r is None and p < 0.01:
                continue
            out.append(dict(id=f"{week}:{kind}:{t}", week=week, grp=f"{week}:{kind}",
                            label=f"{teams[t]['name']} · equipo con {name} de la semana",
                            dec=to_dec(p, VIG1), prob=round(p, 4), status="closed" if r else status,
                            result=r, cat=f"Equipo con {name}", team=t))
    return out


def player_markets(week, pl, teams):
    out = []
    if pl["state"] == "bye" or pl["proj"] <= 0.5:
        return out
    grp = f"{week}:p{pl['id']}"
    status = "open" if pl["state"] == "pre" else "closed"
    done = pl["state"] == "post"
    void = done and not pl["played"]
    mu, sd = pl["proj"], max(player_sd(pl["pos"], pl["proj"]), 1.0)
    if pl["src"]:
        sd = math.hypot(sd, (max(pl["src"].values()) - min(pl["src"].values())) / 2)
    who = pl["name"]
    act = pl["actual"]

    def res(cond):
        if not done:
            return None
        return "push" if void else ("win" if cond else "loss")
    line = half(mu)
    p_over = 1 - phi((line - mu) / sd)
    out.append((f"{week}:pts:{pl['id']}:o", f"{who} over {line} pts", p_over, VIG2, res(act > line), "Puntos fantasy"))
    out.append((f"{week}:pts:{pl['id']}:u", f"{who} under {line} pts", 1 - p_over, VIG2, res(act < line), "Puntos fantasy"))
    for k in range(5, 60, 5):
        p = 1 - phi((k - 0.5 - mu) / sd)
        if 0.07 <= p <= 0.80 and abs(k - line) >= 2:
            out.append((f"{week}:alt:{pl['id']}:{k}", f"{who} {k}+ pts", p, VIG1, res(act >= k), "Escalera de puntos"))
    if pl["pos"] in ("QB", "RB", "WR", "TE") and pl["td"] > 0.03:
        lam = pl["td"]
        out.append((f"{week}:td:{pl['id']}:1", f"{who} anota TD", poisson_ge(lam, 1), VIG1,
                    res(pl["act_td"] >= 1), "Touchdowns"))
        if lam > 0.3:
            out.append((f"{week}:td:{pl['id']}:2", f"{who} anota 2+ TD", poisson_ge(lam, 2), VIG1,
                        res(pl["act_td"] >= 2), "Touchdowns"))
    if pl["pos"] == "QB" and pl["ptd"] > 0.3:
        lam = pl["ptd"]
        pl_line = 1.5 if lam < 2.0 else 2.5
        p = poisson_ge(lam, int(pl_line + 0.5))
        out.append((f"{week}:ptd:{pl['id']}:o", f"{who} over {pl_line} TD de pase", p, VIG2,
                    res(pl["act_ptd"] > pl_line), "Touchdowns"))
        out.append((f"{week}:ptd:{pl['id']}:u", f"{who} under {pl_line} TD de pase", 1 - p, VIG2,
                    res(pl["act_ptd"] < pl_line), "Touchdowns"))
    return [dict(id=i, week=week, grp=grp, label=l, dec=to_dec(p, v), prob=round(p, 4),
                 status=status if r is None else "closed", result=r, cat=c, player=pl["id"])
            for i, l, p, v, r, c in out]


# ---------- futuros: simulación de temporada ----------

def futures(league, teams, cur_week, strength):
    s = league["settings"]["scheduleSettings"]
    reg = s["matchupPeriodCount"]
    n_po = s["playoffTeamCount"]
    sched = [m for m in league["schedule"] if m["matchupPeriodId"] <= reg and "away" in m]
    ids = list(teams)
    base_w = {t: 0 for t in ids}
    base_pf = {t: 0.0 for t in ids}
    remaining = []
    for m in sched:
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        if m["winner"] in ("HOME", "AWAY", "TIE"):
            base_pf[h] += m["home"]["totalPoints"]; base_pf[a] += m["away"]["totalPoints"]
            if m["winner"] == "HOME": base_w[h] += 1
            elif m["winner"] == "AWAY": base_w[a] += 1
            else: base_w[h] += .5; base_w[a] += .5
        else:
            remaining.append((h, a))
    reg_done = not remaining
    cnt = {k: {t: 0 for t in ids} for k in ("champ", "playoffs", "last", "mostpf", "leastpf")}
    rnd = random.Random(cur_week * 7919)
    for _ in range(SIMS):
        w, pf = dict(base_w), dict(base_pf)
        for h, a in remaining:
            sh = rnd.gauss(strength[h], TEAM_WEEK_SD); sa = rnd.gauss(strength[a], TEAM_WEEK_SD)
            pf[h] += sh; pf[a] += sa
            w[h if sh > sa else a] += 1
        order = sorted(ids, key=lambda t: (w[t], pf[t]), reverse=True)
        for t in order[:n_po]:
            cnt["playoffs"][t] += 1
        cnt["last"][order[-1]] += 1
        cnt["mostpf"][max(ids, key=pf.get)] += 1
        cnt["leastpf"][min(ids, key=pf.get)] += 1
        seeds = order[:n_po]
        game = lambda x, y: x if rnd.gauss(strength[x], TEAM_WEEK_SD) > rnd.gauss(strength[y], TEAM_WEEK_SD) else y
        if n_po == 6:
            r1 = [seeds[0], seeds[1], game(seeds[2], seeds[5]), game(seeds[3], seeds[4])]
            f = [game(r1[0], r1[3]), game(r1[1], r1[2])]
        else:
            while len(seeds) > 2:
                seeds = [game(seeds[i], seeds[-1 - i]) for i in range(len(seeds) // 2)]
            f = seeds
        cnt["champ"][game(*f)] += 1
    names = {"champ": "Campeón", "playoffs": "Entra a playoffs", "miss": "No entra a playoffs",
             "last": "Último lugar", "mostpf": "Más puntos a favor", "leastpf": "Menos puntos a favor"}
    out, table = [], []
    for t in ids:
        probs = {k: cnt[k][t] / SIMS for k in cnt}
        probs["miss"] = 1 - probs["playoffs"]
        table.append({"team": t, **{k: round(v, 4) for k, v in probs.items()},
                      "w": base_w[t], "pf": round(base_pf[t], 1), "strength": round(strength[t], 1)})
        for k, p in probs.items():
            if p < 0.01 or p > 0.97:
                continue      # prácticamente decidido: no se ofrece
            two_way = k in ("playoffs", "miss")
            grp = f"F:{k if k not in ('playoffs','miss') else 'po'}:{t}"
            out.append(dict(id=f"F:{k}:{t}", week=0, grp=grp, label=f"{teams[t]['name']} · {names[k]}",
                            dec=to_dec(p, VIG2 if two_way else VIG1), prob=round(p, 4),
                            status="closed" if (reg_done and k != "champ") else "open",
                            result=None, cat=names[k], team=t))
    return out, table


def futures_results(league, teams):
    """Resultados de futuros cuando la temporada regular o los playoffs terminan."""
    s = league["settings"]["scheduleSettings"]
    reg = s["matchupPeriodCount"]
    sched = [m for m in league["schedule"] if "away" in m]
    if any(m["winner"] == "UNDECIDED" for m in sched if m["matchupPeriodId"] <= reg):
        return {}
    res = {}
    rec = {t: (teams[t]["w"], teams[t]["pf"]) for t in teams}
    order = sorted(teams, key=lambda t: rec[t], reverse=True)
    po = set(order[:s["playoffTeamCount"]])
    most = max(teams, key=lambda t: teams[t]["pf"]); least = min(teams, key=lambda t: teams[t]["pf"])
    for t in teams:
        res[f"F:playoffs:{t}"] = "win" if t in po else "loss"
        res[f"F:miss:{t}"] = "loss" if t in po else "win"
        res[f"F:last:{t}"] = "win" if t == order[-1] else "loss"
        res[f"F:mostpf:{t}"] = "win" if t == most else "loss"
        res[f"F:leastpf:{t}"] = "win" if t == least else "loss"
    last = max(m["matchupPeriodId"] for m in sched)
    finals = [m for m in sched if m["matchupPeriodId"] == last and m.get("playoffTierType") == "WINNERS_BRACKET"]
    if finals and finals[0]["winner"] in ("HOME", "AWAY"):
        champ = finals[0]["home" if finals[0]["winner"] == "HOME" else "away"]["teamId"]
        for t in teams:
            res[f"F:champ:{t}"] = "win" if t == champ else "loss"
    return res


# ---------- Supabase ----------

def sb(path, body=None, method="POST"):
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if not SB_KEY.startswith("sb_"):      # las llaves viejas (JWT) también van como Bearer
        h["Authorization"] = f"Bearer {SB_KEY}"
    req = Request(f"{SB_URL}/rest/v1/{path}", data=json.dumps(body).encode() if body is not None else None,
                  headers=h, method=method)
    with urlopen(req, timeout=60) as r:
        b = r.read()
        return json.loads(b) if b else None


BOOST_CAP = 1000       # tope por persona en cada supermomio
BOOST_MAX = 1.25       # el supermomio paga a lo mucho 25% más que el combinado normal
DREAM_CAP = 1000
DREAM_MAX = 1.70
BOOST_EDGE = 0.97      # y nunca más del 97% del momio justo: la casa conserva ventaja


def make_boosts(week, wmk, matchups, fixed):
    """Supermomios de la semana. Las selecciones se eligen una vez y quedan fijas
    (fixed viene de Supabase); el momio se recalcula en cada corrida."""
    by_id = {m["id"]: m for m in wmk}
    where = {}                                  # jugador -> (matchup, equipo NFL)
    for m in matchups:
        for s in (m["home"], m["away"]):
            for p in s["lineup"]:
                where[p["id"]] = (m["id"], p["nfl"])

    def leg_key(m):
        if m.get("player"):
            return where.get(m["player"], (None, None))
        return (int(m["id"].split(":")[2]), None) if ":ml:" in m["id"] else (None, None)

    def pick(pool, k, used_g, used_n):
        out = []
        for m in pool:
            g, nfl = leg_key(m)
            if g is None or g in used_g or (nfl and nfl in used_n):
                continue
            out.append(m); used_g.add(g); used_n.add(nfl)
            if len(out) == k:
                return out
        return None

    templates = {}
    if fixed:
        templates = {k.split(":")[-1]: v for k, v in fixed.items() if k.startswith(f"{week}:boost:")}
    else:
        rnd = random.Random(week * 7717)
        op = [m for m in wmk if m["status"] == "open"]
        ml = sorted([m for m in op if ":ml:" in m["id"] and 0.55 <= m["prob"] <= 0.82], key=lambda m: -m["prob"])
        td = [m for m in op if ":td:" in m["id"] and m["id"].endswith(":1") and 0.38 <= m["prob"] <= 0.72]
        proj = {p["id"]: p["proj"] for mt in matchups for s in (mt["home"], mt["away"]) for p in s["lineup"]}
        over = [m for m in op if ":pts:" in m["id"] and m["id"].endswith(":o") and m["player"] in proj]
        stars = sorted(over, key=lambda m: -proj[m["player"]])
        rnd.shuffle(td)
        combos = {
            "fav": pick(ml, 3, set(), set()),
            "tdml": None,
            "stars": pick(stars[:12], 3, set(), set()),
            "td2": pick(td, 2, set(), set()),
        }
        g1, n1 = set(), set()
        t = pick(td, 1, g1, n1)
        w = pick(ml, 1, g1, n1)
        combos["tdml"] = (t + w) if t and w else None
        # soñadores: combinados difíciles con aumento grande
        dogs = sorted([m for m in op if ":ml:" in m["id"] and 0.22 <= m["prob"] <= 0.45], key=lambda m: m["prob"])
        td_all = sorted([m for m in op if ":td:" in m["id"] and m["id"].endswith(":1") and 0.3 <= m["prob"] <= 0.6],
                        key=lambda m: m["prob"])
        boom = [m for m in op if ":alt:" in m["id"] and 0.12 <= m["prob"] <= 0.3 and m["player"] in proj]
        rnd.shuffle(boom)
        combos["s_dogs"] = pick(dogs, 3, set(), set()) or pick(dogs, 2, set(), set())
        combos["s_td3"] = pick(td_all, 3, set(), set())
        combos["s_boom"] = pick(boom, 2, set(), set())
        templates = {k: [m["id"] for m in v] for k, v in combos.items() if v}

    names = {"fav": "Favoritos de la semana", "tdml": "TD + victoria",
             "stars": "Over de estrellas", "td2": "Doble TD",
             "s_dogs": "Soñador: Noche de underdogs", "s_td3": "Soñador: Lluvia de TDs",
             "s_boom": "Soñador: Semana explosiva"}
    out = []
    for key, legs in templates.items():
        ms = [by_id.get(i) for i in legs]
        if any(m is None for m in ms):
            continue
        p = math.prod(m["prob"] for m in ms)
        base = math.prod(m["dec"] for m in ms)
        dream = key.startswith("s_")
        boosted = round(min(base * (DREAM_MAX if dream else BOOST_MAX), BOOST_EDGE / p), 2) if p > 0 else base
        lo, hi = (0.015, 0.13) if dream else (0.12, 0.45)
        if not fixed and not (lo <= p <= hi and boosted >= base * (1.10 if dream else 1.05)):
            continue                            # nada de "locks" ni aumentos de adorno
        res = [m["result"] for m in ms]
        result = ("loss" if "loss" in res else None if None in res else
                  "win" if all(r == "win" for r in res) else "push")
        sts = [m["status"] for m in ms]
        status = "closed" if result or "closed" in sts else "suspended" if "suspended" in sts else "open"
        out.append(dict(id=f"{week}:boost:{key}", week=week, grp=f"{week}:boost:{key}",
                        label=f"⚡ {names.get(key, 'Supermomio')}: " + " + ".join(m["label"] for m in ms),
                        dec=max(boosted, round(base, 2)), was_dec=round(base, 3), prob=round(p, 4),
                        status=status, result=result, cat="Supermomio", max_stake=DREAM_CAP if dream else BOOST_CAP, dream=dream,
                        legs=legs, title=names.get(key, "Supermomio"), leg_labels=[m["label"] for m in ms]))
    return out


def main():
    league = espn(["mSettings", "mStatus", "mTeam", "mMatchupScore"])
    cur = league["status"]["currentMatchupPeriod"]
    reg = league["settings"]["scheduleSettings"]["matchupPeriodCount"]
    teams = {}
    for t in league["teams"]:
        r = t["record"]["overall"]
        teams[t["id"]] = {"id": t["id"], "name": team_name(t), "abbrev": t.get("abbrev"),
                          "logo": t.get("logo"), "w": r["wins"] + r.get("ties", 0) / 2, "l": r["losses"],
                          "pf": round(r["pointsFor"], 2), "pa": round(r["pointsAgainst"], 2),
                          "seed": t.get("playoffSeed")}
    proteams = pro_teams()

    weeks_to_grade = set()
    if SB_URL and SB_KEY:
        try:
            rows = sb("rpc/open_weeks", {})
            weeks_to_grade = {r["week"] if isinstance(r, dict) else r for r in rows or []}
        except Exception as e:
            print("Supabase open_weeks:", e, file=sys.stderr)
    weeks_to_grade = {w for w in weeks_to_grade if 0 < w < cur}

    def fixed_boosts(w):
        if not (SB_URL and SB_KEY):
            return {}
        try:
            return sb("rpc/boost_legs", {"p_week": w}) or {}
        except Exception as e:
            print("Supabase boost_legs:", e, file=sys.stderr)
            return {}

    wk = build_week(cur, cur, teams, proteams)
    wk["markets"] += make_boosts(cur, wk["markets"], wk["matchups"], fixed_boosts(cur))
    markets = list(wk["markets"])
    for w in sorted(weeks_to_grade):
        old = build_week(w, cur, teams, proteams)
        fx = fixed_boosts(w)
        markets += old["markets"] + (make_boosts(w, old["markets"], old["matchups"], fx) if fx else [])

    # fuerza de cada equipo: mitad lo que llevan anotando, mitad su alineación de esta semana
    played = max(1, min(cur - 1, reg))
    strength = {}
    for t in teams:
        now = next((s["mean"] for m in wk["matchups"] for s in (m["home"], m["away"])
                    if s["team"] == t and m["state"] == "open"), None)
        ppg = teams[t]["pf"] / played if teams[t]["pf"] else 110
        strength[t] = ppg if now is None else 0.5 * ppg + 0.5 * now
    fut, fut_table = futures(league, teams, cur, strength) if cur <= reg + 3 else ([], [])
    fres = futures_results(league, teams)
    for m in fut:
        if m["id"] in fres:
            m["result"], m["status"] = fres[m["id"]], "closed"
    markets += fut
    # futuros ya decididos que dejaron de generarse (probabilidad 0 o 1) también se liquidan
    graded_ids = {m["id"] for m in fut}
    extra = [{"id": k, "result": v} for k, v in fres.items() if k not in graded_ids]

    starters = {p["id"] for m in wk["matchups"] for s in (m["home"], m["away"]) for p in s["lineup"]}
    shown = [m for m in markets if m["week"] in (cur, 0) and m.get("player") in (None, *starters)]
    run_started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = {"updated": run_started,
            "league": league["settings"]["name"], "week": cur, "regWeeks": reg,
            "playoffTeams": league["settings"]["scheduleSettings"]["playoffTeamCount"],
            "sources": wk["sources"], "teams": teams, "matchups": wk["matchups"],
            "markets": shown,
            "futures": fut_table, "supabase": {"url": SB_URL, "anon": os.environ.get("SUPABASE_ANON_KEY", "")}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Semana {cur}: {len(wk['matchups'])} matchups, {len(markets)} mercados, fuentes {wk['sources']}")

    if SB_URL and SB_KEY:
        keys = ("id", "week", "grp", "label", "dec", "status", "result", "max_stake", "was_dec", "legs")
        slim = [{k: m.get(k) for k in keys} for m in markets]
        for i in range(0, len(slim), 500):
            sb("rpc/sync_markets", {"rows": slim[i:i + 500]})
        if extra:
            sb("rpc/sync_results", {"rows": extra})
        sb("rpc/close_stale", {"p_week": cur, "p_since": run_started})
        print("liquidados:", sb("rpc/settle_bets", {}))


if __name__ == "__main__":
    main()
