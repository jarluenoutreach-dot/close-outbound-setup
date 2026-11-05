# End-to-end test with your own phone

Run this after `doctor`, `dry-run` and `setup-webhook`, before going live. It takes about 15 minutes. You'll run the service in the foreground so you can watch every step.

## 0. Pick a time
Do this between **8am and 8pm in your own time zone** so the text sends right away. (Part B tests the after-hours path.)

## 1. Turn on test mode
Once the webhook exists, any lead a teammate touches in Webinar Workflow would be processed for real. Test mode makes the service ignore every lead except ones with your number:

```bash
sudo nano /opt/close-outbound-setup/.env
# add this line (your mobile number):
TEST_PHONES=+15551234567
```
Every other lead is logged as `TEST MODE: ignoring lead ...` and left alone. Remove the line before going live (step 7).

## 2. Start the service in the foreground
```bash
sudo systemctl stop close-outbound 2>/dev/null; cd /opt/close-outbound-setup
sudo -u outbound .venv/bin/python -m outbound serve --verbose
```
You should get a Telegram message: *"Close outbound service started … TEST MODE (only +1…)"*. Leave this window open and use a second SSH window for the next steps.

Check HTTPS from your laptop: open `https://texts.yourdomain.com/health`. It should show `{"ok":true,...}`.

## 3. Create the test lead in Close
1. Create a new lead named something like **"ZZ Test Jarluen"**.
2. Add a contact named **"Jarluen Test"** with **your own mobile number**.
3. Make it match the **Webinar Workflow** smart view's filters (same status, tags or fields a real webinar lead would have). Then open the smart view and confirm the lead is listed.

## 4. Watch the logs (foreground window)
Within a few seconds you should see:
```
QUEUED ZZ Test Jarluen (lead_...) +1XXXXXXXXXX tz=America/... version N
SENT ZZ Test Jarluen (lead_...) +1XXXXXXXXXX version N sms=acti_...
```
If you see `SKIP ... rule N: <reason>` instead, the reason says what to fix. For example, rule 7 means you've texted this number from Close before; use a fresh lead and contact.

## 5. Confirm the results
- [ ] Your phone receives the text: *"Hey Jarluen! …"* (or *"Hi Jarluen! …"*) from +1 978-666-9462.
- [ ] In Close, the lead shows the SMS and a note: **"First text sent: Version N"**.
- [ ] `outbound status` shows 1 sent.
- [ ] About 2 minutes later the log shows no error for that lead. (If the carrier rejects the text, you'll get a Close task "First text failed: …".)
- [ ] Reply to the text from your phone, then send `/report` to the bot: it should show **1 sent, 1 reply**.

## 6. Test the eligibility block
Create a **second** test lead with the **same phone number** and put it in Webinar Workflow. Expected:
- Log: `SKIP ... rule 14: possible duplicate of ZZ Test Jarluen ...`
- A Close task on the second lead: *"Possible duplicate of ZZ Test Jarluen, check before texting."*
- No second text arrives.

## Part B: after-hours path (optional, do it in the evening)
After 8pm your time, add a **different** phone you control (a family member's number, with permission) to `TEST_PHONES` (comma-separated), restart the foreground service, and create a third test lead with that number:
- Log: `SCHEDULED ... for <tomorrow> 08:00:00 AM CST` (or 8am local if the number is Mountain or Pacific).
- In Close, the SMS shows as scheduled.
- At 7:55am the log shows `Morning check passed`. At 8:00am the text arrives, the record changes to sent, and the "First text sent" note is added.

To test the morning cancel, reply from that phone the evening before (or set the lead's status to DNC). At 7:55am the log shows `UNSCHEDULED ... rule 1` (or rule 6), and the scheduled SMS disappears from Close.

## 7. Clean up and go live
1. Stop the foreground service (Ctrl+C).
2. Remove the `TEST_PHONES=` line from `.env`.
3. Delete or archive the test leads in Close.
4. Run `outbound dry-run` one more time and check the "WOULD TEXT NOW" list.
5. If you only want leads that enter the view **from now on** to be texted, run `outbound baseline --yes`. It marks everything already in Webinar Workflow as "leave alone".
6. Go live:
   ```bash
   sudo systemctl enable --now close-outbound
   journalctl -u close-outbound -f
   ```

Kill switch at any time: `/stop` in Telegram or `outbound stop` on the server.
