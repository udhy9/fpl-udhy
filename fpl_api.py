import json
import os
import re
import requests


class FPLClient:
    BASE_URL = "https://fantasy.premierleague.com/api"
    OIDC_CLIENT_ID = os.environ.get(
        "FPL_OIDC_CLIENT_ID", "1f243d70-a140-4035-8c41-341f5af5aa12"
    )
    TOKEN_URL = os.environ.get(
        "FPL_TOKEN_URL", "https://account.premierleague.com/as/token"
    )

    def __init__(self, email=None, password=None, team_id=None, refresh_token=None):
        self.email = email or os.environ.get("FPL_EMAIL")
        self.password = password or os.environ.get("FPL_PASSWORD")
        self.team_id = team_id or os.environ.get("FPL_TEAM_ID")
        self.refresh_token = refresh_token or os.environ.get("FPL_REFRESH_TOKEN")
        self.access_token = None
        self.rotated_refresh_token = None
        self.my_team = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://fantasy.premierleague.com",
            "Referer": "https://fantasy.premierleague.com/",
        })

    @staticmethod
    def _extract_refresh_token(raw):
        if not raw:
            return None
        raw = raw.strip().lstrip("\ufeff").strip().strip('"').strip("'")
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip().strip('"').strip("'")
        if raw.startswith("{"):
            data = json.loads(raw)
            return data.get("refresh_token") or data.get("refreshToken")
        match = re.search(r'refresh_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', raw)
        if match:
            return match.group(1)
        return raw

    def login(self):
        refresh = self._extract_refresh_token(self.refresh_token)
        if not refresh:
            raise ValueError(
                "FPL_REFRESH_TOKEN is missing. FPL no longer accepts email/password "
                "login from GitHub Actions. Copy the oidc.user refresh token from "
                "fantasy.premierleague.com DevTools and store it as a GitHub secret."
            )
        print(f"FPL refresh token loaded (length={len(refresh)}).")

        res = self.session.post(
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.OIDC_CLIENT_ID,
                "refresh_token": refresh,
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept-language": "en",
            },
            timeout=30,
        )
        if res.status_code >= 400:
            detail = res.text[:300]
            try:
                payload = res.json()
                detail = payload.get("error_description") or payload.get("error") or detail
            except Exception:
                pass
            raise RuntimeError(
                f"FPL OIDC login failed ({res.status_code}): {detail}. "
                "Copy a fresh refresh token from fantasy.premierleague.com and update "
                "the FPL_REFRESH_TOKEN secret."
            )

        token_data = res.json()
        self.access_token = token_data.get("access_token")
        if not self.access_token:
            raise RuntimeError("FPL token endpoint did not return an access token.")

        self.session.headers["X-API-Authorization"] = f"Bearer {self.access_token}"
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self.rotated_refresh_token = new_refresh
            self.refresh_token = new_refresh
        return True

    def persist_rotated_refresh_token(self, path="rotated_refresh_token.txt"):
        if not self.rotated_refresh_token:
            return False
        with open(path, "w") as f:
            f.write(self.rotated_refresh_token)
        return True

    def get_bootstrap_data(self):
        res = self.session.get(f"{self.BASE_URL}/bootstrap-static/", timeout=30)
        res.raise_for_status()
        return res.json()

    def get_my_team(self):
        res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
        res.raise_for_status()
        self.my_team = res.json()
        return self.my_team

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
            "transfers": transfers_payload
        }
        res = self.session.post(f"{self.BASE_URL}/transfers/", json=payload, timeout=30)
        res.raise_for_status()
        return res.json()

    def submit_lineup(self, picks_payload):
        payload = {"picks": picks_payload, "chip": None}
        res = self.session.post(
            f"{self.BASE_URL}/my-team/{self.team_id}/", json=payload, timeout=30
        )
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
