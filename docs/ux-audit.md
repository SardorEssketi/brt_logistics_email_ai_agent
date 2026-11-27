# Usability audit and redesign plan

**Scope:** the web interface (`app/`), used by recruiting staff who are not developers.
**Method:** read every route, template, the CSS and JS, and the parts of `core/` and `worker/` the screens report on. Then I used a dry-run copy of the app with fake data: Gmail accounts in every state (ready, resting, stopped, no sender name), campaigns in every status and a messy CSV list (title row above the headers, duplicates, invalid, empty, role and typo addresses, blank rows, extra columns). I went through every flow at 1366 px and 390 px and took screenshots with a headless browser. The screenshots are in the pull request.

The sending engine was not part of the audit and is not changed. See "Not changed" at the end.

---

## Problems found, ranked by impact on the user

### 1. First use leads into a dead end (high)
After first login, the home page offers **"Start your first campaign"**, but no Gmail account exists yet. A new user goes through all four setup steps, writes the message, and only on the last step reads *"No Gmail account yet. Add one in Settings."* Accounts are the real first step, but the page only shows a small amber line with a link. Adding an account also needs an App Password, and the page only says "README part 6". The person adding it can't see how to make one without opening the README.

### 2. Alerts pile up, repeat themselves and don't say what to do (high)
- Every account error, auto-pause and notes problem is logged as a separate box on the home page and stays until dismissed one by one. In the test data, four red/amber boxes pushed the campaign list below the fold on desktop and three screens down on a phone.
- The boxes are a history, not the current state. When an account has been fixed, its "STOPPED" box is still there.
- On the progress page the same problem shows up to **three times**: the pause reason (amber), a red "High bounce rate" line and again under "Recent alerts". It is hard to tell what is still wrong.
- Messages say "Fix it in Settings" without a link, so the user has to search the long Settings page for the right account.

### 3. Settings is one long page that mixes daily work with rare setup (high)
Accounts (used weekly: new App Passwords, sender names, re-enabling) share one 2,000 px page with the sending window, warm-up caps, repeat-email rule, test address, do-not-email list and Google Sheets. There are **seven separate Save buttons**. Changing the window and the caps together needs two saves, and anything not saved is silently lost. On a phone the account cards break: the email wraps mid-word, and the sender-name field shrinks to about 40 px ("Ann").

### 4. The progress page doesn't answer "is it working, and do I need to do anything?" (high)
- The status is a small pill in the corner. "Paused" looks almost the same as "Running".
- The reason for a pause and the Resume button are in different places.
- "Inside the sending window — sending now." is small grey text under the bar.
- **Stop** (permanent) sits right next to **Pause** with the same size and weight.
- On a phone the per-account table scrolls sideways, and each account's **state** (the important column) is off-screen.
- The notes section's most prominent button is "Choose the original file to save notes into…", a rarely needed step shown as the main action.

### 5. Wizard steps miss feedback where it matters (medium)
- **Columns:** the title-row picker is a separate form that reloads on change, and choices already made in the second form are lost. Optional fields (phone, first/last name) look as important as the two required ones, and an unused field shows as "None", which reads like an error.
- **Message:** there is no preview while typing. The user only sees how `{first_name}` comes out on the next page. Leaving the page (e.g. clicking step 2) silently throws away what was typed.
- **Check & start:** the big **Start** button is above the email preview, so it is easy to start without reading the preview. Problems that block starting say "Add one in Settings" without a link. Three full previews make the page very long.

### 6. Error page shows Python errors and offers no way out (medium)
If a draft's file can't be read, the page shows `[Errno 2] No such file or directory: 'C:\\Users\\…\\data\\…'`. That leaks a server path, and on a phone the long path makes the page scroll sideways. The only button is "Back to campaigns". The broken draft can't be deleted, because "Delete draft" is on the page that fails.

### 7. Navigation and wording (medium)
- The main nav has "New campaign", which duplicates the button on the home page. Pages inside the wizard highlight "Campaigns", step 1 highlights "New campaign".
- Developer words leak into the UI: "SMTP", "IMAP", "README part 6/7", "Download log (CSV)", raw status names like "done".
- The dry-run banner is three lines tall on a phone, on every page.
- Account state "Blocked" (no sender name) sounds like Google blocked the account.

### 8. Phone layout (medium)
Data tables (campaigns, accounts, per-account stats) rely on sideways scrolling, so on a 390 px screen the right-hand columns (state, replies, bounces) are hidden. Small buttons (`button.small`) are about 26 px high, below a comfortable 40 px tap target.

