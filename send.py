#!/usr/bin/env python3
"""Alumni email sender.

Sends one personalized plain-text email per alumnus over a single reused
Gmail SMTP connection. All state lives in append-only text files, so the
script is fully resumable and can never double-send:

    sent.txt     one address per line, appended+fsynced before the next sleep
    bounced.txt  email  |  timestamp  |  reason   (SMTP 5xx or IMAP-detected)
    skipped.txt  email  |  timestamp  |  reason   (bad/missing CSV data)
    send.log     timestamp | STATUS | email | smtp response

Usage:
    python3 send.py --dry-run
    python3 send.py --limit 5
    python3 send.py --limit 200
"""

import argparse
import csv
import os
import random
import re
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, formataddr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE_DIR, ".env")
CSV_PATH = os.path.join(BASE_DIR, "alumni.csv")
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.txt")
SENT_PATH = os.path.join(BASE_DIR, "sent.txt")
BOUNCED_PATH = os.path.join(BASE_DIR, "bounced.txt")
SKIPPED_PATH = os.path.join(BASE_DIR, "skipped.txt")
LOG_PATH = os.path.join(BASE_DIR, "send.log")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 60

SLEEP_MIN, SLEEP_MAX = 45, 90            # seconds between sends
TRANSIENT_PAUSE_MIN, TRANSIENT_PAUSE_MAX = 15 * 60, 30 * 60
CIRCUIT_BREAKER = 4                      # consecutive failures -> abort
DAILY_CAP = 450                          # stay well under Gmail's ~500/day

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def one_line(text):
    """Collapse an SMTP response into something safe for a log line."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " ".join(str(text).split())


def append_line(path, line):
    """Append one line and force it to disk before we do anything else."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def log(status, email, response=""):
    append_line(LOG_PATH, "%s | %s | %s | %s" % (now_iso(), status, email, one_line(response)))


def say(msg):
    print(msg, flush=True)


def die(msg, code=1):
    print("\nERROR: %s\n" % msg, file=sys.stderr, flush=True)
    sys.exit(code)


def load_emails(path):
    """Load a state file into a set of lowercased addresses.

    Works for sent.txt (bare address per line) and for the pipe-delimited
    bounced.txt / skipped.txt, where the address is the first field.
    """
    found = set()
    if not os.path.exists(path):
        return found
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            addr = line.split("|")[0].strip().lower()
            if addr:
                found.add(addr)
    return found


def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key] = value
    return env


def load_template(path):
    """template.txt = 'Subject: ...', a blank line, then the body."""
    if not os.path.exists(path):
        die("%s not found." % path)
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    first, _, rest = raw.partition("\n")
    if not first.lower().startswith("subject:"):
        die("template.txt must start with a 'Subject: ...' line.")
    subject = first.split(":", 1)[1].strip()
    body = rest.lstrip("\n")
    if not subject:
        die("template.txt has an empty subject line.")
    if not body.strip():
        die("template.txt has an empty body.")
    if not body.endswith("\n"):
        body += "\n"
    return subject, body


def sent_today():
    """Count today's successful sends from send.log, to honour the daily cap."""
    if not os.path.exists(LOG_PATH):
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[0].startswith(today) and parts[1] == "SENT":
                count += 1
    return count


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

