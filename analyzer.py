import csv
import io
import json
import os

import requests


class FPLAnalyzer:
    def __init__(self, bootstrap_data, fixtures=None):
        self.elements = {p["id"]: p for p in bootstrap_data["elements"]}
        self.teams = {t["id"]: t for t in bootstrap_data["teams"]}
        self.element_types = {et["id"]: et for et in bootstrap_data["element_types"]}
        self.events = bootstrap_data.get("events", [])
        self.fixtures = fixtures or []
        self.current_gw = self._infer_gameweek()
        self.fixture_horizon = 5
        self.fixture_multipliers = {}
        self.historical_points = {}

    @staticmethod
    def _num(value, default=0.0):
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _infer_gameweek(self):
        current = next((e for e in self.events if e.get("is_next")), None)
        if current is None:
            current = next((e for e in self.events if e.get("is_current") and not e.get("finished")), None)
        if current is None and self.events:
            current = self.events[0]
        return int(current["id"]) if current else 1

    def calculate_xmins(self, player):
        """Expected-minutes / starting-likelihood multiplier in [0.0, 1.0]."""
        status = player.get("status")
        chance = player.get("chance_of_playing_next_round")
        pos = player.get("element_type")
        if status in ("i", "s", "u"):
            return 0.0
        if chance is not None:
            chance = self._num(chance)
            if chance < 50:
                return 0.0
            if chance == 50:
                return 0.4
            if chance == 75:
                return 0.75

        starts = int(self._num(player.get("starts")))
        minutes = int(self._num(player.get("minutes")))
        selected_by = self._num(player.get("selected_by_percent"))
        now_cost = int(self._num(player.get("now_cost")))
        gw_num = self.current_gw or 1

        if pos == 1:
            if not self.is_likely_starting_gk(player["id"]):
                return 0.0
            if gw_num > 2 and starts == 0 and minutes == 0:
                return 0.0
            if selected_by < 1.0 and starts == 0:
                return 0.2
            return 1.0

        if gw_num > 2 and starts == 0 and minutes < 45:
            return 0.15

        if starts >= 1 or selected_by > 5.0:
            return 1.0
        if starts == 0 and minutes < 45 and selected_by < 2.0 and now_cost <= 45:
            return 0.2
        return 0.6

    def is_confirmed_starting_gk(self, player_id):
        player = self.elements.get(player_id)
        if not player or player.get("element_type") != 1:
            return False
        if self.should_not_start(player_id):
            return False
        return self.is_likely_starting_gk(player_id) and self.calculate_xmins(player) >= 1.0

    def historical_ppm(self, player):
        """Last-season points per match (Vaastav / FPL history_past / bootstrap PPG)."""
        total = self.historical_points.get(player.get("id"))
        if not total:
            past = player.get("history_past") or []
            if past:
                total = self._num(past[-1].get("total_points"))
        if total:
            return float(total) / 38.0
        return self._num(player.get("points_per_game"))

    def decay_hist_weight(self, gw=None):
        """50% historical prior at GW1, linear decay to 0 by GW6."""
        gw = int(gw or self.current_gw or 1)
        return max(0.0, 0.50 - (max(0, gw - 1) * 0.10))

    def load_historical_priors(self):
        """Load previous-season totals (Vaastav FPL dataset) keyed to current player IDs."""
        self.historical_points = {}
        text = ""
        for season in ("2025-26", "2024-25", "2023-24"):
            url = (
                "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
                f"master/data/{season}/cleaned_players.csv"
            )
            try:
                res = requests.get(url, timeout=20, headers={"User-Agent": "fpl-udhy-agent"})
                if res.status_code == 200 and "total_points" in res.text:
                    text = res.text
                    print(f"Loaded historical priors from Vaastav {season}.")
                    break
            except Exception as exc:
                print(f"Historical prior fetch skipped for {season}: {exc}")
        if not text:
            print("No Vaastav historical file loaded; decaying prior will use points_per_game.")
            return self.historical_points

        by_name = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            first = (row.get("first_name") or "").strip().lower()
            second = (row.get("second_name") or "").strip().lower()
            points = self._num(row.get("total_points"))
            if not second:
                continue
            by_name[second] = points
            by_name[f"{first} {second}"] = points
            if first:
                by_name[first] = points

        for pid, player in self.elements.items():
            web = (player.get("web_name") or "").strip().lower()
            full = f"{(player.get('first_name') or '').strip()} {(player.get('second_name') or '').strip()}".strip().lower()
            total = by_name.get(web) or by_name.get(full) or by_name.get((player.get("second_name") or "").strip().lower())
            if total:
                self.historical_points[pid] = total
        print(f"Mapped last-season totals for {len(self.historical_points)} players.")
        return self.historical_points

    def get_fixture_multipliers(self, current_gw=None):
        """DGW multiplier >= 2, BGW multiplier 0, otherwise 1."""
        gw = int(current_gw or self.current_gw or 1)
        counts = {tid: 0 for tid in self.teams}
        event_fixtures = [fx for fx in (self.fixtures or []) if fx.get("event") == gw]
        if not event_fixtures:
            try:
                res = requests.get(
                    f"https://fantasy.premierleague.com/api/fixtures/?event={gw}",
                    timeout=30,
                    headers={"User-Agent": "fpl-udhy-agent"},
                )
                if res.status_code == 200:
                    event_fixtures = res.json() or []
            except Exception as exc:
                print(f"DGW/BGW fixture fetch failed: {exc}")
                self.fixture_multipliers = {tid: 1 for tid in self.teams}
                return self.fixture_multipliers

        for fx in event_fixtures:
            home, away = fx.get("team_h"), fx.get("team_a")
            if home in counts:
                counts[home] += 1
            if away in counts:
                counts[away] += 1
        self.fixture_multipliers = counts
        doubles = sum(1 for n in counts.values() if n >= 2)
        blanks = sum(1 for n in counts.values() if n == 0)
        if doubles or blanks:
            print(f"GW {gw} fixture multipliers: {doubles} DGW teams, {blanks} BGW teams.")
        return self.fixture_multipliers

    def team_gw_multiplier(self, team_id, event_id=None):
        event_id = event_id or self.current_gw
        if event_id == self.current_gw and self.fixture_multipliers:
            return self.fixture_multipliers.get(team_id, 0)
        count = 0
        for fx in self.fixtures or []:
            if fx.get("event") != event_id:
                continue
            if fx.get("team_h") == team_id or fx.get("team_a") == team_id:
                count += 1
        return count if self.fixtures else 1

    def evaluate_chip_triggers(self, current_gw, squad_ids, fixture_multipliers=None):
        """Recommend Wildcard / Free Hit / Bench Boost / Triple Captain. Does not force Wildcard."""
        multipliers = fixture_multipliers if fixture_multipliers is not None else self.fixture_multipliers
        injured_starters = 0
        dgw_players = 0
        bgw_players = 0
        best_dgw_xp = 0.0
        for pid in squad_ids:
            player = self.elements.get(pid)
            if not player:
                continue
            if self.should_not_start(pid):
                injured_starters += 1
            mult = multipliers.get(player["team"], 1) if multipliers else 1
            if mult >= 2:
                dgw_players += 1
                best_dgw_xp = max(best_dgw_xp, self.calculate_xp(pid))
            elif multipliers and mult == 0:
                bgw_players += 1

        recommendation = None
        reason = "No chip trigger this week."
        if current_gw > 1 and injured_starters >= 3:
            recommendation = "wildcard"
            reason = f"{injured_starters} injured/unavailable squad players — consider Wildcard."
        elif bgw_players >= 4:
            recommendation = "freehit"
            reason = f"{bgw_players} blank-GW players — consider Free Hit."
        elif dgw_players >= 10:
            recommendation = "bboost"
            reason = f"{dgw_players} DGW squad players — Bench Boost is live."
        elif dgw_players >= 1 and best_dgw_xp > 14.0:
            recommendation = "3xc"
            reason = f"Premium DGW xP {best_dgw_xp:.1f} — Triple Captain is live."

        stats = {
            "injured_starters": injured_starters,
            "dgw_players": dgw_players,
            "bgw_players": bgw_players,
            "best_dgw_xp": best_dgw_xp,
            "reason": reason,
        }
        return recommendation, stats

    def attacking_defender_score(self, player):
        threat = self._num(player.get("threat")) / 100.0
        creativity = self._num(player.get("creativity")) / 100.0
        xg = self._num(player.get("expected_goals"))
        xa = self._num(player.get("expected_assists"))
        xgi = self._num(player.get("expected_goal_involvements"), xg + xa)
        return (threat * 1.2) + (creativity * 1.0) + (xgi * 1.5)

    def is_attacking_or_template_def(self, player_id):
        player = self.elements.get(player_id)
        if not player or player.get("element_type") != 2:
            return False
        if self.should_not_start(player_id) or self.is_fringe(player_id):
            return False
        selected_by = self._num(player.get("selected_by_percent"))
        return selected_by >= 8.0 or self.attacking_defender_score(player) >= 0.8

    def is_fringe(self, player_id):
        player = self.elements.get(player_id)
        if not player:
            return True
        return self.calculate_xmins(player) < 0.4

    def fixture_factor(self, player_id, horizon=None):
        """Rolling FDR multiplier for the next N gameweeks. Easier run → higher factor."""
        horizon = horizon or self.fixture_horizon
        player = self.elements.get(player_id)
        if not player or not self.fixtures:
            return 1.0
        team_id = player["team"]
        gw = self.current_gw or 1
        fdrs = []
        for fx in self.fixtures:
            event = fx.get("event")
            if event is None or event < gw or event >= gw + horizon:
                continue
            if fx.get("finished"):
                continue
            if fx.get("team_h") == team_id:
                fdrs.append(self._num(fx.get("team_h_difficulty"), 3.0))
            elif fx.get("team_a") == team_id:
                fdrs.append(self._num(fx.get("team_a_difficulty"), 3.0))
        if not fdrs:
            return 1.0
        avg_fdr = sum(fdrs) / len(fdrs)
        return round(max(0.7, min(1.3, 1.0 + (3.0 - avg_fdr) * 0.1)), 3)

    def fixture_factor_for_event(self, player_id, event_id):
        player = self.elements.get(player_id)
        if not player or not self.fixtures:
            return 1.0
        team_id = player["team"]
        for fx in self.fixtures:
            if fx.get("event") != event_id or fx.get("finished"):
                continue
            if fx.get("team_h") == team_id:
                fdr = self._num(fx.get("team_h_difficulty"), 3.0)
            elif fx.get("team_a") == team_id:
                fdr = self._num(fx.get("team_a_difficulty"), 3.0)
            else:
                continue
            return round(max(0.7, min(1.3, 1.0 + (3.0 - fdr) * 0.1)), 3)
        return 1.0

    def _unfdr_xp(self, player_id):
        player = self.elements.get(player_id)
        if not player:
            return 0.0
        xmins_factor = self.calculate_xmins(player)
        if xmins_factor == 0.0:
            return 0.0

        pos = player["element_type"]
        form = self._num(player.get("form"))
        ep_next = self._num(player.get("ep_next"), form)
        ict_index = self._num(player.get("ict_index")) / 10.0
        selected_by = self._num(player.get("selected_by_percent"))
        xg = self._num(player.get("expected_goals"))
        xa = self._num(player.get("expected_assists"))
        xgi = self._num(player.get("expected_goal_involvements"), xg + xa)

        if pos == 1:
            in_season_score = (ep_next * 0.5) + (form * 0.3) + 1.5
        elif pos == 2:
            attacking_potential = self.attacking_defender_score(player)
            in_season_score = (ep_next * 0.40) + (form * 0.20) + attacking_potential + 1.0
        elif pos == 3:
            in_season_score = (ep_next * 0.4) + (form * 0.25) + (xgi * 1.2) + (ict_index * 0.2)
        else:
            in_season_score = (ep_next * 0.4) + (form * 0.25) + (xgi * 1.4) + (ict_index * 0.2)

        hist_weight = self.decay_hist_weight()
        hist_prior = self.historical_ppm(player)
        if hist_prior <= 0:
            hist_prior = in_season_score
        blended_base = ((1.0 - hist_weight) * in_season_score) + (hist_weight * hist_prior)

        team = self.teams.get(player["team"], {}) or {}
        home_str = self._num(team.get("strength_overall_home"), 1100.0)
        away_str = self._num(team.get("strength_overall_away"), 1100.0)
        def_home = self._num(team.get("strength_defence_home"), 1100.0)
        def_away = self._num(team.get("strength_defence_away"), 1100.0)
        if pos in (1, 2):
            team_strength = ((def_home + def_away) / 2.0) / 1100.0
        else:
            team_strength = ((home_str + away_str) / 2.0) / 1100.0
        ownership_weight = 1.0 + min(0.20, (selected_by / 100.0) * 0.5)
        if pos == 2 and selected_by >= 10.0:
            ownership_weight += 0.05
        return max(0.0, blended_base * xmins_factor * team_strength * ownership_weight)

    def horizon_xp(self, player_id, weeks=3):
        """Sum of next N single-GW xP estimates using that week's FDR."""
        base = self._unfdr_xp(player_id)
        if base == 0.0:
            return 0.0
        gw = self.current_gw or 1
        total = 0.0
        for event_id in range(gw, gw + weeks):
            player = self.elements.get(player_id) or {}
            total += base * self.fixture_factor_for_event(player_id, event_id) * max(
                self.team_gw_multiplier(player.get("team"), event_id), 0
            )
        return round(total, 2)

    def calculate_xp(self, player_id, current_gw=None, fixture_multipliers=None):
        if current_gw is not None:
            self.current_gw = int(current_gw)
        if fixture_multipliers is not None:
            self.fixture_multipliers = fixture_multipliers
        player = self.elements.get(player_id)
        base = self._unfdr_xp(player_id)
        if base == 0.0 or not player:
            return 0.0
        mult = self.team_gw_multiplier(player["team"])
        return round(base * self.fixture_factor(player_id) * mult, 2)

    def playing_chance(self, player_id):
        player = self.elements.get(player_id) or {}
        chance = player.get("chance_of_playing_next_round")
        if chance is not None:
            return int(chance)
        if player.get("status") in ("i", "s", "u"):
            return 0
        if player.get("status") == "d":
            return 50
        return 100

    def is_unavailable(self, player_id):
        player = self.elements.get(player_id) or {}
        return player.get("status") in ("i", "s", "u") or self.playing_chance(player_id) == 0

    def should_not_start(self, player_id):
        player = self.elements.get(player_id) or {}
        if player.get("status") in ("i", "s", "u"):
            return True
        chance = player.get("chance_of_playing_next_round")
        if chance is not None and chance <= 50:
            return True
        return player.get("status") == "d"

    def is_likely_starting_gk(self, player_id):
        player = self.elements.get(player_id)
        if not player or player.get("element_type") != 1:
            return False
        team_gks = [
            gk for gk in self.elements.values()
            if gk.get("element_type") == 1 and gk.get("team") == player["team"]
        ]

        def gk_key(gk):
            unavailable = 1 if gk.get("status") in ("i", "s", "u") else 0
            chance = gk.get("chance_of_playing_next_round")
            if chance is None:
                chance = 0 if unavailable else 100
            return (
                -unavailable,
                int(chance),
                self._num(gk.get("ep_next")),
                self._num(gk.get("selected_by_percent")),
                int(gk.get("now_cost") or 0),
                int(gk.get("minutes") or 0),
            )

        starter = max(team_gks, key=gk_key)
        return starter["id"] == player_id

    def sub_priority(self, player_id, xp=0.0):
        """Higher value = earlier outfield bench slot (first auto-sub)."""
        player = self.elements.get(player_id) or {}
        if player.get("status") in ("i", "s", "u"):
            return -100.0
        chance = player.get("chance_of_playing_next_round")
        if (chance is not None and chance <= 50) or player.get("status") == "d":
            return -50.0 + float(xp or 0.0)
        if self.is_fringe(player_id):
            return -10.0 + float(xp or 0.0)
        return float(xp or 0.0)

    def order_bench(self, bench, squad_xp):
        bench_gk = [pid for pid in bench if self._pos(pid) == 1]
        bench_gk.sort(key=lambda pid: -float(squad_xp.get(pid) or 0.0))
        bench_outfield = [pid for pid in bench if self._pos(pid) != 1]
        bench_outfield.sort(
            key=lambda pid: self.sub_priority(pid, squad_xp.get(pid, 0.0)),
            reverse=True,
        )
        return bench_gk + bench_outfield

    def load_fixture_horizon(self, gameweek, horizon=5):
        self.current_gw = int(gameweek)
        self.fixture_horizon = horizon
        try:
            res = requests.get(
                "https://fantasy.premierleague.com/api/fixtures/",
                timeout=30,
                headers={"User-Agent": "fpl-udhy-agent"},
            )
            if res.status_code == 200:
                all_fixtures = res.json() or []
                self.fixtures = [
                    fx for fx in all_fixtures
                    if fx.get("event") and gameweek <= fx["event"] < gameweek + horizon
                ]
                print(f"Loaded {len(self.fixtures)} fixtures for GW {gameweek}-{gameweek + horizon - 1}.")
        except Exception as exc:
            print(f"Fixture horizon fetch failed: {exc}")
            self.fixtures = []
        self.get_fixture_multipliers(gameweek)
        return self.fixtures

    def load_fixtures(self, gameweek):
        return self.load_fixture_horizon(gameweek, horizon=self.fixture_horizon)

    def _pos(self, pid):
        return self.elements[pid]["element_type"]

    def _is_risky(self, pid):
        player = self.elements[pid]
        chance = player.get("chance_of_playing_next_round")
        if player.get("status") in ("i", "s", "u"):
            return True
        if player.get("status") == "d":
            return True
        if chance is not None and chance < 75:
            return True
        return False

    def _formation_ok(self, starting_xi):
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for pid in starting_xi:
            counts[self._pos(pid)] += 1
        return (
            counts[1] == 1
            and 3 <= counts[2] <= 5
            and 2 <= counts[3] <= 5
            and 1 <= counts[4] <= 3
            and len(starting_xi) == 11
        )

    def _opponent_map(self):
        mapping = {}
        gw = self.current_gw
        for fx in self.fixtures:
            if gw and fx.get("event") not in (None, gw):
                continue
            home, away = fx.get("team_h"), fx.get("team_a")
            if home and away:
                mapping[home] = away
                mapping[away] = home
        return mapping

    def apply_heuristic_tactics(self, plan):
        starting = list(plan["starting_xi"])
        bench = list(plan["bench"])
        xp = plan["squad_xp"]
        notes = []
        opponents = self._opponent_map()

        attackers = [
            pid for pid in starting
            if self._pos(pid) in (3, 4) and not self._is_risky(pid)
        ]
        attacker_teams = {self.elements[pid]["team"] for pid in attackers}

        clash_defs = []
        for pid in starting:
            if self._pos(pid) not in (1, 2):
                continue
            opp = opponents.get(self.elements[pid]["team"])
            if opp in attacker_teams:
                clash_defs.append(pid)

        for pid in clash_defs:
            replacements = [
                bid for bid in bench
                if self._pos(bid) == self._pos(pid) and bid not in clash_defs and not self._is_risky(bid)
            ]
            replacements.sort(key=lambda x: xp.get(x, 0), reverse=True)
            if not replacements:
                continue
            swap = replacements[0]
            starting.remove(pid)
            starting.append(swap)
            bench.remove(swap)
            bench.append(pid)
            notes.append(
                f"Benched {self.elements[pid]['web_name']} to avoid CS cannibalization vs our attackers; "
                f"started {self.elements[swap]['web_name']}."
            )

        for pid in list(starting):
            if not self._is_risky(pid):
                continue
            replacements = [
                bid for bid in bench
                if self._pos(bid) == self._pos(pid) and not self._is_risky(bid)
            ]
            replacements.sort(key=lambda x: xp.get(x, 0), reverse=True)
            if not replacements:
                continue
            swap = replacements[0]
            starting.remove(pid)
            starting.append(swap)
            bench.remove(swap)
            bench.append(pid)
            notes.append(
                f"Benched flagged {self.elements[pid]['web_name']} for healthy {self.elements[swap]['web_name']}."
            )

        for pid in list(starting):
            if not self.is_fringe(pid) or self._pos(pid) == 1:
                continue
            replacements = [
                bid for bid in bench
                if not self.is_fringe(bid) and not self._is_risky(bid) and self._pos(bid) != 1
            ]
            replacements.sort(key=lambda x: xp.get(x, 0), reverse=True)
            swapped = False
            for swap in replacements:
                trial = [sid for sid in starting if sid != pid] + [swap]
                if not self._formation_ok(trial):
                    continue
                starting.remove(pid)
                starting.append(swap)
                bench.remove(swap)
                bench.append(pid)
                notes.append(
                    f"Benched fringe {self.elements[pid]['web_name']} (low xMins); "
                    f"started nailed {self.elements[swap]['web_name']}."
                )
                swapped = True
                break
            if swapped:
                continue

        has_attacking_def = any(
            self.is_attacking_or_template_def(pid) for pid in starting if self._pos(pid) == 2
        )
        if not has_attacking_def:
            bench_defs = [
                bid for bid in bench
                if self.is_attacking_or_template_def(bid)
            ]
            weak_defs = [
                pid for pid in starting
                if self._pos(pid) == 2 and not self.is_attacking_or_template_def(pid)
            ]
            weak_defs.sort(key=lambda pid: xp.get(pid, 0.0))
            bench_defs.sort(key=lambda pid: xp.get(pid, 0.0), reverse=True)
            if bench_defs and weak_defs:
                swap_in, swap_out = bench_defs[0], weak_defs[0]
                starting.remove(swap_out)
                starting.append(swap_in)
                bench.remove(swap_in)
                bench.append(swap_out)
                notes.append(
                    f"Started attacking/template defender {self.elements[swap_in]['web_name']}; "
                    f"benched {self.elements[swap_out]['web_name']}."
                )

        start_gks = [pid for pid in starting if self._pos(pid) == 1]
        bench_gks = [pid for pid in bench if self._pos(pid) == 1]
        if start_gks and bench_gks:
            start_gk, bench_gk = start_gks[0], bench_gks[0]
            swap_gk = False
            if self.is_confirmed_starting_gk(bench_gk) and not self.is_confirmed_starting_gk(start_gk):
                swap_gk = True
            elif self.is_confirmed_starting_gk(start_gk) and self.is_confirmed_starting_gk(bench_gk):
                if self.fixture_factor(bench_gk) > self.fixture_factor(start_gk) + 0.02:
                    swap_gk = True
            if swap_gk:
                starting.remove(start_gk)
                starting.append(bench_gk)
                bench.remove(bench_gk)
                bench.append(start_gk)
                notes.append(
                    f"Started first-choice GK {self.elements[bench_gk]['web_name']} "
                    f"(easier CS / confirmed starter); benched {self.elements[start_gk]['web_name']}."
                )

        starting.sort(key=lambda pid: (self._pos(pid), -xp.get(pid, 0)))
        bench = self.order_bench(bench, xp)

        if not self._formation_ok(starting):
            return plan, "Heuristic tactics skipped (would break formation)."

        ranked = sorted(starting, key=lambda pid: -xp.get(pid, 0))
        plan = dict(plan)
        plan["starting_xi"] = starting
        plan["bench"] = bench
        if plan["captain"] not in starting:
            plan["captain"] = ranked[0]
        if plan["vice_captain"] not in starting or plan["vice_captain"] == plan["captain"]:
            plan["vice_captain"] = ranked[1] if ranked[1] != plan["captain"] else ranked[0]
        reasoning = " ".join(notes) if notes else "No clash/injury swaps required; kept solver XI."
        return plan, reasoning

    def _valid_llm_plan(self, tactical, squad_ids):
        xi = [int(pid) for pid in tactical.get("starting_xi", [])]
        bench = [int(pid) for pid in tactical.get("bench", [])]
        squad = set(squad_ids)
        if len(xi) != 11 or len(bench) != 4:
            return False
        if set(xi + bench) != squad:
            return False
        if not self._formation_ok(xi):
            return False
        cap = int(tactical.get("captain"))
        vc = int(tactical.get("vice_captain"))
        if cap not in xi or vc not in xi or cap == vc:
            return False
        return True

    def run_llm_tactical_review(self, baseline_plan, squad_ids, overrides, gameweek=None, ft_available=1):
        if gameweek:
            self.load_fixtures(gameweek)

        heuristic_plan, heuristic_reason = self.apply_heuristic_tactics(baseline_plan)
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("GEMINI_API_KEY not set. Using xMins heuristic tactical layer.")
            return heuristic_plan, f"Quantitative xMins & statistical solver. {heuristic_reason}"

        squad_summary = []
        for pid in squad_ids:
            player = self.elements[pid]
            team = self.teams.get(player["team"], {})
            squad_summary.append({
                "id": pid,
                "name": player["web_name"],
                "position": self.element_types[player["element_type"]]["singular_name_short"],
                "team": team.get("short_name", ""),
                "news": player.get("news", ""),
                "chance_of_playing": player.get("chance_of_playing_next_round"),
                "status": player.get("status"),
                "selected_by_%": player.get("selected_by_percent"),
                "xmins": self.calculate_xmins(player),
                "fringe_or_youth": self.is_fringe(pid),
                "threat": player.get("threat"),
                "creativity": player.get("creativity"),
                "attacking_or_template_def": self.is_attacking_or_template_def(pid),
                "baseline_xp": baseline_plan["squad_xp"].get(pid, 0.0),
            })

        fixture_summary = []
        for fx in self.fixtures:
            home = self.teams.get(fx.get("team_h"), {})
            away = self.teams.get(fx.get("team_a"), {})
            fixture_summary.append({
                "home": home.get("short_name"),
                "away": away.get("short_name"),
                "home_fdr": fx.get("team_h_difficulty"),
                "away_fdr": fx.get("team_a_difficulty"),
            })

        prompt = f"""
You are an elite FPL analyst combining statistical EV, 5-GW FDR, xMins, and live press-conference intelligence.
Free Transfers available: {ft_available} (FPL cap 5; bank unused FTs toward a mini-wildcard).

SQUAD:
{json.dumps(squad_summary, indent=2)}

FIXTURES (next {self.fixture_horizon} GWs):
{json.dumps(fixture_summary, indent=2)}

BASELINE:
{json.dumps({
    "starting_xi": baseline_plan["starting_xi"],
    "bench": baseline_plan["bench"],
    "captain": baseline_plan["captain"],
    "vice_captain": baseline_plan["vice_captain"],
    "transfers_in": baseline_plan.get("transfers_in") or [],
    "transfers_out": baseline_plan.get("transfers_out") or [],
    "bank_transfer": baseline_plan.get("bank_transfer"),
    "chip_recommendation": baseline_plan.get("chip_recommendation"),
    "chip_meta": baseline_plan.get("chip_meta"),
    "chip": baseline_plan.get("chip"),
}, indent=2)}

TACTICAL OBJECTIVES:
1. DEFENSE COMPOSITION: start at least one attacking/high-ownership defender (threat/creativity/xGI or template ownership). Do not start fringe bench defenders if a nailed mid/fwd has a higher ceiling.
2. STARTER INTEGRITY & ANTI-CANNIBALIZATION: 11 nailed 90-min starters. Never start fringe/youth (Lucky, Ramsay). Never start 3 defenders against our own starting striker.
3. PRESS CONFERENCE: use each player's news/status/chance_of_playing. Bench anyone the presser suggests is rotated, doubtful, or ruled out.
4. FREE TRANSFER STRATEGY: only spend a transfer if a starter is long-term injured / ~0 xMins, or the move is clearly +EV. Otherwise set bank_transfer_recommendation=true to accumulate 2-5 FTs.
5. CHIP STRATEGY: respect the automated recommendation `{baseline_plan.get("chip_recommendation")}` ({(baseline_plan.get("chip_meta") or {}).get("reason")}). Only play Triple Captain / Bench Boost this week if that recommendation is 3xc or bboost. Do not activate Wildcard.
6. BENCH SAFETY: GK first, healthy nailed 1st outfield sub, injured/doubtful last.
7. Respect overrides: {json.dumps(overrides)}
8. Use only the 15 squad IDs. Formation: 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD.

Return ONLY JSON:
{{
  "starting_xi": [11 ids],
  "bench": [4 ids, GK first if present],
  "captain": id,
  "vice_captain": id,
  "bank_transfer_recommendation": true,
  "tactical_reasoning": "short paragraph covering presser flags, formation, captaincy, and whether FTs are banked or spent"
}}
"""
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            last_error = None
            for model_name in ("gemini-2.5-flash", "gemini-2.0-flash"):
                try:
                    response = client.models.generate_content(model=model_name, contents=prompt)
                    raw_text = (response.text or "").replace("```json", "").replace("```", "").strip()
                    tactical_result = json.loads(raw_text)
                    if not self._valid_llm_plan(tactical_result, squad_ids):
                        last_error = "Gemini returned an invalid XI/bench."
                        continue
                    merged = dict(baseline_plan)
                    merged["starting_xi"] = sorted(
                        [int(pid) for pid in tactical_result["starting_xi"]],
                        key=lambda pid: self._pos(pid),
                    )
                    merged["bench"] = self.order_bench(
                        [int(pid) for pid in tactical_result["bench"]],
                        merged["squad_xp"],
                    )
                    merged["captain"] = int(tactical_result["captain"])
                    merged["vice_captain"] = int(tactical_result["vice_captain"])
                    bank_rec = bool(tactical_result.get("bank_transfer_recommendation"))
                    merged["llm_bank_transfer"] = bank_rec
                    reasoning = tactical_result.get("tactical_reasoning") or "Gemini press-conference review applied."
                    if bank_rec and not merged.get("unlimited_transfers"):
                        forced = False
                        for out_id in merged.get("transfers_out") or []:
                            if self.should_not_start(out_id) or self.calculate_xmins(self.elements[out_id]) == 0.0:
                                forced = True
                                break
                        if not forced and (merged.get("transfers_in") or merged.get("transfers_out")):
                            mapping = dict(zip(merged.get("transfers_in") or [], merged.get("transfers_out") or []))

                            def remap(pid):
                                return mapping.get(pid, pid)
                            merged["starting_xi"] = [remap(pid) for pid in merged["starting_xi"]]
                            merged["bench"] = self.order_bench([remap(pid) for pid in merged["bench"]], merged["squad_xp"])
                            merged["captain"] = remap(merged["captain"])
                            merged["vice_captain"] = remap(merged["vice_captain"])
                            if merged["captain"] == merged["vice_captain"]:
                                for pid in merged["starting_xi"]:
                                    if pid != merged["captain"]:
                                        merged["vice_captain"] = pid
                                        break
                            merged["transfers_in"] = []
                            merged["transfers_out"] = []
                            merged["bank_transfer"] = True
                            merged["transfer_strategy"] = (
                                f"Gemini press-conference review banked FTs "
                                f"({ft_available}/5). {merged.get('transfer_strategy') or ''}"
                            ).strip()
                            reasoning = f"Banked transfer after press-conference review. {reasoning}"
                    return merged, reasoning
                except Exception as exc:
                    last_error = exc
                    continue
            print(f"LLM tactical review failed: {last_error}. Using heuristic fallback.")
        except Exception as exc:
            print(f"LLM tactical review failed: {exc}. Using heuristic fallback.")

        return heuristic_plan, f"Quantitative xMins optimization applied. {heuristic_reason}"
