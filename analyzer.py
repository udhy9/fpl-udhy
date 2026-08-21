class FPLAnalyzer:
    def __init__(self, bootstrap_data):
        self.elements = {p["id"]: p for p in bootstrap_data["elements"]}
        self.teams = {t["id"]: t for t in bootstrap_data["teams"]}
        self.element_types = {et["id"]: et for et in bootstrap_data["element_types"]}

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

        final_xp = max(0.0, base_score * avail * team_strength * minutes_factor)
        return round(final_xp, 2)
