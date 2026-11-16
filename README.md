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

## Webinar no-reply nudge

24 hours after a first text goes out, a lead who hasn't answered gets **one** nudge from the same number, 8am–8pm in the lead's time zone:

> Hey {name}, just following up on my text yesterday. Are you looking to start a courier business from scratch, or grow one you already run?

**No nudge, ever, if the lead has replied to anyone on the team before:** any inbound text or inbound call from the lead, to any number, at any time. A phone conversation of 2+ minutes with anyone also counts. It's also skipped when:
- another setter texted the lead after your first text
- the lead has a meeting
- the lead has a blocked status
- the first text failed

Only first texts sent after the nudge went live are nudged; older leads are left alone.

### Day 3 and Day 7 follow-ups
Leads who got the nudge and still never answered anyone get two more texts, timed from the first text and sent **Mon–Sat, 9am–7pm in the lead's time zone** (`NOREPLY_*` in `.env`):

| Day | Text |
|---|---|
| 3 | Hey {name}, Jarluen again from Daeron's team. Still thinking about the courier business? Happy to answer any questions. |
| 7 | {name}, is starting a courier business still something you want this year, or should I check back later? |

Each one is re-checked with the same rules as the nudge, so any reply, inbound call, phone conversation, meeting, another setter's text or blocked status stops them. Only leads first texted after the follow-ups went live get them. Wording is in `outbound/noreply.py`; `NOREPLY_ENABLED=false` turns them off. It adds a note on the lead, obeys the STOP file and `/timer`, and shares the send pacing. Turn it off with `NUDGE_ENABLED=false` in `.env`; change the delay with `NUDGE_AFTER_HOURS`. The wording is in `outbound/nudge.py`.

---

## Follow-up sequences (nurture + SS prep)

The same service also runs two follow-up sequences. They replace Close workflows: nothing is set up in Close except two **private** smart views, and every text goes out from **+1 978-666-9462** as you (the API key's owner). Both are **on** by default; set `NURTURE_ENABLED=false` or `PREP_ENABLED=false` in `.env` to turn one off.

**Send window:** Mon–Sat, 9am–6pm Central (`SEQ_WINDOW_*`). A text that comes due outside the window waits for the next opening. The 15s gap / 60-per-hour cap and the `STOP` file are shared with the first texts.

### Whose leads
A lead is yours when its **Setter Lead Owner is you**, or the field is blank and **you own an opportunity** on it. A lead whose Setter Lead Owner is anyone else never gets these texts, even if you own the opportunity. This is checked when the lead is enrolled and again before every text.

When a sequence switches to a different smart view, everyone already in the new view is left alone, the same as on the first run.

### TC Completed nurture: 8 texts over 15 days
Watches **Jarluen – TC Completed Nurture (SMS)**: leads you texted and had your call with, with an opportunity *you own* in **TC Completed**, minus Bad Fit, DNC, Wrong Number, Bad Data, Student, Customer and Deposit, and anyone active in a Close workflow.

| Day | Text |
|---|---|
| 0 (as soon as the lead enters the view) | Intro: "Hey {name}, it's Jarluen from Daeron's team…" |
| 1 | Nudge: "…just making sure you saw my text yesterday…" (only if they haven't replied) |
| 2 | FedEx driver to $10k/month video |
| 4 | "What's the #1 thing holding you back…" |
| 6 | Diamond Program breakdown |
| 8 | Daeron's $65k video |
| 11 | "Want me to check what's open on the calendar…" |
| 15 | "Should I close out your file…" |

**Waits while a call is booked:** a lead with an upcoming meeting isn't started (and a running nurture stops), so nobody gets "want a few times?" with a call already on the calendar.

**Stops when:** the lead texts back, calls in, gets a meeting booked, the lead status changes, an opportunity moves to SS Booked, or the lead leaves the smart view. Before every text the service also re-checks that you still own a TC Completed / TC Booked / SS Completed opportunity on the lead.

### SS prep: slides before the Strategy Session
Watches **Jarluen – Booked Calls (prep texts)**: your leads with an **upcoming meeting** or an opportunity in **SS Booked**. The opportunity stage doesn't matter, so a call booked while the opportunity stays in TC Completed still counts.

The service reads the upcoming meeting from Close:
- **Meeting 2+ days out:** slides text right away, then "did you get a chance to go through the slides?" a day later. The check-in is skipped if the call is less than 3 hours away by then.
- **Meeting within 2 days:** the slides text only.
- **No meeting in Close yet:** it waits up to 30 minutes for the calendar to sync, then sends the slides text only.

**Stops when:** the lead texts back, calls in, the lead status changes, the call time passes, or the lead leaves the view (for example the opportunity moves to SS Completed). A lead that gets booked while in the nurture is moved over: the nurture stops and the prep texts start.

**Only new leads:** the first time each sequence runs after you turn it on, everyone already in its smart view is marked "leave alone" and never texted. Only leads that join the view after that get the texts. (`outbound seq-baseline --undo` would release them.)

Each lead goes through each sequence **once**. Every text adds a note on the lead ("Booked nurture text 2/8 (nudge) sent"). A failed text stops that lead's sequence and creates a Close task; 3 failures in an hour creates `STOP`.

### Going live
```bash
outbound seq-preview                 # who is in each view; nothing is sent
sudo ./deploy/update.sh              # pulls the code and restarts: both sequences start
outbound seq-status                  # active runs and the next text for each
```

| Command | What it does |
|---|---|
| `outbound seq-preview [nurture\|prep]` | Dry run of both views |
| `outbound seq-status` | Counts, active runs, next text times |
| `outbound seq-stop LEAD_ID [--block]` | Stop one lead (`--block` keeps it out for good) |
| `outbound seq-baseline [nurture\|prep] --yes` / `--undo` | Leave leads already in the view alone |

Message wording lives in `outbound/sequences.py`. Edit it there, then run `sudo ./deploy/update.sh` after pushing.

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
| `outbound/sequences.py` | Nurture and SS prep messages, steps and Close IDs |
| `outbound/seq_engine.py` | Sequence enroll → re-check → send → verify |
| `outbound/__main__.py` | CLI |
| `deploy/` | install script, systemd unit, Caddy config |
