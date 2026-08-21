class FPLAnalyzer:
    def __init__(self, bootstrap_data):
        self.elements = {p["id"]: p for p in bootstrap_data["elements"]}
        self.teams = {t["id"]: t for t in bootstrap_data["teams"]}
        self.element_types = {et["id"]: et for et in bootstrap_data["element_types"]}

    def calculate_xp(self, player_id):
        player = self.elements.get(player_id)
        if not player:
            return 0.0

        # Availability & Injury penalty
        chance_playing = player.get("chance_of_playing_next_round")
        if chance_playing is not None and chance_playing < 100:
            availability_factor = chance_playing / 100.0
        elif player["status"] in ["i", "s", "u"]:
            availability_factor = 0.0
        elif player["status"] == "d":
            availability_factor = 0.5
        else:
            availability_factor = 1.0

        if availability_factor == 0.0:
            return 0.0

        def _num(value, default=0.0):
            if value is None or value == "":
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        form = _num(player.get("form"))
        ep_next = _num(player.get("ep_next"), form)
        threat = _num(player.get("threat")) / 100.0
        creativity = _num(player.get("creativity")) / 100.0

        team = self.teams.get(player["team"], {}) or {}
        team_strength = _num(team.get("strength"), 3.0)
        strength_factor = 1.0 + (team_strength - 3.0) * 0.05

        base_xp = (ep_next * 0.6) + (form * 0.2) + ((threat + creativity) * 0.2)
        final_xp = max(0.0, base_xp * availability_factor * strength_factor)
        return round(final_xp, 2)