### 9. Accessibility details (low–medium)
- Zero values use `#a3aaa5` on white: **2.4:1 contrast** (fails WCAG AA 4.5:1).
- When the progress page polls every 5 s, the whole bar area is an `aria-live` region.
- The campaign status isn't announced when it changes.
- The focus style is good and is kept.

---

## Redesign plan

### Structure and navigation
| Before | After | Why |
|---|---|---|
| Nav: Campaigns · New campaign · Settings | Nav: **Campaigns · Gmail accounts · Settings** | Accounts are the thing people fix most often; give them a page of their own. "New campaign" lives on the Campaigns page. |
| `/settings` = everything | `/accounts` = Gmail accounts; `/settings` = sending rules, test address, do-not-email list, Google Sheets | Separates daily account work from rare setup (problem 3). Old `/settings#accounts` links forward to `/accounts`. |
| Window, caps and repeat days: 3 forms, 3 Save buttons | One **Sending rules** form with one Save | Nothing is lost by saving the wrong form. Validation is the same. |

### Home (Campaigns)
- **First-run checklist** (problem 1) until at least one account can send: ① add a Gmail account with sender name and App Password, ② (optional) set your test address, ③ (optional) Google Sheets. "New campaign" still works, but the checklist makes the order clear.
- **"Needs your attention"** (problem 2) is built from the **current state**, not the log: stopped accounts, accounts without a sender name, campaigns paused automatically. Each item has one button that goes straight to the fix ("Fix account", "Review and resume"). Fixed things disappear on their own.
- The event history moves into a collapsed **Recent activity** list with one **Clear all**.
- Campaigns show as rows with a progress bar and a plain status ("Sending", "Paused – needs you", "Finished"). On a phone they become cards without sideways scrolling.

### Progress page
- A **status banner** at the top says, in one sentence, what is happening and what to do. It has the main action inline: *Sending now – next email in 3 min*, *Waiting for the sending window – starts Mon 9:00 AM CT*, *Paused automatically: 5% bounced … [Resume]*, *Finished*.
- **Stop** moves away from Pause/Resume, gets a clear label ("Stop campaign for good…") and keeps its confirmation.
- Account problems show as one line with a link to the account. They are not repeated as separate red boxes.
- The per-account table becomes stacked cards on a phone.
- Notes: a plain sentence ("38 of 40 notes written to your file") with the actions in order of usefulness.
- History is a collapsed **Activity** list.

### Wizard
- **Step 1:** warns up front if no account can send yet, with a link.
- **Step 2:** title row and tab live in the same form, so the column choices are kept. The mapping is split into **Needed** (Email, Name) and **Optional**. "None" becomes "Not in this list".
- **Step 3:** **live preview** of the first driver's email next to the editor (rendered by the server, same code as the real email). The browser warns before leaving with unsaved text.
- **Step 4** is reordered top to bottom as a checklist:
  1. who gets it
  2. preview (one driver at a time, with "Show another driver")
  3. send a test
  4. **Start**, last, with a one-line recap of what will happen.

  Blockers link to the exact fix.

### Errors
- Friendly message.
- Technical detail hidden under "Details".
- For a broken draft, a **Delete this draft** button on the error page.
- Long text wraps.

### Visual direction
Keep the highway-sign identity: guide-sign green header, Interstate-blue actions, warning yellow for dry run, Overpass font (self-hosted). It is distinctive, it suits a trucking company, and the staff already know it. The changes are about hierarchy, not decoration:
- one page-header pattern (title, one-line explanation, main action on the right)
- one status-banner component
- one "attention item" component
- fewer boxes and borders
- 40 px tap targets on phones
- tables that turn into cards below 640 px
- AA contrast for all text

### Wording
Plain recruiting language, no SMTP/IMAP/README references in the UI:
- "Download report (CSV)"
- "Needs sender name" instead of "Blocked"
- "Finished" instead of "done"
- a shorter dry-run banner

---

## Not changed (and why)
- **Sending logic and safety rules.** No double sends, suppression check, caps, window, random gaps, auto-pause thresholds, how accounts are stopped, and NOTES handling are untouched. Only the screens that show them changed. The combined "Sending rules" form calls the same validation as the three old forms, and the old endpoints still work.
- **Email content and format.** Unchanged.
- **Auth, CSRF and secrets.** Unchanged. All new POST routes go through the same login and Origin check.
- **Unsubscribe page logic.** Unchanged (Confirm button, one-click POST). Only the styling was aligned.
- **Native `confirm()` dialogs for Start/Stop/Delete/Remove.** Kept. They are accessible, work on every phone, and need no extra code.
- **Sideways-scrolling column preview on step 2.** Kept. It shows the spreadsheet as a spreadsheet, and stacking it would hide the column letters people match against Excel.
