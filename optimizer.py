from collections import defaultdict

import pulp


class FPLOptimizer:
    def __init__(self, analyzer, my_team_data, bootstrap_data, overrides, manual_locks=None, gameweek=1):
        self.analyzer = analyzer
        self.my_team_data = my_team_data
        self.elements = analyzer.elements
        self.overrides = overrides
        self.manual_locks = manual_locks or []
        self.gameweek = gameweek
        self.current_picks = [p["element"] for p in my_team_data.get("picks", [])]
        self.selling_price = {
            p["element"]: p.get("selling_price") or self.elements[p["element"]]["now_cost"]
            for p in my_team_data.get("picks", [])
        }
        self.bank = my_team_data.get("transfers", {}).get("bank", 0)
        self.free_transfers = my_team_data.get("transfers", {}).get("limit", 1)

    def _max_transfers(self):
        if not self.overrides.get("allow_transfers", True):
            return 0
        chips = self.my_team_data.get("chips") or []
        wildcard_active = any(
            c.get("name") == "wildcard" and c.get("status_for_entry") == "active"
            for c in chips
        )
        # GW1 is unlimited free transfers. Do not play the Wildcard chip.
        if self.gameweek == 1 or wildcard_active:
            return 15
        return max(0, int(self.free_transfers or 0))

    def _name_matches(self, pid, names):
        name = self.elements[pid]["web_name"].lower()
        return any(n.lower() in name for n in names if n)

    def _can_transfer_in(self, player):
        if player.get("status") in ("u", "i", "s"):
            return False
        chance = player.get("chance_of_playing_next_round")
        if chance is not None and chance <= 50:
            return False
        if player.get("status") == "d":
            return False
        if player.get("element_type") == 1 and not self.analyzer.is_likely_starting_gk(player["id"]):
            return False
        return True

    def _optimize_squad(self, max_transfers):
        current = set(self.current_picks)
        candidates = [
            pid for pid, player in self.elements.items()
            if pid in current or self._can_transfer_in(player)
        ]
        xp = {pid: self.analyzer.calculate_xp(pid) for pid in candidates}
        budget = self.bank + sum(self.selling_price.get(pid, 0) for pid in current)
        cost = {
            pid: self.selling_price[pid] if pid in current else self.elements[pid]["now_cost"]
            for pid in candidates
        }

        prob = pulp.LpProblem("FPL_Squad", pulp.LpMaximize)
        take = {pid: pulp.LpVariable(f"squad_{pid}", cat=pulp.LpBinary) for pid in candidates}
        prob += pulp.lpSum([take[pid] * xp[pid] for pid in candidates])
        prob += pulp.lpSum([take[pid] for pid in candidates]) == 15
        prob += pulp.lpSum([take[pid] * cost[pid] for pid in candidates]) <= budget

        pos = {pid: self.elements[pid]["element_type"] for pid in candidates}
        prob += pulp.lpSum([take[pid] for pid in candidates if pos[pid] == 1]) == 2
        prob += pulp.lpSum([take[pid] for pid in candidates if pos[pid] == 2]) == 5
        prob += pulp.lpSum([take[pid] for pid in candidates if pos[pid] == 3]) == 5
        prob += pulp.lpSum([take[pid] for pid in candidates if pos[pid] == 4]) == 3

        teams = defaultdict(list)
        gks_by_team = defaultdict(list)
        for pid in candidates:
            teams[self.elements[pid]["team"]].append(pid)
            if pos[pid] == 1:
                gks_by_team[self.elements[pid]["team"]].append(pid)
        for team_pids in teams.values():
            prob += pulp.lpSum([take[pid] for pid in team_pids]) <= 3
        for team_gks in gks_by_team.values():
            prob += pulp.lpSum([take[pid] for pid in team_gks]) <= 1

        if current:
            prob += pulp.lpSum([take[pid] for pid in current if pid in take]) >= 15 - max_transfers

        must_keep = list(self.manual_locks) + [
            pid for pid in current if self._name_matches(pid, self.overrides.get("must_start", []))
        ]
        for pid in must_keep:
            if pid in take and not self.analyzer.should_not_start(pid):
                prob += take[pid] == 1

        status = prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=30))
        if pulp.LpStatus[status] != "Optimal":
            print(f"Squad solver status={pulp.LpStatus[status]}; keeping current 15.")
            return list(self.current_picks), xp

        return [pid for pid in candidates if pulp.value(take[pid]) == 1], xp

    def _pair_transfers(self, old_squad, new_squad):
        old_set, new_set = set(old_squad), set(new_squad)
        outs = [pid for pid in old_squad if pid not in new_set]
        ins = [pid for pid in new_squad if pid not in old_set]
        outs_by_pos = defaultdict(list)
        ins_by_pos = defaultdict(list)
        for pid in outs:
            outs_by_pos[self.elements[pid]["element_type"]].append(pid)
        for pid in ins:
            ins_by_pos[self.elements[pid]["element_type"]].append(pid)
        transfers_out, transfers_in = [], []
        for position in (1, 2, 3, 4):
            for out_id, in_id in zip(outs_by_pos[position], ins_by_pos[position]):
                transfers_out.append(out_id)
                transfers_in.append(in_id)
        return transfers_in, transfers_out

    def optimize(self):
        max_transfers = self._max_transfers()
        current_squad_ids = list(self.current_picks)
        if max_transfers > 0:
            current_squad_ids, squad_xp_all = self._optimize_squad(max_transfers)
        else:
            squad_xp_all = {pid: self.analyzer.calculate_xp(pid) for pid in current_squad_ids}

        transfers_in, transfers_out = self._pair_transfers(self.current_picks, current_squad_ids)
        must_start = self.overrides.get("must_start", [])
        must_bench = self.overrides.get("must_bench", [])
        lock_cap = self.overrides.get("lock_captain", "").strip().lower()
        lock_vc = self.overrides.get("lock_vice_captain", "").strip().lower()

        squad_xp = {pid: squad_xp_all.get(pid, self.analyzer.calculate_xp(pid)) for pid in current_squad_ids}

        prob = pulp.LpProblem("FPL_Lineup", pulp.LpMaximize)
        start_vars = {pid: pulp.LpVariable(f"start_{pid}", cat=pulp.LpBinary) for pid in current_squad_ids}

        prob += pulp.lpSum([start_vars[pid] * squad_xp[pid] for pid in current_squad_ids])
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids]) == 11

        pos_map = {pid: self.elements[pid]["element_type"] for pid in current_squad_ids}

        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 1]) == 1
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) >= 3
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) <= 5
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) >= 2
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) <= 5
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) >= 1
        prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) <= 3

        for pid in current_squad_ids:
            if self._name_matches(pid, must_start) or pid in self.manual_locks:
                if not self.analyzer.should_not_start(pid):
                    prob += start_vars[pid] == 1
            if self._name_matches(pid, must_bench) or self.analyzer.should_not_start(pid) or self.analyzer.is_fringe(pid):
                if not (pos_map[pid] == 1 and all(
                    self.analyzer.should_not_start(gk)
                    for gk in current_squad_ids if pos_map[gk] == 1
                )):
                    prob += start_vars[pid] == 0

        status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
        if pulp.LpStatus[status] != "Optimal":
            print(f"Lineup solver status={pulp.LpStatus[status]}; retrying without injury bench locks.")
            prob = pulp.LpProblem("FPL_Lineup_Fallback", pulp.LpMaximize)
            start_vars = {pid: pulp.LpVariable(f"start_{pid}", cat=pulp.LpBinary) for pid in current_squad_ids}
            prob += pulp.lpSum([start_vars[pid] * squad_xp[pid] for pid in current_squad_ids])
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids]) == 11
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 1]) == 1
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) >= 3
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 2]) <= 5
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) >= 2
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 3]) <= 5
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) >= 1
            prob += pulp.lpSum([start_vars[pid] for pid in current_squad_ids if pos_map[pid] == 4]) <= 3
            for pid in current_squad_ids:
                if self._name_matches(pid, must_start) or pid in self.manual_locks:
                    prob += start_vars[pid] == 1
                if self._name_matches(pid, must_bench):
                    prob += start_vars[pid] == 0
            prob.solve(pulp.PULP_CBC_CMD(msg=False))

        starting_xi = [pid for pid in current_squad_ids if pulp.value(start_vars[pid]) == 1]
        bench = [pid for pid in current_squad_ids if pid not in starting_xi]

        starting_xi.sort(key=lambda pid: (pos_map[pid], -squad_xp[pid]))
        ordered_bench = self.analyzer.order_bench(bench, squad_xp)

        healthy_starters = [
            pid for pid in starting_xi if not self.analyzer.should_not_start(pid)
        ] or starting_xi
        ranked_starters = sorted(healthy_starters, key=lambda pid: -squad_xp[pid])
        cap_id = ranked_starters[0]
        vc_id = ranked_starters[1] if len(ranked_starters) > 1 else ranked_starters[0]

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
        if cap_id == vc_id:
            for pid in sorted(starting_xi, key=lambda pid: -squad_xp[pid]):
                if pid != cap_id:
                    vc_id = pid
                    break

        return {
            "starting_xi": starting_xi,
            "bench": ordered_bench,
            "captain": cap_id,
            "vice_captain": vc_id,
            "squad_xp": squad_xp,
            "transfers_in": transfers_in,
            "transfers_out": transfers_out,
            "chip": None,
            "unlimited_transfers": max_transfers >= 15,
        }
