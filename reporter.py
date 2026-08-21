class FPLReporter:
    @staticmethod
    def generate_report(gw, plan, elements, manual_transfers, is_dry_run=True, teams=None):
        status = "DRY RUN (Preview for Email Review)" if is_dry_run else "EXECUTED (Applied to FPL Server)"
        cap_name = elements[plan["captain"]]["web_name"]
        vc_name = elements[plan["vice_captain"]]["web_name"]
        teams = teams or {}
        pos_labels = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        def team_name(pid):
            team_id = elements[pid]["team"]
            return teams.get(team_id, {}).get("short_name", team_id)

        lines = [
            f"# ⚽ FPL Gameweek {gw} Decision Report",
            f"**Status:** `{status}`\n",
            f"### 🎖️ Captaincy",
            f"- **Captain (C):** **{cap_name}** (xP: {plan['squad_xp'][plan['captain']]})",
            f"- **Vice-Captain (VC):** **{vc_name}** (xP: {plan['squad_xp'][plan['vice_captain']]})\n"
        ]

        transfers_in = plan.get("transfers_in") or []
        transfers_out = plan.get("transfers_out") or []
        if plan.get("unlimited_transfers"):
            lines.append("### ♻️ Transfer Window")
            lines.append("- GW1/unlimited free transfers. Wildcard chip is **not** played.\n")
        else:
            ft = plan.get("ft_available")
            if ft is not None:
                lines.append("### 🏦 Free Transfer Strategy")
                action = "BANK this week's FT" if plan.get("bank_transfer") else "PLAY qualifying transfer(s)"
                lines.append(f"- **Available:** {ft} / 5")
                lines.append(f"- **Action:** {action}")
                if plan.get("transfer_strategy"):
                    lines.append(f"- {plan['transfer_strategy']}")
                lines.append("")

        chip_rec = plan.get("chip_recommendation")
        chip_play = plan.get("chip")
        chip_meta = plan.get("chip_meta") or {}
        lines.append("### 🎰 Automated Chip Strategy")
        lines.append(f"- **Window:** {chip_meta.get('half') or ('Set 1 (expires GW19)' if gw <= 19 else 'Set 2 (GW20–38)')}")
        lines.append(f"- **Recommendation:** `{chip_rec or 'None (standard gameweek)'}`")
        if chip_play:
            lines.append(f"- **Playing this week:** `{chip_play}`")
        else:
            lines.append("- **Playing this week:** none (alert only; Wildcard/Free Hit are never auto-played unless `allow_auto_chips` is set)")
        if chip_meta.get("reason"):
            lines.append(f"- {chip_meta['reason']}")
        if chip_meta.get("urgency_note"):
            lines.append(f"- **Urgency:** {chip_meta['urgency_note']}")
        if chip_meta.get("available_this_half"):
            lines.append(f"- **Available this half:** {', '.join(chip_meta['available_this_half'])}")
        if chip_meta:
            lines.append(
                f"- Injured/unavailable: {chip_meta.get('injured_starters', 0)}; "
                f"DGW players: {chip_meta.get('dgw_players', 0)}; "
                f"BGW players: {chip_meta.get('bgw_players', 0)}"
            )
        lines.append("")

        if plan.get("liquidated_budget") is not None:
            sell = (plan.get("squad_sell_value") or 0) / 10.0
            bank = (plan.get("liquidated_bank") or 0) / 10.0
            spend = (plan.get("liquidated_budget") or 0) / 10.0
            lines.append("### 💷 Squad Liquidation")
            lines.append(f"- **Current 15 sell value (50% profit tax):** £{sell:.1f}m")
            lines.append(f"- **Bank:** £{bank:.1f}m")
            lines.append(f"- **Spendable budget:** £{spend:.1f}m")
            rank = plan.get("overall_rank")
            if rank:
                if rank < 100000:
                    posture = "protecting rank (template safety)"
                elif rank > 500000:
                    posture = "chasing rank (differentials allowed)"
                else:
                    posture = "balanced"
                lines.append(f"- **Overall rank:** {rank:,} — {posture}")
            lines.append("")
        if transfers_in and transfers_out:
            lines.append("### 🔄 Planned Transfers")
            lines.append("| Out | In |")
            lines.append("| :--- | :--- |")
            for out_id, in_id in zip(transfers_out, transfers_in):
                lines.append(
                    f"| {elements[out_id]['web_name']} | **{elements[in_id]['web_name']}** |"
                )
            lines.append("")
        else:
            lines.append("### 🔄 Planned Transfers")
            lines.append("- None — banking the FT / current 15 already satisfies the 3-GW EV rule.\n")

        if manual_transfers:
            lines.append("### 👤 Manual Moves Detected & Preserved")
            for pid in manual_transfers:
                lines.append(f"- Respected manual app transfer: **{elements[pid]['web_name']}**")
            lines.append("")

        def xp_per_m(pid):
            cost = max(0.1, (elements[pid].get("now_cost") or 40) / 10.0)
            return round((plan["squad_xp"].get(pid) or 0.0) / cost, 2)

        lines.append("### 🟢 Starting XI")
        lines.append("| Pos | Player | Team | Projected xP | xP/£ |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")

        for pid in plan["starting_xi"]:
            p = elements[pid]
            armband = " **(C)**" if pid == plan["captain"] else (" **(VC)**" if pid == plan["vice_captain"] else "")
            lines.append(
                f"| {pos_labels[p['element_type']]} | {p['web_name']}{armband} | {team_name(pid)} | {plan['squad_xp'][pid]} | {xp_per_m(pid)} |"
            )

        lines.append("\n### 🪑 Bench Order")
        for i, pid in enumerate(plan["bench"], 1):
            p = elements[pid]
            lines.append(f"{i}. **{p['web_name']}** ({pos_labels[p['element_type']]}) - xP: {plan['squad_xp'][pid]}")

        lines.append(f"\n*Auto-generated at {status} window.*")
        return "\n".join(lines)
