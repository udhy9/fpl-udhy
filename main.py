import argparse
import json
import os
from fpl_api import FPLClient
from analyzer import FPLAnalyzer
from optimizer import FPLOptimizer
from reporter import FPLReporter


def run(mode="dry-run"):
    client = FPLClient()
    is_dry_run = (mode == "dry-run")

    logged_in = client.login()
    if logged_in:
        client.persist_rotated_refresh_token()
    elif not is_dry_run:
        raise RuntimeError(
            "Authentication failed. Set GitHub secrets FPL_EMAIL and FPL_PASSWORD so Playwright can log in, "
            "or set FPL_ACCESS_TOKEN (access_token cookie) as a fallback. FPL_TEAM_ID is required."
        )
    else:
        print("Auth unavailable; dry-run will use the public team endpoint if FPL_TEAM_ID is set.")

    bootstrap = client.get_bootstrap_data()
    event = client.get_current_event()
    gw = event["id"]

    my_team = client.get_my_team(current_gw=gw)
    prev_snapshot = client.load_previous_snapshot(gw)
    manual_transfers = client.detect_manual_transfers(my_team, prev_snapshot)

    ft_limit = my_team.get("transfers", {}).get("limit", 1)

    with open("manager_override.json", "r") as f:
        overrides = json.load(f)

    analyzer = FPLAnalyzer(bootstrap)
    analyzer.load_fixture_horizon(gw)
    analyzer.load_historical_priors()
    optimizer = FPLOptimizer(
        analyzer, my_team, bootstrap, overrides, manual_locks=manual_transfers, gameweek=gw, current_gw=gw
    )
    plan = optimizer.optimize()

    squad_ids = [p["element"] for p in my_team.get("picks", [])]
    if plan.get("starting_xi"):
        squad_ids = plan["starting_xi"] + plan["bench"]
    plan, tactical_reasoning = analyzer.run_llm_tactical_review(
        plan, squad_ids, overrides, gameweek=gw, ft_available=ft_limit
    )

    report_md = FPLReporter.generate_report(
        gw, plan, analyzer.elements, manual_transfers, is_dry_run, teams=analyzer.teams
    )
    report_md += (
        f"\n\n### 🧠 Tactical AI Analysis\n"
        f"- **Free Transfers Available:** {plan.get('ft_available', ft_limit)} / 5 "
        f"({'banked' if plan.get('bank_transfer') else 'in play'})\n"
        f"- **Automated Chip Strategy:** `{plan.get('chip_recommendation') or 'None (standard gameweek)'}`"
        f"{' — playing `' + plan['chip'] + '`' if plan.get('chip') else ''}\n"
        f"- **Rationale:** {tactical_reasoning}\n"
    )

    with open("REPORT.md", "w") as f:
        f.write(report_md)

    if not is_dry_run:
        snapshot_path = f"data/gw{gw}_snapshot.json"
        if os.path.exists(snapshot_path):
            print(
                f"GW {gw} snapshot exists from a previous execute. "
                "Transfers already on the live squad will be skipped; lineup/captaincy will still be posted."
            )

        transfers_in = plan.get("transfers_in") or []
        transfers_out = plan.get("transfers_out") or []
        if transfers_in and transfers_out:
            print(f"Submitting transfers: OUT {transfers_out} -> IN {transfers_in}")
            transfer_chip = plan.get("chip") if plan.get("chip") in ("wildcard", "freehit") else None
            transfer_res = client.submit_transfers(
                transfers_in, transfers_out, chip=transfer_chip
            )
            print(f"Transfers submitted: {transfer_res}")
            my_team = client.get_my_team(current_gw=gw, require_auth=True)
            live_ids = {p["element"] for p in my_team.get("picks", [])}
            missing = [pid for pid in transfers_in if pid not in live_ids]
            if missing:
                raise RuntimeError(
                    f"Transfers POST returned success but these IN players are still missing from my-team: {missing}"
                )
        else:
            if plan.get("bank_transfer"):
                print(f"Banking FT ({ft_limit}/5). Lineup-only submit.")
            else:
                print("No planned transfers; submitting current 15 as lineup only.")

        live_ids = {p["element"] for p in my_team.get("picks", [])}
        planned_squad = list(plan["starting_xi"]) + list(plan["bench"])
        missing_squad = [pid for pid in planned_squad if pid not in live_ids]
        if missing_squad:
            raise RuntimeError(
                f"Cannot submit lineup; planned players are not in the live squad: {missing_squad}"
            )

        starting_xi = sorted(plan["starting_xi"], key=lambda pid: analyzer.elements[pid]["element_type"])
        bench = analyzer.order_bench(plan["bench"], plan["squad_xp"])

        picks = []
        for i, pid in enumerate(starting_xi, 1):
            picks.append({
                "element": int(pid),
                "position": i,
                "is_captain": (pid == plan["captain"]),
                "is_vice_captain": (pid == plan["vice_captain"]),
            })
        for i, pid in enumerate(bench, 12):
            picks.append({
                "element": int(pid),
                "position": i,
                "is_captain": False,
                "is_vice_captain": False,
            })

        lineup_chip = plan.get("chip") if plan.get("chip") in ("bboost", "3xc") else None
        lineup_res = client.submit_lineup(picks, chip=lineup_chip)
        print(f"Lineup submitted: {lineup_res}")

        verified = client.get_my_team(current_gw=gw, require_auth=True)
        verified_picks = {p["element"]: p for p in verified.get("picks", [])}
        cap = next((p for p in verified["picks"] if p.get("is_captain")), None)
        vc = next((p for p in verified["picks"] if p.get("is_vice_captain")), None)
        if not cap or cap["element"] != plan["captain"]:
            raise RuntimeError(
                f"Lineup POST did not stick. Live captain={cap} expected={plan['captain']}."
            )
        if not vc or vc["element"] != plan["vice_captain"]:
            raise RuntimeError(
                f"Lineup POST did not stick. Live VC={vc} expected={plan['vice_captain']}."
            )
        for pid in starting_xi:
            pick = verified_picks.get(pid)
            if not pick or pick.get("position", 99) > 11:
                raise RuntimeError(f"Planned starter {pid} is not in the live starting XI.")

        client.save_state_snapshot(gw, verified)
        print(f"Successfully applied GW {gw} team & lineup to FPL.")
    else:
        print(f"GW {gw} dry-run report generated in REPORT.md.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    args = parser.parse_args()
    run(mode=args.mode)
