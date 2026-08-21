import argparse
import json
import os
from datetime import datetime, timezone

from fpl_api import FPLClient
from analyzer import FPLAnalyzer
from optimizer import FPLOptimizer
from reporter import FPLReporter

DRY_RUN_WINDOW_MAX = 90
EXECUTE_WINDOW_MAX = 75


def _parse_deadline(deadline_str):
    if not deadline_str:
        raise ValueError("Missing deadline_time")
    try:
        from dateutil import parser as date_parser

        deadline = date_parser.isoparse(deadline_str)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline
    except Exception:
        return datetime.fromisoformat(str(deadline_str).replace("Z", "+00:00"))


def check_deadline_window(bootstrap_data):
    """Minutes remaining until the official FPL deadline for the next unfinished GW."""
    events = bootstrap_data.get("events", [])
    next_event = next((e for e in events if e.get("is_next")), None)
    if next_event is None:
        next_event = next(
            (e for e in events if e.get("is_current") and not e.get("finished")),
            None,
        )
    if next_event is None and events:
        next_event = events[0]
    if not next_event:
        return None, 999999.0, "No active gameweek found"

    deadline_str = next_event.get("deadline_time") or ""
    deadline_utc = _parse_deadline(deadline_str)
    minutes_to_deadline = (deadline_utc - datetime.now(timezone.utc)).total_seconds() / 60.0
    return next_event, minutes_to_deadline, deadline_str


def decide_scheduled_mode(minutes_remaining):
    """T-90 dry-run preview, T-75 execute (prefers T-60). None means skip to save Actions minutes."""
    if minutes_remaining < 0:
        return None
    if minutes_remaining <= EXECUTE_WINDOW_MAX:
        return "execute"
    if minutes_remaining <= DRY_RUN_WINDOW_MAX:
        return "dry-run"
    return None


def save_schedule_log(next_event, deadline_str, minutes_to_deadline, executed, mode=None, skipped=False):
    os.makedirs("data", exist_ok=True)
    schedule_data = {
        "target_gameweek": next_event["id"] if next_event else None,
        "target_gw_name": next_event.get("name") if next_event else None,
        "deadline_utc": deadline_str,
        "minutes_remaining": round(minutes_to_deadline, 1),
        "last_checked_utc": datetime.now(timezone.utc).isoformat(),
        "execution_completed": executed,
        "mode": mode,
        "skipped": skipped,
    }
    with open("data/schedule.json", "w") as f:
        json.dump(schedule_data, f, indent=2)
    print(f"Wrote data/schedule.json (skipped={skipped}, executed={executed}).")


def _write_github_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for key, value in kwargs.items():
            f.write(f"{key}={value}\n")


def run(mode="dry-run", force=False, scheduled=False, gate_only=False):
    client = FPLClient()
    bootstrap = client.get_bootstrap_data()
    next_event, minutes_remaining, deadline_str = check_deadline_window(bootstrap)
    if not next_event:
        raise RuntimeError("No active gameweek found")

    print(f"Next Gameweek: {next_event.get('name')} | Deadline (UTC): {deadline_str}")
    print(f"Time remaining: {minutes_remaining:.1f} minutes")

    decided_mode = mode
    if scheduled and not force:
        decided_mode = decide_scheduled_mode(minutes_remaining)
        if decided_mode is None:
            print("Outside the T-90 dry-run / T-60 execute window. Exiting early to conserve Actions minutes.")
            save_schedule_log(
                next_event, deadline_str, minutes_remaining, executed=False, mode=mode, skipped=True
            )
            _write_github_output(run="false", mode="dry-run")
            return
        print(f"Scheduled deadline window selected mode={decided_mode}.")

    if not force and decided_mode == "execute" and minutes_remaining > EXECUTE_WINDOW_MAX:
        print("Execute is gated to the T-75 window (prefers T-60). Exiting early. Pass --force to override.")
        save_schedule_log(
            next_event, deadline_str, minutes_remaining, executed=False, mode="execute", skipped=True
        )
        _write_github_output(run="false", mode="execute")
        return

    _write_github_output(run="true", mode=decided_mode)
    save_schedule_log(
        next_event, deadline_str, minutes_remaining, executed=False, mode=decided_mode, skipped=False
    )
    if gate_only:
        return

    mode = decided_mode
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

    gw = next_event["id"]

    my_team = client.get_my_team(current_gw=gw)
    prev_snapshot = client.load_previous_snapshot(gw)
    manual_transfers = client.detect_manual_transfers(my_team, prev_snapshot)

    ft_limit = my_team.get("transfers", {}).get("limit", 1)

    with open("manager_override.json", "r") as f:
        overrides = json.load(f)

    overall_rank = client.get_overall_rank()
    analyzer = FPLAnalyzer(bootstrap)
    analyzer.overall_rank = overall_rank
    analyzer.load_fixture_horizon(gw)
    analyzer.load_historical_priors()
    optimizer = FPLOptimizer(
        analyzer,
        my_team,
        bootstrap,
        overrides,
        manual_locks=manual_transfers,
        gameweek=gw,
        current_gw=gw,
        overall_rank=overall_rank,
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
        f"- **Target GW Deadline:** {deadline_str} ({minutes_remaining:.1f} min remaining)\n"
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
        save_schedule_log(
            next_event, deadline_str, minutes_remaining, executed=True, mode=mode, skipped=False
        )
        print(f"Successfully applied GW {gw} team & lineup to FPL.")
    else:
        save_schedule_log(
            next_event, deadline_str, minutes_remaining, executed=False, mode=mode, skipped=False
        )
        print(f"GW {gw} dry-run report generated in REPORT.md. Saved data/schedule.json.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    parser.add_argument("--force", action="store_true", help="Bypass deadline window check")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Cron mode: skip outside T-90/T-60, auto-select dry-run vs execute",
    )
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="Write schedule.json and GitHub Actions outputs, then exit before login",
    )
    args = parser.parse_args()
    run(mode=args.mode, force=args.force, scheduled=args.scheduled, gate_only=args.gate_only)