def build_queue(done, already_skipped, dry_run):
    """Rows from alumni.csv that are not already sent/bounced/skipped.

    Deduped case-insensitively on email. Rows with missing last_name or an
    invalid-looking email are recorded in skipped.txt and never sent.
    """
    if not os.path.exists(CSV_PATH):
        die("%s not found." % CSV_PATH)

    queue = []
    seen = set()
    new_skips = []

    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            die("alumni.csv is empty (no header row).")
        missing = {"first_name", "last_name", "email"} - {
            (name or "").strip() for name in reader.fieldnames
        }
        if missing:
            die("alumni.csv is missing column(s): %s" % ", ".join(sorted(missing)))

        for row_no, row in enumerate(reader, start=2):  # row 1 is the header
            email = (row.get("email") or "").strip()
            first_name = (row.get("first_name") or "").strip()
            last_name = (row.get("last_name") or "").strip()
            key = email.lower()

            if not email:
                new_skips.append(("(csv row %d)" % row_no, "missing email"))
                continue
            if not EMAIL_RE.match(email):
                new_skips.append((key, "invalid email"))
                continue
            if not last_name:
                new_skips.append((key, "missing last_name"))
                continue
            if key in seen:
                continue                      # duplicate inside the CSV
            seen.add(key)
            if key in done:
                continue                      # already sent / bounced / skipped

            queue.append({
                "email": email,
                "key": key,
                "first_name": first_name,
                "last_name": last_name,
            })

    # Record skips once; never re-log an address already in skipped.txt.
    recorded = 0
    for key, reason in new_skips:
        if key in already_skipped:
            continue
        already_skipped.add(key)
        recorded += 1
        if dry_run:
            say("  would skip %-40s %s" % (key, reason))
        else:
            append_line(SKIPPED_PATH, "%s  |  %s  |  %s" % (key, now_iso(), reason))
            log("SKIPPED", key, reason)

    return queue, recorded


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render(subject_tpl, body_tpl, recipient, config):
    fields = {
        "first_name": recipient["first_name"],
        "last_name": recipient["last_name"],
        "email": recipient["email"],
        "your_name": config["your_name"],
        "chapter_name": config["chapter_name"],
    }
    try:
        return subject_tpl.format(**fields), body_tpl.format(**fields)
    except KeyError as exc:
        die("template.txt uses an unknown placeholder {%s}. "
            "Available: %s" % (exc.args[0], ", ".join(sorted(fields))))
    except (IndexError, ValueError) as exc:
        die("template.txt has a malformed placeholder (%s). "
            "Literal braces must be doubled: {{ }}" % exc)


