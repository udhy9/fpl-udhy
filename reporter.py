class FPLReporter:
    @staticmethod
    def generate_report(gw, plan, elements, manual_transfers, is_dry_run=True):
        status = "DRY RUN (Preview for Email Review)" if is_dry_run else "EXECUTED (Applied to FPL Server)"
        cap_name = elements[plan["captain"]]["web_name"]
        vc_name = elements[plan["vice_captain"]]["web_name"]

        lines = [
            f"# ⚽ FPL Gameweek {gw} Decision Report",
            f"**Status:** `{status}`\n",
            f"### 🎖️ Captaincy",
            f"- **Captain (C):** **{cap_name}** (xP: {plan['squad_xp'][plan['captain']]})",
            f"- **Vice-Captain (VC):** **{vc_name}** (xP: {plan['squad_xp'][plan['vice_captain']]})\n"
        ]

        if manual_transfers:
            lines.append("### 👤 Manual Moves Detected & Preserved")
            for pid in manual_transfers:
                lines.append(f"- Respected manual app transfer: **{elements[pid]['web_name']}**")
            lines.append("")

        lines.append("### 🟢 Starting XI")
        lines.append("| Pos | Player | Team | Projected xP |")
        lines.append("| :--- | :--- | :--- | :--- |")
        pos_labels = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        for pid in plan["starting_xi"]:
            p = elements[pid]
            armband = " **(C)**" if pid == plan["captain"] else (" **(VC)**" if pid == plan["vice_captain"] else "")
            lines.append(f"| {pos_labels[p['element_type']]} | {p['web_name']}{armband} | {p['team']} | {plan['squad_xp'][pid]} |")

        lines.append("\n### 🪑 Bench Order")
        for i, pid in enumerate(plan["bench"], 1):
            p = elements[pid]
            lines.append(f"{i}. **{p['web_name']}** ({pos_labels[p['element_type']]}) - xP: {plan['squad_xp'][pid]}")

        lines.append(f"\n*Auto-generated at {status} window.*")
        return "\n".join(lines)
