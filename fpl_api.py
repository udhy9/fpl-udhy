import json
import os
import re
from datetime import datetime

import requests


class FPLClient:
    LOGIN_URL = "https://users.premierleague.com/accounts/login/"
    BASE_URL = "https://fantasy.premierleague.com/api"
    OIDC_CLIENT_ID = os.environ.get(
        "FPL_OIDC_CLIENT_ID", "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
    )
    TOKEN_URL = os.environ.get(
        "FPL_TOKEN_URL", "https://account.premierleague.com/as/token"
    )

    def __init__(self, email=None, password=None, team_id=None, refresh_token=None, cookie=None):
        self.email = email or os.environ.get("FPL_EMAIL")
        self.password = password or os.environ.get("FPL_PASSWORD")
        self.team_id = team_id or os.environ.get("FPL_TEAM_ID")
        self.refresh_token = refresh_token or os.environ.get("FPL_REFRESH_TOKEN")
        self.cookie = cookie or os.environ.get("FPL_COOKIE") or os.environ.get("pl_profile")
        self.access_token = None
        self.rotated_refresh_token = None
        self.my_team = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://fantasy.premierleague.com",
            "Referer": "https://fantasy.premierleague.com/",
        })
        if self.cookie:
            self._apply_cookie(self.cookie)

    @staticmethod
    def _parse_oidc_blob(raw):
        parsed = {
            "refresh_token": None,
            "access_token": None,
            "expires_at": None,
            "client_id": None,
        }
        if not raw:
            return parsed
        raw = raw.strip().lstrip("\ufeff").strip().strip('"').strip("'")
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip().strip('"').strip("'")
        if raw.startswith("{"):
            data = json.loads(raw)
            parsed["refresh_token"] = data.get("refresh_token") or data.get("refreshToken")
            parsed["access_token"] = data.get("access_token") or data.get("accessToken")
            parsed["expires_at"] = data.get("expires_at") or data.get("expiresAt")
            parsed["client_id"] = data.get("client_id")
            return parsed
        match = re.search(r'refresh_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', raw)
        parsed["refresh_token"] = match.group(1) if match else raw
        return parsed

    def _apply_access_token(self, access_token):
        self.access_token = access_token
        self.session.headers["X-API-Authorization"] = f"Bearer {access_token}"

    def _apply_cookie(self, cookie):
        cookie = cookie.strip().strip('"').strip("'")
        if "pl_profile=" in cookie or ";" in cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                self.session.cookies.set(name.strip(), value.strip(), domain=".premierleague.com")
            return
        self.session.cookies.set("pl_profile", cookie, domain=".premierleague.com")

    def _session_is_authenticated(self):
        if not self.team_id:
            return False
        res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
        return res.status_code == 200

    def _login_oidc(self):
        blob = self._parse_oidc_blob(self.refresh_token)
        refresh = blob["refresh_token"]
        access = blob["access_token"]
        client_id = blob["client_id"] or self.OIDC_CLIENT_ID
        expires_at = blob["expires_at"]

        if access and expires_at:
            try:
                if float(expires_at) > (datetime.now().timestamp() + 60):
                    print("Using unexpired FPL access token from oidc.user blob.")
                    self._apply_access_token(access)
                    self.refresh_token = refresh
                    return True
            except (TypeError, ValueError):
                pass
        elif access and not refresh:
            print("Using FPL access token from oidc.user blob.")
            self._apply_access_token(access)
            return True

        if not refresh:
            return False
        print(f"FPL refresh token loaded (length={len(refresh)}, client_id={client_id}).")

        res = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh,
            },
            headers={
                "User-Agent": self.session.headers.get("User-Agent"),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
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
                "Set FPL_COOKIE (pl_profile from DevTools) or copy a fresh refresh token "
                "after closing every FPL tab."
            )

        token_data = res.json()
        self.access_token = token_data.get("access_token")
        if not self.access_token:
            raise RuntimeError("FPL token endpoint did not return an access token.")

        self._apply_access_token(self.access_token)
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self.rotated_refresh_token = new_refresh
            self.refresh_token = new_refresh
        return True

    def login(self):
        if self.cookie:
            try:
                if self._session_is_authenticated():
                    print("Authenticated via FPL_COOKIE / pl_profile.")
                    return True
                print("FPL_COOKIE was set but my-team rejected the session.")
            except Exception as exc:
                print(f"FPL_COOKIE session check failed: {exc}")

        if self.refresh_token:
            try:
                if self._login_oidc():
                    return True
            except Exception as exc:
                print(f"OIDC login failed: {exc}")

        if self.email and self.password:
            try:
                payload = {
                    "login": self.email,
                    "password": self.password,
                    "app": "plfpl-web",
                    "redirect_uri": "https://fantasy.premierleague.com/",
                }
                res = self.session.post(self.LOGIN_URL, data=payload, timeout=30)
                if "pl_profile" in self.session.cookies or res.status_code == 200:
                    if self._session_is_authenticated():
                        print("Authenticated via FPL email/password.")
                        return True
            except Exception as exc:
                print(f"Email/password login failed: {exc}")

        return False

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
        try:
            res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
            if res.status_code == 200:
                self.my_team = res.json()
                return self.my_team
        except Exception as exc:
            print(f"Authenticated my-team lookup failed: {exc}")

        public_team = self._public_team(current_gw)
        if public_team and public_team.get("picks"):
            self.my_team = public_team
            return self.my_team

        raise RuntimeError(
            "Could not retrieve team data. For execute, set FPL_COOKIE (pl_profile) "
            "or a valid FPL_REFRESH_TOKEN. Dry-run can use FPL_TEAM_ID plus the public picks API."
        )

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