def build_message(subject, body, recipient, config):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config["your_name"], config["gmail_address"]))
    msg["To"] = recipient["email"]
    msg["Reply-To"] = config["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    # Plain ASCII goes out as clean 7bit; anything else falls back to utf-8.
    try:
        body.encode("ascii")
        msg.set_content(body, cte="7bit")
    except UnicodeEncodeError:
        msg.set_content(body, charset="utf-8")
    return msg


# --------------------------------------------------------------------------
# SMTP
# --------------------------------------------------------------------------

def connect(config):
    smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
    smtp.ehlo()
    smtp.starttls(context=ssl.create_default_context())
    smtp.ehlo()
    smtp.login(config["gmail_address"], config["password"])
    return smtp


def reconnect(config, attempts=3):
    """Re-establish a dropped connection. Returns the new SMTP object or None."""
    for attempt in range(1, attempts + 1):
        wait = 20 * attempt
        say("  reconnecting to SMTP (attempt %d/%d) in %ds..." % (attempt, attempts, wait))
        time.sleep(wait)
        try:
            smtp = connect(config)
            say("  reconnected.")
            log("RECONNECT", "-", "ok on attempt %d" % attempt)
            return smtp
        except smtplib.SMTPAuthenticationError as exc:
            die("SMTP authentication failed on reconnect: %s\n"
                "Check GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env." % one_line(exc))
        except (smtplib.SMTPException, socket.error, OSError, ssl.SSLError) as exc:
            log("RECONNECT_FAIL", "-", exc)
    return None


def send_one(smtp, msg, address):
    """Attempt one send.

    Returns (status, response) where status is:
        'sent'          delivered to Gmail for sending
        'permanent'     5xx, undeliverable -> bounced.txt
        'transient'     4xx, stays in the queue -> pause and retry
        'disconnected'  connection died -> reconnect and retry
    """
    try:
        smtp.send_message(msg)
        return "sent", "250 accepted"
    except smtplib.SMTPRecipientsRefused as exc:
        code, text = exc.recipients.get(address, (550, b"recipient refused"))
        return ("permanent" if code >= 500 else "transient"), "%s %s" % (code, one_line(text))
    except smtplib.SMTPSenderRefused as exc:
        return ("permanent" if exc.smtp_code >= 500 else "transient"), \
               "%s %s" % (exc.smtp_code, one_line(exc.smtp_error))
    except smtplib.SMTPResponseException as exc:      # covers SMTPDataError etc.
        return ("permanent" if exc.smtp_code >= 500 else "transient"), \
               "%s %s" % (exc.smtp_code, one_line(exc.smtp_error))
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            socket.error, ssl.SSLError, OSError) as exc:
        return "disconnected", one_line(exc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Send the alumni invitation email.")
    parser.add_argument("--limit", type=int, default=None,
                        help="maximum number of emails to send this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="render every email and print it; never connect to SMTP")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def load_config(dry_run):
    env = load_env(ENV_PATH)

    def get(key):
        return (env.get(key) or os.environ.get(key) or "").strip()

    config = {
        "gmail_address": get("GMAIL_ADDRESS"),
        "password": get("GMAIL_APP_PASSWORD").replace(" ", ""),
        "your_name": get("YOUR_NAME"),
        "chapter_name": get("CHAPTER_NAME"),
    }
    config["reply_to"] = get("REPLY_TO") or config["gmail_address"]

    env_names = {
        "gmail_address": "GMAIL_ADDRESS",
        "password": "GMAIL_APP_PASSWORD",
        "your_name": "YOUR_NAME",
        "chapter_name": "CHAPTER_NAME",
    }
    placeholders = {
        "gmail_address": "unset-gmail-address@example.invalid",
        "password": "unset",
        "your_name": "UNSET_YOUR_NAME",
        "chapter_name": "UNSET_CHAPTER_NAME",
    }
    missing = [k for k in env_names if not config[k]]
    if missing:
        names = ", ".join(env_names[k] for k in missing)
        if not dry_run:
            die("missing in .env: %s" % names)
        say("WARNING: missing in .env: %s — using placeholders for this dry run." % names)
        for key in missing:
            config[key] = placeholders[key]
        config["reply_to"] = config["reply_to"] or config["gmail_address"]
    return config


def do_dry_run(queue, subject_tpl, body_tpl, config, limit):
    batch = queue[:limit] if limit else queue
    for i, recipient in enumerate(batch, start=1):
        subject, body = render(subject_tpl, body_tpl, recipient, config)
        message = build_message(subject, body, recipient, config)
        say("\n" + "=" * 72)
        say("[%d/%d] %s" % (i, len(batch), recipient["email"]))
        say("=" * 72)
        say(message.as_string())
    say("\n" + "-" * 72)
    say("DRY RUN: %d email(s) rendered, nothing sent, no state files written." % len(batch))
    if limit and len(queue) > limit:
        say("%d more in the queue beyond --limit %d." % (len(queue) - limit, limit))


def countdown(seconds=5):
    say("")
    for remaining in range(seconds, 0, -1):
        print("  starting live send in %d... (ctrl-c to abort)   \r" % remaining,
              end="", flush=True)
        time.sleep(1)
    print(" " * 60 + "\r", end="", flush=True)


def run(args):
    config = load_config(args.dry_run)
    subject_tpl, body_tpl = load_template(TEMPLATE_PATH)

    sent = load_emails(SENT_PATH)
    bounced = load_emails(BOUNCED_PATH)
    skipped = load_emails(SKIPPED_PATH)
    done = sent | bounced | skipped

    queue, new_skips = build_queue(done, skipped, args.dry_run)

    say("")
    say("  already sent .... %d" % len(sent))
    say("  bounced ......... %d" % len(bounced))
    say("  skipped ......... %d (%d new)" % (len(skipped), new_skips))
    say("  queue ........... %d" % len(queue))

    if not queue:
        say("\nNothing left to send.")
        return 0

    limit = args.limit

    if args.dry_run:
        do_dry_run(queue, subject_tpl, body_tpl, config, limit)
        return 0

    # Daily cap: Gmail free accounts die around 500 recipients/day.
    today = sent_today()
    headroom = DAILY_CAP - today
    if headroom <= 0:
        say("\nDaily cap reached: %d sent today (cap %d). Try again tomorrow."
            % (today, DAILY_CAP))
        return 0
    if limit is None or limit > headroom:
        if limit is not None:
            say("\n  --limit %d trimmed to %d (%d already sent today, cap %d)."
                % (limit, headroom, today, DAILY_CAP))
        limit = headroom

    planned = min(limit, len(queue))
    say("\n  sending ......... %d this run (%d already sent today)" % (planned, today))
    say("  from ............ %s" % config["gmail_address"])
    say("  pace ............ %d-%ds between sends (~%d-%d min total)"
        % (SLEEP_MIN, SLEEP_MAX,
           planned * SLEEP_MIN // 60, planned * SLEEP_MAX // 60))
    countdown()

    try:
        smtp = connect(config)
    except smtplib.SMTPAuthenticationError as exc:
        die("SMTP authentication failed: %s\n"
            "Check GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env "
            "(it must be a 16-character App Password, not your login password)."
            % one_line(exc))
    except (smtplib.SMTPException, socket.error, OSError, ssl.SSLError) as exc:
        die("could not connect to %s:%d — %s" % (SMTP_HOST, SMTP_PORT, one_line(exc)))
    say("  connected to %s\n" % SMTP_HOST)

    index = 0
    sent_count = 0
    bounced_count = 0
    consecutive_failures = 0

    try:
        while index < len(queue) and sent_count < limit:
            recipient = queue[index]
            address = recipient["email"]
            subject, body = render(subject_tpl, body_tpl, recipient, config)
            message = build_message(subject, body, recipient, config)

            status, response = send_one(smtp, message, address)

            if status == "sent":
                # Persist BEFORE sleeping or advancing: a kill here is safe.
                append_line(SENT_PATH, recipient["key"])
                log("SENT", address, response)
                sent_count += 1
                consecutive_failures = 0
                index += 1
                say("  [%d/%d] sent -> %s" % (sent_count, planned, address))

                if index < len(queue) and sent_count < limit:
                    nap = random.uniform(SLEEP_MIN, SLEEP_MAX)
                    say("        sleeping %.0fs" % nap)
                    time.sleep(nap)
                continue

            if status == "permanent":
                append_line(BOUNCED_PATH,
                            "%s  |  %s  |  %s" % (recipient["key"], now_iso(), response))
                log("BOUNCED", address, response)
                bounced_count += 1
                consecutive_failures += 1
                index += 1
                say("  bounced (5xx) -> %s : %s" % (address, response))

            elif status == "transient":
                # Deliberately not recorded anywhere: it stays in the queue.
                log("DEFERRED", address, response)
                consecutive_failures += 1
                say("  deferred (4xx) -> %s : %s" % (address, response))
                if consecutive_failures < CIRCUIT_BREAKER:
                    pause = random.uniform(TRANSIENT_PAUSE_MIN, TRANSIENT_PAUSE_MAX)
                    say("        pausing %.0f min, then retrying the same address"
                        % (pause / 60))
                    time.sleep(pause)

            else:  # disconnected
                log("DISCONNECTED", address, response)
                consecutive_failures += 1
                say("  connection lost -> %s : %s" % (address, response))
                try:
                    smtp.close()
                except Exception:
                    pass
                if consecutive_failures < CIRCUIT_BREAKER:
                    smtp = reconnect(config)
                    if smtp is None:
                        consecutive_failures = CIRCUIT_BREAKER

            if consecutive_failures >= CIRCUIT_BREAKER:
                log("CIRCUIT_BREAKER", address, "%d consecutive failures" % consecutive_failures)
                say("")
                say("!" * 72)
                say("!! CIRCUIT BREAKER: %d consecutive failures. Aborting the run." % consecutive_failures)
                say("!! Something is wrong at the account level — continuing would")
                say("!! damage sender reputation. Check send.log, verify the account")
                say("!! in the Gmail web UI, and wait before trying again.")
                say("!" * 72)
                summary(sent_count, bounced_count, queue, index, limit)
                return 2

    except KeyboardInterrupt:
        say("\n\nInterrupted. State is safe — rerun to resume exactly where this stopped.")
        log("INTERRUPTED", "-", "%d sent this run" % sent_count)
        summary(sent_count, bounced_count, queue, index, limit)
        return 130
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    summary(sent_count, bounced_count, queue, index, limit)
    return 0


def summary(sent_count, bounced_count, queue, index, limit):
    remaining = len(queue) - index
    say("")
    say("  sent this run ... %d" % sent_count)
    say("  bounced ......... %d" % bounced_count)
    say("  still queued .... %d" % remaining)
    say("  sent today ...... %d / %d" % (sent_today(), DAILY_CAP))
    if remaining:
        say("\n  Rerun to continue. Run bounce_check.py between send days.")


if __name__ == "__main__":
    sys.exit(run(parse_args()))
