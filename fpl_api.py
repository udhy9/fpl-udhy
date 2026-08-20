import requests
import json
import os
from datetime import datetime


class FPLClient:
    LOGIN_URL = "https://users.premierleague.com/accounts/login/"
    BASE_URL = "https://fantasy.premierleague.com/api"

    def __init__(self, email=None, password=None, team_id=None):
        self.email = email or os.environ.get("FPL_EMAIL")
        self.password = password or os.environ.get("FPL_PASSWORD")
        self.team_id = team_id or os.environ.get("FPL_TEAM_ID")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    def login(self):
        if not self.email or not self.password:
            raise ValueError("FPL credentials missing.")
        payload = {
            "login": self.email,
            "password": self.password,
            "app": "plfpl-web",
            "redirect_uri": "https://fantasy.premierleague.com/"
        }
        res = self.session.post(self.LOGIN_URL, data=payload)
        if res.status_code != 200 and "pl_profile" not in self.session.cookies:
            raise RuntimeError("FPL authentication failed. Check credentials.")
        return True

    def get_bootstrap_data(self):
        res = self.session.get(f"{self.BASE_URL}/bootstrap-static/")
        res.raise_for_status()
        return res.json()

    def get_my_team(self):
        res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/")
        res.raise_for_status()
        return res.json()

    def save_state_snapshot(self, gameweek, team_data):
        os.makedirs("data", exist_ok=True)
        filepath = f"data/gw{gameweek}_snapshot.json"
        with open(filepath, "w") as f:
            json.dump(team_data, f, indent=2)

    def load_previous_snapshot(self, current_gw):
        if current_gw <= 1:
            return None
        filepath = f"data/gw{current_gw - 1}_snapshot.json"
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                return json.load(f)
        return None

    def detect_manual_transfers(self, current_my_team, prev_snapshot):
        """Identifies if the user already made transfers manually via the official app."""
        if not prev_snapshot:
            return []
        prev_element_ids = {p["element"] for p in prev_snapshot.get("picks", [])}
        curr_element_ids = {p["element"] for p in current_my_team.get("picks", [])}
        new_transfers_in = list(curr_element_ids - prev_element_ids)
        return new_transfers_in

    def submit_transfers(self, transfers_in, transfers_out, chip=None):
        if not transfers_in or not transfers_out:
            return True
        elements = self.get_bootstrap_data()["elements"]
        id_to_player = {p["id"]: p for p in elements}

        transfers_payload = []
        for out_id, in_id in zip(transfers_out, transfers_in):
            transfers_payload.append({
                "element_in": in_id,
                "element_out": out_id,
                "purchase_price": id_to_player[in_id]["now_cost"],
                "selling_price": id_to_player[out_id]["now_cost"]
            })

        payload = {
            "chip": chip,
            "entry": int(self.team_id),
            "event": self.get_current_event()["id"],
            "transfers": transfers_payload
        }
        res = self.session.post(f"{self.BASE_URL}/transfers/", json=payload)
        res.raise_for_status()
        return res.json()

    def submit_lineup(self, picks_payload):
        payload = {"picks": picks_payload, "chip": None}
        res = self.session.post(f"{self.BASE_URL}/my-team/{self.team_id}/", json=payload)
        res.raise_for_status()
        return res.json()

    def get_current_event(self):
        data = self.get_bootstrap_data()
        for event in data["events"]:
            if event["is_next"]:
                return event
            if event["is_current"] and not event["finished"]:
                return event
        return data["events"][0]
