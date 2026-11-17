# Driver Outreach

A small private website that sends **one recruiting email per driver** from 2–3 Gmail accounts, slowly and safely, and writes the result into the **NOTES** column of your driver list (Excel, CSV or Google Sheet).

How it works:

- You open the site in a browser, choose a list, write the message, check the preview, send a test to yourself, and press **Start**.
- The server then sends in the background (you can close the browser): only Mon–Fri 9:00–16:00 US Central (20:00–03:00 Tashkent in winter, 19:00–02:00 in US summer), with a random 1.5–5 minute gap between emails from each account, and never more than each account's daily cap.
- Your message is sent exactly as you write it. The only thing the tool adds is one line at the bottom, "Not interested? Reply 'unsubscribe' or click: <link>", plus Gmail's unsubscribe headers. Replies, bounces and unsubscribes are picked up from the Gmail inboxes every 10 minutes; those drivers are never emailed again automatically.
- The NOTES cell of each row gets `Sent 11/18/2026`, `Not sent - duplicate`, `Replied 11/19/2026`, and so on. Existing notes are kept: new notes are added after a ` | `.

---

## Contents

1. [Create the Oracle Cloud account](#1-create-the-oracle-cloud-always-free-account)
2. [Create the server (VM)](#2-create-the-server-vm)
3. [Open ports 80 and 443 in Oracle](#3-open-ports-80-and-443-in-oracle)
4. [Get a free web address (DuckDNS)](#4-get-a-free-web-address-duckdns)
5. [Install the tool on the server](#5-install-the-tool-on-the-server)
6. [Gmail: 2-Step Verification + App Password](#6-gmail-turn-on-2-step-verification-and-create-an-app-password)
7. [Google Sheets: the service account](#7-google-sheets-create-the-service-account)
8. [First login and Settings](#8-first-login-and-settings)
9. [Sending a campaign](#9-sending-a-campaign)
10. [Update, back up, restart, logs](#10-update-back-up-restart-logs)
11. [Troubleshooting](#11-troubleshooting)
12. [For developers](#12-for-developers)

---

## 1. Create the Oracle Cloud "Always Free" account

1. Go to **https://www.oracle.com/cloud/free/** and click **Start for free**.
2. Fill in your details. Oracle asks for a credit card **only to verify you**. Always Free resources are not charged.
3. **Home region** (you can't change it later): pick a US region. If one region keeps saying "out of capacity" in step 2, that is normal for free servers. Some people pick a less busy region such as *US West (Phoenix)* or *US Midwest (Chicago)*.
4. Wait for the "Your account is ready" email, then sign in at **https://cloud.oracle.com**.

## 2. Create the server (VM)

1. In the Oracle console, click the **☰ menu** (top-left) → **Compute** → **Instances** → blue **Create instance** button.
2. **Name**: `driver-outreach`.
3. **Image and shape**: click **Edit**.
   - **Image** → **Change image** → choose **Canonical Ubuntu** → version **24.04** (22.04 also works) → **Select image**.
   - **Shape** → **Change shape** → **Ampere** → tick **VM.Standard.A1.Flex** → set **1 OCPU** and **6 GB memory** → **Select shape**. The label says "Always Free-eligible".
   - If you later get **"Out of capacity"**, come back here and choose **Specialty and previous generation** → **VM.Standard.E2.1.Micro** instead (1 GB memory; the tool is built to fit).
4. **Networking**: leave **Create new virtual cloud network** and **Create new public subnet** selected. Make sure **Assign a public IPv4 address** is ON.
5. **Add SSH keys**: choose **Generate a key pair for me** and click **Save private key** (and **Save public key**). Keep the downloaded `.key` file safe: it is the key to your server.
6. Click **Create**. After 1–2 minutes the yellow box turns green: **RUNNING**.
7. On the instance page, copy the **Public IP address** (looks like `129.153.x.x`). You'll need it in steps 4 and 5.

## 3. Open ports 80 and 443 in Oracle

Browsers reach the site on ports 80/443. Oracle blocks them by default in two places: the cloud firewall (here) and inside the server (`install.sh` handles that one).

1. On the instance page, find **Primary VNIC** → click the **Subnet** link (e.g. `subnet-2026…`).
2. Open the **Security** tab (older screens: **Security Lists** on the left) → click **Default Security List for …**.
3. Click **Add Ingress Rules** and fill in:
   - **Source CIDR**: `0.0.0.0/0`
   - **IP Protocol**: `TCP`
   - **Destination Port Range**: `80`
   - Click **+ Another Ingress Rule** and add the same with port `443`.
4. Click **Add Ingress Rules**. You should now see rules for 22, 80 and 443.

## 4. Get a free web address (DuckDNS)

1. Go to **https://www.duckdns.org** and sign in (with Google is fine).
2. In **sub domain**, type a name, e.g. `abclogistics-recruit`, and click **add domain**.
3. In the **current ip** box next to it, paste your server's **Public IP** and click **update ip**.
4. Copy the **token** shown at the top of the page. `install.sh` asks for it, so the address keeps pointing to your server.

Your site will be **https://abclogistics-recruit.duckdns.org**.

## 5. Install the tool on the server

**Connect to the server** (from Windows, open *PowerShell*):

```powershell
# one time: make the key private (Windows refuses keys that others can read)
icacls "C:\Users\YOU\Downloads\ssh-key-2026-11-18.key" /inheritance:r /grant:r "$($env:USERNAME):R"
ssh -i "C:\Users\YOU\Downloads\ssh-key-2026-11-18.key" ubuntu@YOUR_PUBLIC_IP
```

Type `yes` the first time. You are on the server when the prompt shows `ubuntu@driver-outreach:~$`.

**Copy the tool to the server.** In a *second* PowerShell window on your PC, from the folder that contains `email_sender_for_cdl`:

```powershell
tar -czf outreach.tgz --exclude=.venv --exclude=data --exclude=dryrun-output --exclude=.git email_sender_for_cdl
scp -i "C:\Users\YOU\Downloads\ssh-key-2026-11-18.key" outreach.tgz ubuntu@YOUR_PUBLIC_IP:~
```

**Run the installer** (back in the server window):

```bash
tar -xzf outreach.tgz && cd email_sender_for_cdl
sudo bash install.sh
```

It asks for:

- your DuckDNS address (e.g. `abclogistics-recruit.duckdns.org`)
- a login username and password for the website (at least 10 characters; it is stored only as a hash)
- your DuckDNS token (optional, but recommended)

It then adds a 2 GB swap file, installs everything, opens ports 80/443 inside the server, gets an HTTPS certificate, and starts two services that restart by themselves after crashes and reboots. At the end it prints **Done! Open https://…**.

## 6. Gmail: turn on 2-Step Verification and create an App Password

Do this **for each** Gmail account (use dedicated accounts, not anyone's main inbox).

1. Sign in to the Gmail account in a browser and open **https://myaccount.google.com/security**.
2. Under **How you sign in to Google**, click **2-Step Verification** → **Turn on**. Add a phone number and confirm the code.
3. Open **https://myaccount.google.com/apppasswords** (you may need to sign in again).
4. In **App name** type `Driver Outreach` → **Create**.
5. A yellow box shows a **16-letter password** like `abcd efgh ijkl mnop`. Copy it now (Google shows it only once).
6. In the tool: **Settings → Gmail accounts** → enter the Gmail address, the **Sender name (shown in the inbox)**, and paste the App Password → **Add & test**. The tool checks sending (SMTP) and inbox reading (IMAP) before saving.
   - Use a real person's name for the sender name, e.g. `Mike - ABC Logistics`. You can change it later in the account's row. **An account without a sender name doesn't send** (the other accounts continue); Settings and the progress page say so in red.

Notes:

- If you change the Google password of that account, Google cancels its App Passwords. The tool stops that account and shows a red alert. Create a new App Password and use **New App Password** in Settings.
- "App passwords" page missing? 2-Step Verification isn't on yet, or the account uses Advanced Protection.

## 7. Google Sheets: create the service account

This gives the tool its own Google "robot" email address. You share a sheet with it as Editor, just like sharing with a person. It's free and needs no billing.

1. Open **https://console.cloud.google.com** (signed in with any Google account; a company Gmail is fine).
2. At the top, click the project picker → **New project** → name `driver-outreach` → **Create**. Make sure the new project is selected at the top.
3. **☰ menu → APIs & Services → Library** → search **Google Sheets API** → **Enable**. Go back to the Library and also enable **Google Drive API**.
4. **☰ menu → IAM & Admin → Service Accounts** → **+ Create service account** → name `sheets-writer` → **Create and continue** → skip the optional role steps → **Done**.
5. Click the new service account (its email looks like `sheets-writer@driver-outreach-12345.iam.gserviceaccount.com`) → **Keys** tab → **Add key → Create new key → JSON → Create**. A `.json` file downloads.
6. Put it on the server (PowerShell on your PC):
   ```powershell
   scp -i "C:\path\to\ssh.key" "C:\Users\YOU\Downloads\driver-outreach-12345-abc.json" ubuntu@YOUR_PUBLIC_IP:~/sa.json
   ```
   then on the server:
   ```bash
   sudo mv ~/sa.json /etc/driver-outreach/google-service-account.json
   sudo chown root:outreach /etc/driver-outreach/google-service-account.json
   sudo chmod 640 /etc/driver-outreach/google-service-account.json
   sudo systemctl restart outreach-web outreach-worker
   ```
7. **Settings → Google Sheets** in the tool now shows the robot's email. For each driver sheet: **Share** → paste that email → role **Editor** → untick "Notify people" → **Share**.

Keep the JSON file private. Anyone who has it can edit the sheets shared with it.

## 8. First login and Settings

Open your `https://…duckdns.org` address and log in. Then in **Settings**:

1. **Gmail accounts**: add 2–3 (step 6), each with its sender name.
2. **Sending window**: default Mon–Fri 9:00–16:00 Central. The matching Tashkent time is shown underneath.
3. **Daily caps**: default warm-up is 20 → 40 → 60 → 80 per account per day (weeks 1, 2, 3, 4+), counted over a rolling 24 hours. Keep them. Above 100/day a free Gmail is likely to be locked.
4. **"Send test to myself" address**: your own inbox.

## 9. Sending a campaign

1. **New campaign** → choose an **Excel/CSV file** or paste a **Google Sheets link**.
   - In **Chrome or Edge** the file picker asks for permission to **edit** the file. Allow it: then the tool can write the NOTES back into your original file on your computer.
2. **Columns**: the tool finds the header row (first 10 rows) and the columns (Name / Full Name / Driver Name, First + Last Name, Email / E-mail, Phone, Position / Driver Type, Notes / Comments). Check them. Anything it couldn't find is shown in red with **Please choose**. If there is no NOTES column, it says where it will add one (the first completely empty column to the right of your data; no other column is touched).
   - **Skip drivers whose NOTES already say Sent** is ticked by default. Untick it to email them again (unsubscribed/bounced/replied drivers are always skipped).
3. **Message**: subject + plain text. Paste the **whole** message, including your signature, company name, MC/DOT and phone: the tool adds nothing except the unsubscribe line. If the text has no postal address (street number + state/ZIP), a yellow note reminds you that US law (CAN-SPAM) may require one in a commercial email; you can still send. Click a placeholder to insert it: `{first_name}` (empty → "there", so "Hi {first_name}," becomes "Hi there,"), `{name}`, `{last_name}`, `{position}`. The **Content check** box updates as you type: yellow = advice, red = must fix (for example, a subject starting with "Re:"/"Fwd:" is not allowed).
4. **Check & start**: summary (rows, duplicates, invalid, already unsubscribed/bounced, already Sent, **will send**, estimated finish date), suggested fixes for typos like `gmial.com`, a preview of 3 random drivers exactly as they will get it, and **Send test to myself**. Then **Start sending**.
5. **Progress page**: live counters, per-account numbers, **Pause / Resume / Stop**, **Download log (CSV)**.
   - Excel/CSV lists: **Save marks to Excel file** writes the notes into your original file (it also saves automatically every 3 minutes while the page is open). If the file is open in Excel, close it and click again. **Download marked copy** always works, in any browser.
   - Google Sheets: notes are written automatically in small batches. If someone sorts the sheet during the campaign, rows are found again by email. If a row was deleted, the page lists that note instead of writing it somewhere wrong.

**Automatic safety stops**

- Bounce rate above 3% (after 30+ emails) or unsubscribes above 2% (after 50+) → the campaign pauses and tells you why. Fix the list or message, then press **Resume**.
- Gmail says "daily limit" → that account rests for 24 hours; the others continue.
- Gmail refuses the password or flags the account → that account stops immediately (red alert) and is **never** retried automatically. Sign in to that Gmail in a browser, clear any security warning, make a new App Password, then use **New App Password** in Settings.

**What the tool deliberately does not do:** no attachments, images, tracking pixels or click tracking; no hidden text or tricks; no fake "Re:/Fwd:" subjects; no follow-up sequences; no buying or scraping lists.

## 10. Update, back up, restart, logs

Connect with `ssh` as in step 5, then:

| Task | Command |
|---|---|
| Update to a new version | copy the new folder to the server as in step 5, then `cd email_sender_for_cdl && sudo bash install.sh` (keeps settings and data) |
| Restart | `sudo systemctl restart outreach-web outreach-worker` |
| Status | `systemctl status outreach-web outreach-worker` |
| Live logs | `sudo journalctl -u outreach-worker -f` (Ctrl+C to quit) |
| Back up now | `sudo /etc/cron.daily/driver-outreach-backup` |
| Download backups to your PC | `scp -i KEY -r ubuntu@IP:/var/lib/driver-outreach/backups .` (run `sudo chmod -R o+r /var/lib/driver-outreach/backups` first, then undo with `o-r`) |

Backups run **every day automatically** (database + uploaded lists) into `/var/lib/driver-outreach/backups`, keeping 14 days.

**Restore** a backup:

```bash
sudo systemctl stop outreach-web outreach-worker
sudo cp /var/lib/driver-outreach/backups/outreach-2026-11-18.db /var/lib/driver-outreach/outreach.db
sudo rm -f /var/lib/driver-outreach/outreach.db-wal /var/lib/driver-outreach/outreach.db-shm
sudo chown outreach:outreach /var/lib/driver-outreach/outreach.db
sudo systemctl start outreach-web outreach-worker
```

**Dry-run mode** (test without sending): `sudo nano /etc/driver-outreach/outreach.env`, set `DRY_RUN=1`, save (Ctrl+O, Enter, Ctrl+X), restart. A yellow banner appears; emails are saved as files in `/var/lib/driver-outreach/outbox/` and the notes are still written. Set `DRY_RUN=0` and restart to send for real.

**Change the website password:**

```bash
cd /opt/driver-outreach && sudo ./venv/bin/python -c "from core.crypto import hash_password; import getpass; print(hash_password(getpass.getpass('New password: ')))"
```

Copy the line it prints, put it after `ADMIN_PASSWORD_HASH=` in `/etc/driver-outreach/outreach.env`, and restart.

## 11. Troubleshooting

| Problem | What to do |
|---|---|
| Site doesn't open | Check step 3 (ports 80/443) and that DuckDNS shows the server's current IP. `sudo journalctl -u caddy -n 50` shows certificate errors. |
| "Out of capacity" creating the VM | Try another availability domain, try again later, or use `VM.Standard.E2.1.Micro`. |
| Add account fails: "Username and Password not accepted" | Use the 16-letter **App Password**, not the normal Gmail password. Check that 2-Step Verification is on. |
| Add account fails on IMAP | In Gmail: Settings ⚙ → See all settings → Forwarding and POP/IMAP → Enable IMAP (if that option is shown). |
| "The tool can't open this sheet" | Share the sheet with the service-account email as **Editor** (step 7). |
| Nothing is sending | The progress page says why: outside the sending window, all accounts at their cap, accounts without a sender name, or the campaign is paused. |
| Emails land in Spam | Shorten the message, use at most one link, avoid ALL CAPS/"!!!", and keep the daily caps low. Replies from drivers help the most. |
| Notes not appearing in the Sheet | The progress page shows "waiting to be written" and the last error. Notes are kept and retried; nothing is lost. |

## 12. For developers

```
app/        FastAPI web app (server-rendered Jinja templates, vanilla JS in app/static)
core/       shared logic: db (SQLite), list I/O (openpyxl/csv/gspread), notes rules, column detection,
            email checks, rendering, content checker, caps, compose, SMTP/IMAP, classification
worker/     python -m worker  → sender + IMAP watcher + notes flusher threads
tests/      pytest
tools/dryrun_demo.py   end-to-end dry run on 20 fake drivers (Excel with/without NOTES + simulated Sheet)
deploy/     systemd units, backup script
install.sh  server install/update
```

Local run (Windows/macOS/Linux, Python 3.11+):

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # (bin/pip on Linux/macOS)
cp .env.example .env    # fill APP_SECRET, FERNET_KEY, ADMIN_PASSWORD_HASH (commands inside)
.venv/Scripts/python -m pytest
.venv/Scripts/uvicorn app.main:app --reload       # web on http://localhost:8000
.venv/Scripts/python -m worker                    # background worker
.venv/Scripts/python tools/dryrun_demo.py         # writes dryrun-output/
```

Key guarantees and where they live:

- **No double sends:** `worker/sender.py`. A row becomes `sending` (with a new Message-ID) before the SMTP call and `sent` after. Rows stuck in `sending` are looked up in that account's IMAP *Sent Mail* (by Message-ID, then by recipient) before any retry. If IMAP can't be reached, the row is left alone.
- **Notes never lost:** `core/marks.py`. Every note is queued in SQLite and only marked written after the list accepts it. Rows are found by row number + email, or by email if moved.
- **Suppression checked before every send:** `Sender.claim_next`.
