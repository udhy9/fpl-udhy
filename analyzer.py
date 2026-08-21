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

        if player.get("element_type") == 1 and not self.is_likely_starting_gk(player["id"]):
            return 0.15

        if gw_num > 2 and starts == 0 and minutes < 45:
            return 0.15

        if starts >= 1 or selected_by > 5.0:
            return 1.0
        if starts == 0 and minutes < 45 and selected_by < 2.0 and now_cost <= 45:
            return 0.2
        return 0.5

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

    def calculate_xp(self, player_id):
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
            base_score = (ep_next * 0.5) + (form * 0.3) + 1.5
        elif pos == 2:
            base_score = (ep_next * 0.45) + (form * 0.25) + (xgi * 0.8) + 1.0
        elif pos == 3:
            base_score = (ep_next * 0.4) + (form * 0.25) + (xgi * 1.2) + (ict_index * 0.2)
        else:
            base_score = (ep_next * 0.4) + (form * 0.25) + (xgi * 1.4) + (ict_index * 0.2)

        team = self.teams.get(player["team"], {}) or {}
        home_str = self._num(team.get("strength_overall_home"), 1100.0)
        away_str = self._num(team.get("strength_overall_away"), 1100.0)
        def_home = self._num(team.get("strength_defence_home"), 1100.0)
        def_away = self._num(team.get("strength_defence_away"), 1100.0)
        if pos in (1, 2):
            team_strength = ((def_home + def_away) / 2.0) / 1100.0
        else:
            team_strength = ((home_str + away_str) / 2.0) / 1100.0

        ownership_bonus = 1.0 + min(0.15, selected_by / 100.0)
        fdr_factor = self.fixture_factor(player_id)
        return round(max(0.0, base_score * xmins_factor * team_strength * ownership_bonus * fdr_factor), 2)

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

    def run_llm_tactical_review(self, baseline_plan, squad_ids, overrides, gameweek=None):
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
You are an expert FPL manager using xMins, ownership, and a 5-GW FDR horizon.
Eliminate non-starting youth/fodder from the Starting XI and prefer nailed 90-minute players.

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
}, indent=2)}

TACTICAL OBJECTIVES:
1. STARTER INTEGRITY: never start fringe/youth/non-playing options (low xmins / fringe_or_youth true, e.g. Lucky, Ramsay) if a nailed midfielder or forward can start instead.
2. FORMATION FLEXIBILITY: use 3-4-3, 3-5-2, or 4-4-2 so all 11 outfield starters are nailed 90-minute options.
3. BENCH SAFETY: GK first, then healthy nailed subs, then doubtful/injured last.
4. ANTI-CANNIBALIZATION: do not start defenders/GK whose opponent is a team we also start a premium attacker from.
5. Respect overrides: {json.dumps(overrides)}
6. Use only the 15 squad IDs. Formation must be 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD.

Return ONLY JSON:
{{
  "starting_xi": [11 ids],
  "bench": [4 ids, GK first if present],
  "captain": id,
  "vice_captain": id,
  "tactical_reasoning": "short paragraph covering formation, non-starters benched, and captaincy"
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
                    reasoning = tactical_result.get("tactical_reasoning") or "Gemini tactical review applied."
                    return merged, reasoning
                except Exception as exc:
                    last_error = exc
                    continue
            print(f"LLM tactical review failed: {last_error}. Using heuristic fallback.")
        except Exception as exc:
            print(f"LLM tactical review failed: {exc}. Using heuristic fallback.")

        return heuristic_plan, f"Quantitative xMins optimization applied. {heuristic_reason}"
