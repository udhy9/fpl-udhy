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

    @staticmethod
    def _num(value, default=0.0):
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def calculate_xp(self, player_id):
        player = self.elements.get(player_id)
        if not player:
            return 0.0

        chance_playing = player.get("chance_of_playing_next_round")
        if chance_playing is not None:
            avail = chance_playing / 100.0
        elif player["status"] in ["i", "s", "u"]:
            avail = 0.0
        elif player["status"] == "d":
            avail = 0.5
        else:
            avail = 1.0

        if avail == 0.0:
            return 0.0

        pos = player["element_type"]
        form = self._num(player.get("form"))
        ep_next = self._num(player.get("ep_next"), form)
        ict_index = self._num(player.get("ict_index")) / 10.0

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

        starts = int(self._num(player.get("starts")))
        minutes = int(self._num(player.get("minutes")))
        minutes_factor = 1.0 if minutes > 180 or starts >= 2 else 0.75

        return round(max(0.0, base_score * avail * team_strength * minutes_factor), 2)

    def load_fixtures(self, gameweek):
        try:
            res = requests.get(
                f"https://fantasy.premierleague.com/api/fixtures/?event={gameweek}",
                timeout=30,
                headers={"User-Agent": "fpl-udhy-agent"},
            )
            if res.status_code == 200:
                self.fixtures = res.json() or []
        except Exception as exc:
            print(f"Fixture fetch failed: {exc}")
            self.fixtures = []
        return self.fixtures

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
        for fx in self.fixtures:
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

        starting.sort(key=lambda pid: (self._pos(pid), -xp.get(pid, 0)))
        bench_gk = [pid for pid in bench if self._pos(pid) == 1]
        bench_out = [pid for pid in bench if self._pos(pid) != 1]
        bench_out.sort(key=lambda pid: -xp.get(pid, 0))
        bench = bench_gk + bench_out

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
            print("GEMINI_API_KEY not set. Using heuristic tactical layer.")
            return heuristic_plan, f"Heuristic tactics (set GEMINI_API_KEY for Gemini review). {heuristic_reason}"

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
You are an elite Fantasy Premier League manager.
Review this 15-man squad and baseline XI for the next gameweek.

SQUAD:
{json.dumps(squad_summary, indent=2)}

FIXTURES (FDR):
{json.dumps(fixture_summary, indent=2)}

BASELINE:
- Starting XI IDs: {baseline_plan['starting_xi']}
- Bench IDs: {baseline_plan['bench']}
- Captain ID: {baseline_plan['captain']}
- VC ID: {baseline_plan['vice_captain']}

RULES:
1. ANTI-CANNIBALIZATION: do not start defenders/GK whose opponent is a team we also start a premium attacker from.
2. FIXTURES: prefer easier FDR and healthy minutes.
3. INJURY/ROTATION: do not start status d/i/u or chance_of_playing < 75 if a healthy same-position bench option exists.
4. Respect overrides: {json.dumps(overrides)}
5. Use only the 15 squad IDs. Formation must be 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD.

Return ONLY JSON:
{{
  "starting_xi": [11 ids],
  "bench": [4 ids, GK first if present],
  "captain": id,
  "vice_captain": id,
  "tactical_reasoning": "short paragraph"
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
                    merged["starting_xi"] = [int(pid) for pid in tactical_result["starting_xi"]]
                    merged["bench"] = [int(pid) for pid in tactical_result["bench"]]
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

        return heuristic_plan, f"Baseline/heuristic selection. {heuristic_reason}"
