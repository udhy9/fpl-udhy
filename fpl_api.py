import json
import os

import requests


class FPLClient:
    BASE_URL = "https://fantasy.premierleague.com/api"

    def __init__(self, team_id=None, access_token=None, cookie=None, email=None, password=None):
        self.team_id = team_id or os.environ.get("FPL_TEAM_ID")
        self.email = email or os.environ.get("FPL_EMAIL")
        self.password = password or os.environ.get("FPL_PASSWORD")
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

    def _import_browser_cookies(self, cookies):
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = cookie.get("domain") or ".premierleague.com"
            self.session.cookies.set(name, value, domain=domain)
            if name == "access_token":
                self._apply_access_token(value)

    def login_with_playwright(self):
        if not self.email or not self.password:
            raise ValueError("FPL_EMAIL and FPL_PASSWORD must be set in GitHub Secrets.")

        from playwright.sync_api import sync_playwright

        print("Starting Playwright FPL login...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=self.session.headers["User-Agent"],
                locale="en-GB",
            )
            page = context.new_page()
            page.set_default_timeout(45000)
            page.goto("https://fantasy.premierleague.com/", wait_until="domcontentloaded")

            for label in ("Accept All Cookies", "Accept all", "Accept", "I Accept"):
                try:
                    page.get_by_role("button", name=label).click(timeout=2500)
                    break
                except Exception:
                    continue

            signed_in = False
            for locator in (
                page.get_by_role("link", name="Sign in"),
                page.get_by_role("link", name="Log in"),
                page.locator("a[href*='login']"),
            ):
                try:
                    locator.first.click(timeout=4000)
                    signed_in = True
                    break
                except Exception:
                    continue
            if not signed_in:
                page.goto(
                    "https://fantasy.premierleague.com/",
                    wait_until="domcontentloaded",
                )

            user_box = None
            for selector in (
                "input[type='email']",
                "input[name='username']",
                "input[name='login']",
                "input[id='username']",
                "input[autocomplete='username']",
            ):
                try:
                    page.wait_for_selector(selector, timeout=8000)
                    user_box = selector
                    break
                except Exception:
                    continue
            if not user_box:
                browser.close()
                raise RuntimeError(
                    "Playwright could not find the FPL login form. Cloudflare Turnstile "
                    "may have blocked the GitHub Actions IP."
                )

            page.fill(user_box, self.email)
            page.fill("input[type='password']", self.password)
            for submit in (
                "button[type='submit']",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "input[type='submit']",
            ):
                try:
                    page.click(submit, timeout=3000)
                    break
                except Exception:
                    continue

            try:
                page.wait_for_url("**/fantasy.premierleague.com/**", timeout=60000)
            except Exception:
                browser.close()
                raise RuntimeError(
                    "Playwright login did not return to fantasy.premierleague.com. "
                    "Turnstile/CAPTCHA likely blocked the headless browser."
                )

            token = page.evaluate(
                """() => {
                    try {
                        const key = Object.keys(localStorage).find(k => k.startsWith('oidc.user:'));
                        if (!key) return null;
                        const data = JSON.parse(localStorage.getItem(key) || '{}');
                        return data.access_token || null;
                    } catch (e) {
                        return null;
                    }
                }"""
            )
            self._import_browser_cookies(context.cookies())
            if token:
                self._apply_access_token(token)
            browser.close()

        if self.team_id:
            res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
            if res.status_code != 200:
                raise RuntimeError(
                    f"Playwright login finished but my-team returned {res.status_code}."
                )
        print("Authenticated via Playwright (FPL_EMAIL / FPL_PASSWORD).")
        return True

    def login(self):
        if self.email and self.password:
            try:
                return self.login_with_playwright()
            except Exception as exc:
                print(f"Playwright login failed: {exc}")

        if not self.access_token and not self.cookie:
            print("Warning: No Playwright credentials and no FPL_ACCESS_TOKEN / FPL_COOKIE.")
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

    def get_my_team(self, current_gw=None, require_auth=False):
        res = self.session.get(f"{self.BASE_URL}/my-team/{self.team_id}/", timeout=30)
        if res.status_code == 200:
            self.my_team = res.json()
            return self.my_team

        if require_auth:
            raise RuntimeError(
                f"Authenticated my-team fetch failed with HTTP {res.status_code}: {(res.text or '')[:400]}. "
                "Cannot refresh squad after transfers."
            )

        public_team = self._public_team(current_gw)
        if public_team and public_team.get("picks"):
            self.my_team = public_team
            return self.my_team

        raise RuntimeError(
            f"Failed to fetch team data for Team ID {self.team_id}. (Status: {res.status_code}). "
            "Update FPL_EMAIL / FPL_PASSWORD, or FPL_ACCESS_TOKEN if Playwright login is blocked."
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

    def _require_write_success(self, res, action):
        preview = (res.text or "")[:800]
        print(f"{action} HTTP {res.status_code}: {preview}")
        if res.status_code not in (200, 201, 204):
            raise RuntimeError(f"{action} failed with HTTP {res.status_code}: {preview}")

        content_type = (res.headers.get("Content-Type") or "").lower()
        text = (res.text or "").lstrip()
        if "html" in content_type or text.lower().startswith(("<!doctype", "<html")):
            raise RuntimeError(
                f"{action} returned a login HTML page. Session expired — refresh FPL_ACCESS_TOKEN "
                "or FPL_EMAIL / FPL_PASSWORD."
            )

        body = self._safe_json(res)
        if isinstance(body, dict):
            for key in ("detail", "non_form_error", "non_form_errors", "error", "errors"):
                value = body.get(key)
                if value:
                    raise RuntimeError(f"{action} rejected by FPL ({key}): {value}")
        return body

    def _transfer_payload(self, transfers_in, transfers_out, chip=None, confirmed=True):
        if len(transfers_in) != len(transfers_out):
            raise ValueError(
                f"Transfer mismatch: {len(transfers_out)} out vs {len(transfers_in)} in."
            )

        elements = self.get_bootstrap_data()["elements"]
        id_to_player = {p["id"]: p for p in elements}
        live_picks = (self.my_team or {}).get("picks") or []
        selling = {
            p["element"]: p.get("selling_price") or id_to_player[p["element"]]["now_cost"]
            for p in live_picks
            if p.get("element") in id_to_player
        }

        transfers_payload = []
        for out_id, in_id in zip(transfers_out, transfers_in):
            out_id, in_id = int(out_id), int(in_id)
            if in_id not in id_to_player:
                raise RuntimeError(f"Unknown transfer-in player id {in_id}.")
            if out_id not in id_to_player:
                raise RuntimeError(f"Unknown transfer-out player id {out_id}.")
            transfers_payload.append({
                "element_in": in_id,
                "element_out": out_id,
                "purchase_price": int(id_to_player[in_id]["now_cost"]),
                "selling_price": int(selling.get(out_id, id_to_player[out_id]["now_cost"])),
            })

        chip_value = chip if chip else None
        return {
            "confirmed": bool(confirmed),
            "chip": chip_value,
            "entry": int(self.team_id),
            "event": int(self.get_current_event()["id"]),
            "transfers": transfers_payload,
        }

    def submit_transfers(self, transfers_in, transfers_out, chip=None):
        if not transfers_in or not transfers_out:
            print("No transfers to submit.")
            return {"status": "skipped", "reason": "empty"}

        live_ids = {p["element"] for p in (self.my_team or {}).get("picks", [])}
        pending_in, pending_out = [], []
        for in_id, out_id in zip(transfers_in, transfers_out):
            if in_id in live_ids and out_id not in live_ids:
                continue
            pending_in.append(in_id)
            pending_out.append(out_id)
        if not pending_in:
            print("Planned transfers already present on the live squad; skipping transfer POST.")
            return {"status": "skipped", "reason": "already_applied"}

        headers = {
            "Content-Type": "application/json",
            "Referer": "https://fantasy.premierleague.com/transfers",
        }
        full_payload = self._transfer_payload(pending_in, pending_out, chip=chip, confirmed=True)
        modern_payload = {k: v for k, v in full_payload.items() if k != "confirmed"}
        print(f"Submitting transfers payload: {json.dumps(modern_payload)}")

        res = self.session.post(
            f"{self.BASE_URL}/transfers/", json=modern_payload, timeout=30, headers=headers
        )
        if res.status_code in (200, 201, 204):
            body = self._require_write_success(res, "Transfer submit")
            spent = int(body.get("spent_points") or 0) if isinstance(body, dict) else 0
            if spent > 0:
                raise RuntimeError(
                    f"Transfers cost a {spent}-point hit unexpectedly. Check the live FPL squad."
                )
            return body

        print(
            f"Direct transfer POST returned HTTP {res.status_code}: {(res.text or '')[:400]}. "
            "Retrying FPL confirmed two-step."
        )
        validate_payload = dict(modern_payload)
        validate_payload["confirmed"] = False
        validate_res = self.session.post(
            f"{self.BASE_URL}/transfers/", json=validate_payload, timeout=30, headers=headers
        )
        validate_body = self._require_write_success(validate_res, "Transfer validation")
        spent = int(validate_body.get("spent_points") or 0) if isinstance(validate_body, dict) else 0
        if spent > 0:
            raise RuntimeError(
                f"Transfers would cost a {spent}-point hit. Aborting so the live squad is not changed."
            )

        confirm_payload = dict(modern_payload)
        confirm_payload["confirmed"] = True
        confirm_res = self.session.post(
            f"{self.BASE_URL}/transfers/", json=confirm_payload, timeout=30, headers=headers
        )
        try:
            return self._require_write_success(confirm_res, "Transfer confirm")
        except RuntimeError as exc:
            print(f"Transfer confirm reported an error ({exc}); checking whether the live squad already updated.")
            live = self.get_my_team(require_auth=True)
            live_ids = {p["element"] for p in live.get("picks", [])}
            if all(pid in live_ids for pid in pending_in):
                return {"status": "applied", "note": "confirm POST errored but live squad contains transferred-in players"}
            raise

    def submit_lineup(self, picks_payload, chip=None):
        if len(picks_payload) != 15:
            raise ValueError(f"Lineup must contain 15 picks, got {len(picks_payload)}.")
        positions = [p.get("position") for p in picks_payload]
        if sorted(positions) != list(range(1, 16)):
            raise ValueError(f"Lineup positions must be 1-15 uniquely, got {positions}.")
        captains = [p for p in picks_payload if p.get("is_captain")]
        vices = [p for p in picks_payload if p.get("is_vice_captain")]
        if len(captains) != 1 or len(vices) != 1 or captains[0]["element"] == vices[0]["element"]:
            raise ValueError("Lineup must have exactly one captain and a different vice-captain.")

        payload = {"picks": picks_payload, "chip": chip if chip else None}
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://fantasy.premierleague.com/my-team",
        }
        print(f"Submitting lineup payload: {json.dumps(payload)}")
        res = self.session.post(
            f"{self.BASE_URL}/my-team/{self.team_id}/",
            json=payload,
            timeout=30,
            headers=headers,
        )
        return self._require_write_success(res, "Lineup submit")

    def get_current_event(self):
        data = self.get_bootstrap_data()
        for event in data["events"]:
            if event["is_next"]:
                return event
            if event["is_current"] and not event["finished"]:
                return event
        return data["events"][0]
