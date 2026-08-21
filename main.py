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

    with open("manager_override.json", "r") as f:
        overrides = json.load(f)

    analyzer = FPLAnalyzer(bootstrap)
    optimizer = FPLOptimizer(
        analyzer, my_team, bootstrap, overrides, manual_locks=manual_transfers, gameweek=gw
    )
    plan = optimizer.optimize()

    squad_ids = [p["element"] for p in my_team.get("picks", [])]
    if plan.get("starting_xi"):
        squad_ids = plan["starting_xi"] + plan["bench"]
    plan, tactical_reasoning = analyzer.run_llm_tactical_review(
        plan, squad_ids, overrides, gameweek=gw
    )

    report_md = FPLReporter.generate_report(
        gw, plan, analyzer.elements, manual_transfers, is_dry_run, teams=analyzer.teams
    )
    report_md += f"\n\n### 🧠 Tactical AI Analysis\n{tactical_reasoning}\n"

    with open("REPORT.md", "w") as f:
        f.write(report_md)

    if not is_dry_run:
        snapshot_path = f"data/gw{gw}_snapshot.json"
        if os.path.exists(snapshot_path):
            print(f"GW {gw} already executed (snapshot exists). Skipping submit.")
            return

        # Build lineup picks payload
        picks = []
        for i, pid in enumerate(plan["starting_xi"], 1):
            picks.append({
                "element": pid,
                "position": i,
                "is_captain": (pid == plan["captain"]),
                "is_vice_captain": (pid == plan["vice_captain"])
            })
        for i, pid in enumerate(plan["bench"], 12):
            picks.append({
                "element": pid,
                "position": i,
                "is_captain": False,
                "is_vice_captain": False
            })
        if plan.get("transfers_in") and plan.get("transfers_out"):
            client.submit_transfers(
                plan["transfers_in"], plan["transfers_out"], chip=plan.get("chip")
            )
            print(f"Submitted {len(plan['transfers_in'])} GW {gw} transfers.")
        client.submit_lineup(picks)
        client.save_state_snapshot(gw, my_team)
        print(f"Successfully submitted GW {gw} lineup to FPL.")
    else:
        print(f"GW {gw} dry-run report generated in REPORT.md.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    args = parser.parse_args()
    run(mode=args.mode)
