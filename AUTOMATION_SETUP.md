# GRV Availability — Daily Automation: Install & Rollout

This automates what you do manually today: run the refresh, then publish
`availability.json` / `metadata.json` (and `config.json` when it changes) to the
GitHub `docs/` folder. No CMD copy-paste, no web-portal upload.

It does **not** touch `refresh_availability.py` or `compute_availability.py`. It
runs the refresh as-is, validates the output, mirrors it into `docs\`, and pushes
to GitHub over HTTPS using the Contents API.

---

## 1. Files to install

Copy these three files into `C:\GRV-Availability`:

| File | Purpose |
|------|---------|
| `run_daily.py` | the wrapper that does everything |
| `github.json` | your GitHub token + repo settings (you edit this) |
| `run_daily.bat` | the launcher Task Scheduler will call |

Your `credentials.json` (NetSuite) stays exactly where it is. Nothing about
NetSuite auth changes.

---

## 2. Prerequisites (one-time)

1. Confirm `requests` is installed (the refresh already uses it):
   ```
   pip install requests
   ```
2. Find your Python path — you'll need it for the launcher if PATH is flaky:
   ```
   where python
   ```
   Note the full path it prints (e.g. `C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe`).

---

## 3. Create the GitHub token (fine-grained PAT)

1. GitHub → your profile → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Token name**: `GRV availability automation`.
3. **Expiration**: fine-grained tokens expire. Pick a date you'll remember to
   rotate (e.g. 1 year), and set a calendar reminder — when it expires, pushes
   will start failing and STATUS.txt will show a GitHub auth error.
4. **Resource owner**: select **GrivelSC**.
   - ⚠️ I'm not certain whether `GrivelSC` is an organization or a personal
     account. If it's an **organization**, the org may require fine-grained PATs
     to be enabled and/or an admin to approve this token before it works. If the
     token "works locally but pushes 403", that approval is the likely cause —
     you may want to verify this with whoever administers the org.
5. **Repository access**: **Only select repositories** → **GRV-Item-Availability**.
6. **Permissions** → **Repository permissions** → **Contents**: set to
   **Read and write**. (Metadata read is added automatically.) Nothing else.
7. **Generate token** and copy it (starts with `github_pat_`). You won't see it again.

---

## 4. Fill in `github.json`

Open `C:\GRV-Availability\github.json` and replace the placeholder token:

```json
{
  "token": "github_pat_xxxxxxxxxxxxxxxxxxxx",
  "owner": "GrivelSC",
  "repo": "GRV-Item-Availability",
  "branch": "main",
  "docs_path": "docs"
}
```

**Security:** `github.json` (like `credentials.json`) must stay local. The
`C:\GRV-Availability` folder is not a git clone, so there's no accidental-commit
risk through this tool. If you ever run `git init` in that folder for any reason,
add both files to `.gitignore` first.

---

## 5. Test in stages (before scheduling)

Run these from a Command Prompt in `C:\GRV-Availability`. Each stage adds risk,
so you isolate problems early.

**Stage A — gate only, no NetSuite, no GitHub** (uses the JSON already on disk):
```
python run_daily.py --skip-refresh --no-push
```
Expect: "Sanity gate passed", files mirrored to `docs\`, `STATUS.txt` = OK.
This proves the gate logic and your counts line up with `config.json`.

**Stage B — publish using existing JSON** (tests GitHub auth, no NetSuite run):
```
python run_daily.py --skip-refresh
```
Expect: "Pushed: ..." or "Unchanged, not pushing: ...". Open the live dashboard
and confirm it still loads. This is where a bad token / org-approval issue shows.

**Stage C — full run**:
```
python run_daily.py
```
Expect: the refresh runs (this takes the usual minute or two), gate passes,
files publish. Confirm the dashboard's "last updated" timestamp moves and that
`refreshed_by` now reads `scheduled`.

After any run, check `STATUS.txt` and `run_daily.log` if something looks off.

---

## 6. Schedule it (Windows Task Scheduler)

Open **Task Scheduler** → **Create Task** (not "Basic Task", so you get all tabs).

**General tab**
- Name: `GRV Availability daily refresh`.
- Leave **"Run only when user is logged on"** selected (recommended). This needs
  no stored password and inherits your normal PATH and network, so `python`
  resolves the same way it does in your shell. The trade-off: it runs only while
  you're logged in — which, for a daily refresh during work hours on your own
  machine, is usually fine.
  - *Alternative:* if you need it to run while logged off, switch to "Run whether
    user is logged on or not" — but then Windows stores your password and the
    service-context PATH may differ, which is exactly when you'd hardcode the
    full Python path in `run_daily.bat` (see the comment in that file).

**Triggers tab** → New
- Daily, at a time the machine is reliably on (e.g. 07:30).

**Actions tab** → New
- Action: **Start a program**
- Program/script: `C:\GRV-Availability\run_daily.bat`
- Start in: `C:\GRV-Availability`

**Conditions tab**
- If this is a laptop: uncheck "Start the task only if the computer is on AC power".
- Optionally check "Wake the computer to run this task".

**Settings tab**
- Check **"Run task as soon as possible after a scheduled start is missed"** so a
  day the machine was off still refreshes once it's back on.

Save. Right-click the task → **Run** to test the scheduled path end-to-end, then
check `STATUS.txt`, `run_daily.lastrun.txt`, and the dashboard.

---

## 7. Monitoring (your STATUS-file loop)

There is no email/alert by design. Your signal is the dashboard's "last updated"
timestamp:

- If the timestamp moved today → the run succeeded.
- If it's stale → open `C:\GRV-Availability\STATUS.txt`. On a failure it shows
  `FAILED`, the time, and the reason. On a gate rejection it tells you the
  expected vs actual SKU counts. `run_daily.log` has the full detail.

A failed run never publishes, so a stale-but-correct dashboard is always
preferable to a freshly-broken one.

---

## 8. Maintenance & rollback

- **Token rotation:** when the PAT nears expiry, generate a new one and paste it
  into `github.json`. Nothing else changes.
- **Rollback:** this tool only ever *adds* commits; it never deletes. To revert
  to the manual process, just disable the scheduled task — your old CMD + portal
  workflow still works unchanged.

---

## Notes / things worth verifying

- The GitHub Contents API headers and fields used here follow REST API version
  `2022-11-28`. They're stable, but if GitHub ever changes the contract a push
  would fail loudly (caught and written to STATUS.txt) rather than silently.
- Change comparison uses the standard git blob hash, so a file is pushed only
  when its content genuinely differs from what's on GitHub. `metadata.json`
  changes every run (new timestamp), so it always publishes; `config.json`
  publishes only when the analysis domain actually changes.
