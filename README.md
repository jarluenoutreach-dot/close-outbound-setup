# Close Outbound Setup: first-text service

A small Python service for your VPS. When a new lead lands in the **Webinar Workflow** smart view in Close, it checks the lead against the team's lead ownership rules. If the lead passes, it sends the first outreach SMS from **+1 978-666-9462** right away, or schedules it for the next morning if it's after hours where the lead lives. It runs 24/7 under systemd behind HTTPS (Caddy + Let's Encrypt).

```
Close webhook ──HTTPS──▶ Caddy ──▶ FastAPI (127.0.0.1:8000)
 (signed)                              │
                                       ├─ wait 5s → in smart view? → eligibility check
                                       │     ├─ 8am–8pm local → send queue → worker (15s gap, 60/hr) → SMS "outbox" + note
                                       │     └─ 8pm–8am local → SMS "scheduled" for 8am (Central / local in the West)
                                       ├─ every 5 min: backup sweep of the smart view
                                       ├─ 5 min before each scheduled text: morning re-check (deletes it if the lead fails)
                                       ├─ 2 min after each send: backup error check
                                       ├─ Monday 9am Central: weekly report → Telegram
                                       └─ Telegram bot: /report /status /stop /resume
```

## Eligibility check

The webhook step, the sweep, the check right before every send, and the morning check all run the same check. A lead is **skipped** if any rule below applies, and the rule number is logged:

| Rule | Skip when | Rulebook |
|---|---|---|
| 1 | Lead sent any incoming SMS in the last 60 days | 2 & 4 |
| 2 | Another setter had a call of 120s+ with the lead in the last 60 days | 1 |
| 3 | A meeting booked or held in the last 60 days (any status, any user) | 3 |
| 4 | Anyone has a scheduled or outbox SMS to this lead right now | |
| 5 | Another setter texted the lead in the last 7 days (`OTHER_SETTER_TEXT_DAYS`) | |
| 6 | Status is DNC, Wrong Number, Bad Data, Not Interested, Customer, Student, Deposit, Wanting Refund or No Show | |
| 7 | You already texted this lead (local database, or any outgoing SMS from your user ID) | |
| 8 | No contact has a phone number | |
| 14 | Another lead with the same phone fails the check. You also get a task: *"Possible duplicate of [lead name], check before texting."* | 14 |

Other skip reasons that can appear: `view` (left the smart view before sending), `gone` (lead deleted), `deleted` (someone deleted the scheduled SMS in Close), `manual` (`cancel-scheduled`), `api` (Close kept failing for one lead).

The text goes to the first contact that has a phone. The time zone comes from that phone's area code (US Central if unknown). If an area code covers more than one zone, the text only goes out when it's 8am–8pm in all of them.

## Safety

- **Kill switch:** while a file named `STOP` exists in the project folder, nothing is sent or scheduled. Create it with `outbound stop` or `/stop` on Telegram. Remove it with `outbound resume` or `/resume`. Texts that are *already scheduled in Close* still go out; run `outbound cancel-scheduled --yes` to delete them.
- **Errors:** a failed text is never retried. You get a Close task: *"First text failed: [error]"*. If **3 or more texts fail within one hour**, the service creates `STOP` and sends you a Telegram alert.
- **Signed webhooks only:** every webhook's `close-sig-hash` is checked against the saved signature key, and anything unsigned or invalid gets a 401.
- **Logging:** every send, schedule, skip and error is logged with its reason to `logs/outbound.log`, to journald, and to the `events` table in `data/outbound.db`.
- **Dry run:** `outbound dry-run` (one-shot) or `serve --dry-run` (the full service) does everything except write to Close.
- **Test mode:** `TEST_PHONES=+1…` in `.env` makes the service ignore every lead except ones with those numbers.

---

## Setup on the VPS

Assumes Ubuntu or Debian, a domain or subdomain (e.g. `texts.yourdomain.com`), and ports 80 and 443 open.

### 1. Point the domain at the VPS
At your DNS provider, add an **A record** for the subdomain pointing to the VPS's IP address. Wait until `ping texts.yourdomain.com` shows that IP.

### 2. Clone and install
```bash
sudo git clone https://github.com/jarluenoutreach-dot/close-outbound-setup.git /opt/close-outbound-setup
cd /opt/close-outbound-setup
sudo ./deploy/install.sh texts.yourdomain.com
```
The repo is private, so GitHub will ask for credentials. Use your GitHub username and a [personal access token](https://github.com/settings/tokens) as the password.

This installs Python, Caddy (which fetches the HTTPS certificate automatically) and the `close-outbound` systemd unit, plus an `outbound` command. **Nothing is started yet.**

### 3. Close API key
In Close, go to **Settings → Developer → API Keys** and create a key **while logged in as yourself**. Texts are sent as the key's owner. Put it in `.env`:
```bash
sudo nano /opt/close-outbound-setup/.env      # CLOSE_API_KEY=...
```

### 4. Telegram bot
1. In Telegram, open **@BotFather** and send `/newbot`.
2. Pick a display name (e.g. *Close Outbound*) and a username ending in `bot` (e.g. `jarluen_outbound_bot`).
3. BotFather replies with a token like `123456789:AA...`. Put it in `.env` as `TELEGRAM_BOT_TOKEN=`.
4. Open your new bot, press **Start**, and send it any message.
5. Run `outbound telegram-chat-id`. It prints `TELEGRAM_CHAT_ID=...`. Copy that line into `.env`.
6. Optional: in BotFather, send `/setcommands`, choose your bot, and paste:
   ```
   report - first-text report for the past 7 days
   status - queue, scheduled, sent and errors
   stop - stop sending (kill switch)
   resume - resume sending
   ```

The bot only obeys commands from `TELEGRAM_CHAT_ID`. Anyone can send `/chatid` to see their own chat's ID.

### 5. Check everything
```bash
outbound doctor
```
This tests the API key (and confirms it's yours), the sending number, the smart view and its membership check, activity reads, the duplicate phone search, the webhook key and Telegram. Fix anything marked **FAIL** before continuing.

### 6. Dry run, then review the results
```bash
outbound dry-run
```
This prints every lead in Webinar Workflow and what would happen to it: text now (with version), schedule for the morning (with time), or skip (with rule and reason). It also saves a CSV to `data/`. **Nothing is sent or written to Close.**

> ⚠️ **Read the "WOULD TEXT NOW" count carefully.** On go-live, the backup sweep texts *every* eligible lead currently in the smart view, not just new ones. If you only want leads that arrive from now on, run `outbound baseline --yes` right before going live. It marks every lead already in the view as "leave alone". (`outbound baseline --undo` reverses it.)

### 7. Create the Close webhook
```bash
outbound setup-webhook --url https://texts.yourdomain.com/webhooks/close
```
This subscribes to *lead created*, *lead updated* and *SMS activity updated*, and saves the signature key to `.env`.

### 8. Test end to end
Follow **[TESTING.md](TESTING.md)**: test mode plus a test lead with your own phone number, watched through the whole flow.

### 9. Go live
```bash
sudo systemctl enable --now close-outbound
journalctl -u close-outbound -f          # watch it work
```
`enable` makes it start on boot. `Restart=always` restarts it within 5 seconds after a crash. You'll get a Telegram message every time it starts.

---

## Day to day

| Command | What it does |
|---|---|
| `outbound status` | Queue, upcoming scheduled texts, recent errors |
| `outbound report [--send]` | Report for the past 7 days (`--send` posts it to Telegram) |
| `outbound stop` / `outbound resume` | Kill switch |
| `outbound cancel-scheduled [--yes]` | Delete every scheduled first text from Close |
| `outbound dry-run [--limit N]` | One-shot simulation of the smart view |
| `outbound baseline [--yes \| --undo]` | Leave leads already in the view alone |
| `outbound list-webhooks` / `delete-webhook ID` | Manage Close webhooks |
| `journalctl -u close-outbound -f` | Live logs |
| `sudo ./deploy/update.sh` | Pull new code and restart |

**Weekly report** (Mondays 9am Central, or any time with `/report`). For leads texted in the past 7 days, broken down by message version: texts sent, replies after the first text, reply rate, booked calls (meetings created after the text), and leads moved to Qualified/Customer/Deposit. It also shows total errors and skips by rule.

**Settings** such as the 7-day rule 5 window, send window and rate limits live in `.env`; see `.env.example`. Restart after changing them: `sudo systemctl restart close-outbound`.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
The tests run the whole flow against a fake Close: every rule, scheduling and spacing, the morning check, errors and auto-STOP, duplicates, dry run, test mode, signature checks and the HTTP client's retries and fallbacks.

| File | Purpose |
|---|---|
| `outbound/engine.py` | Webhook → check → queue/schedule → send → verify → morning check → sweep |
| `outbound/eligibility.py` | Rules 1–8 (pure functions) |
| `outbound/close_api.py` | Close REST client |
| `outbound/app.py` | FastAPI webhook endpoint, background loops, Telegram commands |
| `outbound/report.py` | Weekly report |
| `outbound/__main__.py` | CLI |
| `deploy/` | install script, systemd unit, Caddy config |
