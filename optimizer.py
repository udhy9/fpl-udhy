import pulp


class FPLOptimizer:
    def __init__(self, analyzer, my_team_data, bootstrap_data, overrides, manual_locks=None):
        self.analyzer = analyzer
        self.my_team_data = my_team_data
        self.elements = analyzer.elements
        self.overrides = overrides
        self.manual_locks = manual_locks or []
        self.current_picks = [p["element"] for p in my_team_data.get("picks", [])]
        self.free_transfers = my_team_data.get("transfers", {}).get("limit", 1)

    def optimize(self):
        current_squad_ids = self.current_picks
        must_start = self.overrides.get("must_start", [])
        must_bench = self.overrides.get("must_bench", [])
        lock_cap = self.overrides.get("lock_captain", "").strip().lower()
        lock_vc = self.overrides.get("lock_vice_captain", "").strip().lower()

        # Score squad
        squad_xp = {pid: self.analyzer.calculate_xp(pid) for pid in current_squad_ids}

        # Solve Starting XI (11 players, valid formation: 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD)
        prob = pulp.LpProblem("FPL_Lineup", pulp.LpMaximize)
        start_vars = {pid: pulp.LpVariable(f"start_{pid}", cat=pulp.LpBinary) for pid in current_squad_ids}

        prob += pulp.lpSum([start_vars[pid] * squad_xp[pid] for pid in current_squad_ids])
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids]) == 11

        pos_map = {pid: self.elements[pid]["element_type"] for pid in current_squad_ids}

        # Position bounds: 1=GK, 2=DEF, 3=MID, 4=FWD
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 1]) == 1
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) >= 3
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) <= 5
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) >= 2
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) <= 5
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) >= 1
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) <= 3

        # Must start / bench overrides
        for pid in current_squad_ids:
            name = self.elements[pid]["web_name"].lower()
            if any(ms.lower() in name for ms in must_start) or pid in self.manual_locks:
                prob += start_vars[pid] == 1
            if any(mb.lower() in name for mb in must_bench):
                prob += start_vars[pid] == 0

        prob.solve(pulp.PULP_CBC_CMD(msg=False))

        starting_xi = [pid for pid in current_squad_ids if pulp.value(start_vars[pid]) == 1]
        bench = [pid for pid in current_squad_ids if pid not in starting_xi]

        # Order Starting XI: GK, DEF, MID, FWD
        starting_xi.sort(key=lambda pid: (pos_map[pid], -squad_xp[pid]))

        # Order Bench: Sub GK first, then field players by descending xP
        bench_gk = [pid for pid in bench if pos_map[pid] == 1]
        bench_outfield = [pid for pid in bench if pos_map[pid] != 1]
        bench_outfield.sort(key=lambda pid: -squad_xp[pid])
        ordered_bench = bench_gk + bench_outfield

        # Captaincy Selection
        ranked_starters = sorted(starting_xi, key=lambda pid: -squad_xp[pid])
        cap_id = ranked_starters[0]
        vc_id = ranked_starters[1]

        if lock_cap:
            for pid in starting_xi:
                if lock_cap in self.elements[pid]["web_name"].lower():
                    cap_id = pid
                    break
        if lock_vc:
            for pid in starting_xi:
                if lock_vc in self.elements[pid]["web_name"].lower() and pid != cap_id:
                    vc_id = pid
                    break

        return {
            "starting_xi": starting_xi,
            "bench": ordered_bench,
            "captain": cap_id,
            "vice_captain": vc_id,
            "squad_xp": squad_xp
        }
