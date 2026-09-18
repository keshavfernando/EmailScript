# Alumni Email Sender

One-time bulk email project: send a personalized invitation to ~500 fraternity alumni from our chapter Gmail account, inviting them to reconnect and attend an event. Priorities: deliverability (stay out of spam), respect Gmail rate limits, never double-send.

## Stack

- Python 3, standard library only where possible: `smtplib`, `email.message`, `csv`, `time`, `imaplib`
- Sending via SMTP: `smtp.gmail.com:587` with STARTTLS
- Auth: Gmail App Password loaded from `.env` (never hardcoded, never committed)
- No Gmail API / OAuth — out of scope for a one-time project

## Files

- `send.py` — main sender loop
- `bounce_check.py` — post-run IMAP scan of the inbox for bounce messages; flags bounced addresses in the DB
- `template.txt` — the email. Line 1 is `Subject: ...`, then a blank line, then the body. Already exists in the repo — read it, do not rewrite it.
- `alumni.csv` — recipient list with columns: `first_name`, `last_name`, `email`. Gitignored.
- `sent.txt` — one email address per line, appended immediately after each successful send. This is the no-double-send state. Gitignored.
- `bounced.txt` — one line per bounced/undeliverable address: `email  |  timestamp  |  reason` (SMTP rejection or IMAP-detected bounce). Gitignored.
- `skipped.txt` — rows skipped for missing/invalid data. Gitignored.
- `.env` — `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `YOUR_NAME`, `CHAPTER_NAME`. Gitignored.

## Template rendering

- Placeholders in `template.txt` use `{curly_brace}` style: `{last_name}` fills per-recipient from the CSV; `{your_name}` and `{chapter_name}` fill from `.env` constants.
- Render subject and body per recipient with Python `str.format` (no Jinja2 dependency needed).
- Skip any row with a missing/blank `last_name` or an invalid-looking email and log it to `skipped.txt` — never send "Hi Brother ," to anyone.

## Text-file state (the core of the design)

No database. State lives in plain text files:

- On startup, load `sent.txt`, `bounced.txt`, and `skipped.txt` into sets (lowercased emails).
- The send queue = rows from `alumni.csv` (deduped, case-insensitive on email) whose email is in none of those sets → the script is fully resumable and can NEVER double-send, even if killed mid-run.
- Append to `sent.txt` and flush/`fsync` immediately after each successful send, BEFORE sleeping or moving to the next recipient. Append-only, never rewrite these files.
- SMTP 5xx rejection at send time → append to `bounced.txt` with timestamp and the SMTP response text.

## Sending behavior (deliverability + rate limits)

- One recipient per message, `To:` only. Never BCC a list.
- Sleep 45–90 seconds (randomized within that range) between sends — no fixed metronome interval.
- `--limit N` flag caps sends per invocation. Planned schedule: Day 1 = 50, Day 2 = 200, Day 3 = remainder. Free Gmail caps ~500 recipients/day; never exceed ~450 in a day.
- `--dry-run` flag renders every email and prints to stdout without connecting to SMTP.
- Reuse one SMTP connection; reconnect (with fresh login) if it drops mid-run.
- Set a real `Reply-To`. Plain text body (`text/plain`), no HTML, no attachments, no link shorteners.

## Error handling

- SMTP 4xx (e.g. 421, 452 — transient/rate limiting): do NOT log the address anywhere (it stays in the queue), pause 15–30 minutes, then resume automatically. Never hammer retries.
- SMTP 5xx (e.g. 550, 553 — permanent/undeliverable): append to `bounced.txt` with the response text, continue immediately.
- Circuit breaker: 4 consecutive failures of any kind → abort the run with a loud message. Something is wrong at the account level; continuing damages sender reputation.
- Log every attempt (timestamp, email, status, SMTP response) to `send.log` in addition to the DB.

## bounce_check.py

- Connect via `imaplib` (SSL, `imap.gmail.com`) using the same `.env` credentials.
- Scan recent inbox messages for bounce notifications (from `mailer-daemon`/`postmaster`, or subjects containing "Delivery Status Notification", "Undeliverable", "Returned mail").
- Extract the failed recipient address from each bounce and append it to `bounced.txt` (skip addresses already in the file — no duplicates).
- Print a summary: total sent (line count of `sent.txt`) / total bounced / bounce rate. Run this between send days; if bounce rate > 8%, stop and clean the list before continuing.

## Non-negotiables

- `.gitignore` must cover `alumni.csv`, `sent.txt`, `bounced.txt`, `skipped.txt`, `.env`, `send.log`, `__pycache__/` — personal data and credentials never reach GitHub.
- No feature creep: no HTML emails, no threading/async, no third-party deps. Simple, readable, resumable.
- Test path: `--dry-run` first, then a real run with `--limit 5` to test inboxes only, then the ramp schedule.
