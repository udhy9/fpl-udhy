# 🏆 FPL AI Manager

An autonomous, tactical Fantasy Premier League manager powered by **Mixed-Integer Linear Programming (PuLP)** and **Google Gemini AI**.

It handles weekly transfers, captaincy selection, formation adjustments, bench order, and long-term chip timing—running automatically via GitHub Actions ahead of every Gameweek deadline.

---

## 🧠 Core Features

* **Dual-Layer Decision Engine:** Combines statistical expected points ($xP$), Expected Minutes ($xMins$), and Expected Goal Involvement ($xGI$) with LLM tactical sanity checks.
* **Anti-Cannibalization Logic:** Automatically prevents fixture conflicts (e.g., benching defenders who play away against your starting premium striker).
* **5 Free Transfer Accumulation:** Strategically banks up to 5 FTs to plan multi-player "mini-Wildcards" without taking point hits ($-4$).
* **Full 8-Chip Ecosystem:** Tracks both sets of chips (Wildcard, Free Hit, Triple Captain, Bench Boost) with automated expiration warnings for Set 1 before Gameweek 19.
* **Double & Blank Gameweek Awareness:** Identifies DGW/BGW schedules directly from the official FPL fixture matrix and adjusts expected value multipliers.
* **Rank-Aware Strategy:** Adjusts posture between template protection (defending rank) and high-$xGI$ differentials (chasing rank).
* **Automated Cloud Execution:** Uses Playwright to authenticate headlessly on GitHub Actions, running previews (`dry-run`) or auto-committing team updates (`execute`) 60 minutes before the official deadline.
* **Mobile App Respect:** Automatically detects and locks manual transfers made in the official FPL app.

---

## 🏗 Architecture & Data Flow

```
[Official FPL API] ───────┐
(Live fixtures, news,    │
ICT, ownership, prices) ├──> [1. Quantitative Solver (PuLP)]
                         │     - Mathematical xP / xMins
[Rolling 5-GW Fixture FDR]─┘     - Linear formation constraints
                                 - Accurate bank liquidation math
                                         │
                                         ▼
[Live Tactical Context] ──────> [2. Gemini Tactical Review]
(Injury news, rotation risk,        - Anti-cannibalization check
 press conferences)                 - Transfer banking / spend evaluation
                                    - Sub order & Captaincy pick
                                         │
                                         ▼
[Execution & Reporting] <────── [3. Headless Submitter]
 * Publishes REPORT.md              - Submits transfers & Starting XI
 * Commits state snapshots          - 0 manual token inputs required
```

---

## ⚙️ How It Thinks

### 1. Minutes Security & Goalkeeper Rule
* Goalkeepers must have verified starter status ($xMins = 1.0$). Backup goalkeepers with zero starts are never placed in the Starting XI.
* Outfield non-playing youth/budget options are discounted to prevent weak starters.

### 2. Intelligent Bench Hierarchy
* **1st Sub:** Assigned to the highest-$xP$, fully fit outfield player.
* **2nd & 3rd Subs:** Reserved for doubtful (yellow flag) or injured (red flag) squad players so that auto-substitutions remain protected.

### 3. Transfer Discipline
* **Roll / Bank:** If your starting XI is healthy and projected gains from a move are $< 2.0\text{ net } xP$.
* **1 Transfer:** If a key starter has a confirmed multi-week injury, or a target player has sustained form ($xGI90 > 0.50$) with 4+ consecutive green fixtures.
* **Multi-Transfer (2–5 FTs):** Executed when structural budget reallocation is needed without taking $-4$ hits.

---

## 🚀 Quick Setup

### 1. Fork or Clone the Repository

```bash
git clone https://github.com/udhy9/fpl-udhy.git
cd fpl-udhy
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure GitHub Secrets

Navigate to **Settings** → **Secrets and variables** → **Actions** and add:

| Secret Name | Description | Example |
| --- | --- | --- |
| `FPL_EMAIL` | Your Premier League login email | `user@example.com` |
| `FPL_PASSWORD` | Your Premier League password | `YourPassword123` |
| `FPL_TEAM_ID` | Your FPL Entry / Team ID | `1234567` |
| `GEMINI_API_KEY` | Free API key from Google AI Studio | `AIzaSy...` |

### 3. Manager Overrides (Optional)

To force specific choices without modifying the code, edit `manager_override.json`:

```json
{
  "must_start": ["Haaland", "Saka"],
  "must_bench": [],
  "lock_captain": "Haaland",
  "lock_vice_captain": "Palmer"
}
```

---

## 🛠 Manual Execution

You can run the script locally or trigger the workflow manually from the **Actions** tab:

```bash
# Dry run: Generates tactical report without submitting changes
python main.py --mode dry-run

# Execute: Submits transfers, lineup, and captaincy directly to FPL
python main.py --mode execute
```

---

## 📋 Generated Output (`REPORT.md`)

After every run, the agent writes a markdown report detailing:

* **Starting XI & Bench Order:** Clear position-by-position breakdown with expected points.
* **Captain & Vice-Captain:** Selected based on highest expected value and home fixture weighting.
* **Transfer Audit:** Details whether transfers were executed or banked toward the 5-FT cap.
* **Tactical AI Summary:** Concise reasoning explaining fixture targets, chip status, and bench prioritization.
