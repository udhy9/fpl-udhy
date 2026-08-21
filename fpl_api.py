import json
import os

import requests


class FPLClient:
    BASE_URL = "https://fantasy.premierleague.com/api"

    def __init__(self, team_id=None, access_token=None, cookie=None):
        self.team_id = team_id or os.environ.get("FPL_TEAM_ID")
        self.access_token = self._clean_token(
            access_token or os.environ.get("FPL_ACCESS_TOKEN")
        )
        self.cookie = cookie or os.environ.get("FPL_COOKIE") or os.environ.get("pl_profile")
        self.my_team = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://fantasy.premierleague.com",
            "Referer": "https://fantasy.premierleague.com/",
            "Accept": "application/json, text/plain, */*",
        })

        if self.cookie:
            self._apply_cookie(self.cookie)
        if self.access_token:
            self._apply_access_token(self.access_token)

    @staticmethod
    def _clean_token(token):
        if not token:
            return None
        token = token.strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None

    def _apply_access_token(self, access_token):
        self.access_token = self._clean_token(access_token)
        if not self.access_token:
            return
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        self.session.headers["X-API-Authorization"] = f"Bearer {self.access_token}"

    def _apply_cookie(self, cookie):
        cookie = cookie.strip().strip('"').strip("'")
        if cookie.lower().startswith("bearer "):
            cookie = cookie[7:].strip()

        if cookie.startswith("eyJ") and "=" not in cookie.split(".")[0]:
            self._apply_access_token(cookie)
            return

        if "=" in cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name, value = name.strip(), value.strip()
                self.session.cookies.set(name, value, domain=".premierleague.com")
                if name == "access_token":
                    self._apply_access_token(value)
            return

        self.session.cookies.set("pl_profile", cookie, domain=".premierleague.com")

    def login(self):
        if not self.access_token and not self.cookie:
            print("Warning: Neither FPL_ACCESS_TOKEN nor FPL_COOKIE provided.")
            return False
        if not self.team_id:
            print("Warning: FPL_TEAM_ID is missing.")
            return False

        try:
            res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
            if res.status_code == 200:
                print("Authenticated via FPL_ACCESS_TOKEN / FPL_COOKIE.")
                return True
            print(f"Auth check response: {res.status_code} {res.text[:200]}")
            return False
        except Exception as exc:
            print(f"Auth check failed: {exc}")
            return False

    @staticmethod
    def _safe_json(res):
        if res.status_code in (204,):
            return {"status": "success", "status_code": res.status_code}
        text = (res.text or "").strip()
        if not text:
            return {"status": "success", "status_code": res.status_code}
        try:
            return res.json()
        except ValueError:
            return {"status": "success", "status_code": res.status_code, "raw": text[:200]}

    def persist_rotated_refresh_token(self, path="rotated_refresh_token.txt"):
        return False

    def get_bootstrap_data(self):
        res = self.session.get(f"{self.BASE_URL}/bootstrap-static/", timeout=30)
        res.raise_for_status()
        return res.json()

    def _public_team(self, current_gw):
        if not self.team_id:
            return None
        gameweeks = []
        if current_gw:
            gameweeks.append(int(current_gw))
            if int(current_gw) > 1:
                gameweeks.append(int(current_gw) - 1)
        else:
            gameweeks.append(1)

        bank = 0
        try:
            entry_res = self.session.get(f"{self.BASE_URL}/entry/{self.team_id}/", timeout=30)
            if entry_res.status_code == 200:
                bank = entry_res.json().get("last_deadline_bank") or 0
        except Exception:
            pass

        for gw in gameweeks:
            res = self.session.get(
                f"{self.BASE_URL}/entry/{self.team_id}/event/{gw}/picks/",
                timeout=30,
            )
            if res.status_code != 200:
                continue
            data = res.json()
            print(f"Using public squad snapshot from event {gw} (unauthenticated).")
            return {
                "picks": data.get("picks", []),
                "transfers": {"limit": 1, "made": 0, "bank": bank},
                "chips": [],
            }
        return None

    def get_my_team(self, current_gw=None):
        res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
        if res.status_code == 200:
            self.my_team = res.json()
            return self.my_team

        public_team = self._public_team(current_gw)
        if public_team and public_team.get("picks"):
            self.my_team = public_team
            return self.my_team

        raise RuntimeError(
            f"Failed to fetch team data for Team ID {self.team_id}. (Status: {res.status_code}). "
            "Update GitHub secret FPL_ACCESS_TOKEN with the access_token cookie from "
            "fantasy.premierleague.com DevTools."
        )

    def save_state_snapshot(self, gameweek, team_data):
        os.makedirs("data", exist_ok=True)
        with open(f"data/gw{gameweek}_snapshot.json", "w") as f:
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
        if not prev_snapshot:
            return []
        prev_element_ids = {p["element"] for p in prev_snapshot.get("picks", [])}
        curr_element_ids = {p["element"] for p in current_my_team.get("picks", [])}
        return list(curr_element_ids - prev_element_ids)

    def submit_transfers(self, transfers_in, transfers_out, chip=None):
        if not transfers_in or not transfers_out:
            return True
        elements = self.get_bootstrap_data()["elements"]
        id_to_player = {p["id"]: p for p in elements}

        selling = {}
        if self.my_team:
            selling = {
                p["element"]: p.get("selling_price") or id_to_player[p["element"]]["now_cost"]
                for p in self.my_team.get("picks", [])
            }

        transfers_payload = []
        for out_id, in_id in zip(transfers_out, transfers_in):
            transfers_payload.append({
                "element_in": in_id,
                "element_out": out_id,
                "purchase_price": id_to_player[in_id]["now_cost"],
                "selling_price": selling.get(out_id, id_to_player[out_id]["now_cost"]),
            })

        payload = {
            "chip": chip,
            "entry": int(self.team_id),
            "event": self.get_current_event()["id"],
            "transfers": transfers_payload,
        }
        res = self.session.post(f"{self.BASE_URL}/transfers/", json=payload, timeout=30)
        res.raise_for_status()
        return self._safe_json(res)

    def submit_lineup(self, picks_payload):
        payload = {"picks": picks_payload, "chip": None}
        res = self.session.post(
            f"{self.BASE_URL}/my-team/{self.team_id}/", json=payload, timeout=30
        )
        res.raise_for_status()
        return self._safe_json(res)

    def get_current_event(self):
        data = self.get_bootstrap_data()
        for event in data["events"]:
            if event["is_next"]:
                return event
            if event["is_current"] and not event["finished"]:
                return event
        return data["events"][0]
