#!/usr/bin/env python3
"""Post-run bounce scanner.

Connects to Gmail over IMAP (read-only), looks through recent inbox messages
for bounce notifications, extracts the address that failed, and appends it to
bounced.txt so send.py will never retry it.

Run this between send days. If the bounce rate goes above 8%, stop and clean
the list before sending any more.

Usage:
    python3 bounce_check.py
    python3 bounce_check.py --days 30
    python3 bounce_check.py --dry-run
    python3 bounce_check.py --mailbox "[Gmail]/Spam"
"""

import argparse
import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE_DIR, ".env")
SENT_PATH = os.path.join(BASE_DIR, "sent.txt")
BOUNCED_PATH = os.path.join(BASE_DIR, "bounced.txt")
LOG_PATH = os.path.join(BASE_DIR, "send.log")

IMAP_HOST = "imap.gmail.com"

BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
BOUNCE_SUBJECTS = (
    "delivery status notification",
    "undeliverable",
    "returned mail",
)

ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
BOUNCE_RATE_LIMIT = 0.08


# --------------------------------------------------------------------------
# small helpers (kept local so this script stands alone)
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def one_line(text):
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " ".join(str(text).split())


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def say(msg):
    print(msg, flush=True)


def die(msg, code=1):
    print("\nERROR: %s\n" % msg, file=sys.stderr, flush=True)
    sys.exit(code)


def load_emails(path):
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


def header(msg, name):
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


# --------------------------------------------------------------------------
# bounce detection
# --------------------------------------------------------------------------

def is_bounce(msg):
    sender = (header(msg, "From") + " " + header(msg, "Return-Path")).lower()
    if any(name in sender for name in BOUNCE_SENDERS):
        return True
    subject = header(msg, "Subject").lower()
    if any(phrase in subject for phrase in BOUNCE_SUBJECTS):
        return True
    if msg.get_content_type() == "multipart/report":
        return True
    return False


def clean(addr):
    addr = (addr or "").strip().strip("<>").strip()
    if ";" in addr:                      # "rfc822; someone@example.com"
        addr = addr.split(";", 1)[1].strip()
    addr = addr.strip("<>").strip().lower()
    return addr if ADDRESS_RE.fullmatch(addr) else ""


def extract_failures(msg, sent, our_address):
    """Return {address: reason} for every recipient this bounce reports.

    Three sources, most reliable first:
      1. the X-Failed-Recipients header Gmail sets on its own bounces
      2. the message/delivery-status part (Final-/Original-Recipient)
      3. addresses in the attached original message or body text, but only
         if we actually sent to them
    """
    failures = {}
    subject = header(msg, "Subject") or "bounce"

    for raw in (header(msg, "X-Failed-Recipients") or "").split(","):
        addr = clean(raw)
        if addr:
            failures[addr] = subject

    for part in msg.walk():
        ctype = part.get_content_type()

        if ctype == "message/delivery-status":
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else []
            for block in blocks:
                if not hasattr(block, "get"):
                    continue
                addr = clean(block.get("Final-Recipient") or
                             block.get("Original-Recipient") or "")
                if not addr:
                    continue
                reason = one_line(block.get("Diagnostic-Code") or
                                  block.get("Status") or subject)
                failures[addr] = reason      # most specific wins

        elif ctype == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
            if inner is not None and hasattr(inner, "get"):
                addr = clean(inner.get("To") or "")
                if addr and addr in sent:
                    failures.setdefault(addr, subject)

    if not failures:
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            try:
                text = part.get_payload(decode=True).decode("utf-8", "replace")
            except Exception:
                continue
            for candidate in ADDRESS_RE.findall(text):
                addr = candidate.lower()
                if addr in sent:
                    failures.setdefault(addr, subject)

    failures.pop(our_address.lower(), None)
    return failures


# --------------------------------------------------------------------------
# IMAP
# --------------------------------------------------------------------------

def fetch_bounces(config, mailbox, days, sent):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
    except Exception as exc:
        die("could not connect to %s — %s" % (IMAP_HOST, one_line(exc)))

    try:
        imap.login(config["gmail_address"], config["password"])
    except imaplib.IMAP4.error as exc:
        die("IMAP login failed: %s\n"
            "Check GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env, and make sure IMAP "
            "is enabled in Gmail settings." % one_line(exc))

    found = {}
    try:
        # readonly: scanning must not mark anything as read
        status, _ = imap.select(mailbox, readonly=True)
        if status != "OK":
            die("could not open mailbox %r." % mailbox)

        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = imap.search(None, "(SINCE %s)" % since)
        if status != "OK":
            die("IMAP search failed.")

        ids = data[0].split()
        say("  scanning %d message(s) in %s since %s" % (len(ids), mailbox, since))

        for num, msg_id in enumerate(ids, start=1):
            if num % 50 == 0:
                say("    ...%d/%d" % (num, len(ids)))
            status, payload = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            try:
                msg = email.message_from_bytes(payload[0][1])
            except Exception:
                continue
            if not is_bounce(msg):
                continue
            for addr, reason in extract_failures(msg, sent, config["gmail_address"]).items():
                found.setdefault(addr, reason)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass

    return found


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Scan the inbox for bounced alumni emails.")
    parser.add_argument("--days", type=int, default=14,
                        help="how many days back to scan (default: 14)")
    parser.add_argument("--mailbox", default="INBOX",
                        help="mailbox to scan (default: INBOX)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be recorded without writing bounced.txt")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    return args


def main():
    args = parse_args()

    env = load_env(ENV_PATH)

    def get(key):
        return (env.get(key) or os.environ.get(key) or "").strip()

    config = {
        "gmail_address": get("GMAIL_ADDRESS"),
        "password": get("GMAIL_APP_PASSWORD").replace(" ", ""),
    }
    missing = [k.upper() for k, v in config.items() if not v]
    if missing:
        die("missing in .env: %s" % ", ".join(missing))

    sent = load_emails(SENT_PATH)
    bounced = load_emails(BOUNCED_PATH)
    if not sent:
        say("\nNote: sent.txt is empty — nothing has been sent yet.")

    say("")
    found = fetch_bounces(config, args.mailbox, args.days, sent)

    new = {addr: reason for addr, reason in found.items() if addr not in bounced}

    say("")
    say("  bounce messages matched ... %d address(es)" % len(found))
    say("  already in bounced.txt .... %d" % (len(found) - len(new)))
    say("  new ....................... %d" % len(new))

    for addr, reason in sorted(new.items()):
        short = reason if len(reason) <= 60 else reason[:57] + "..."
        if args.dry_run:
            say("    would record %-40s %s" % (addr, short))
        else:
            append_line(BOUNCED_PATH, "%s  |  %s  |  %s" % (addr, now_iso(), one_line(reason)))
            append_line(LOG_PATH, "%s | BOUNCED_IMAP | %s | %s"
                                  % (now_iso(), addr, one_line(reason)))
            say("    recorded %-40s %s" % (addr, short))
        bounced.add(addr)

    if args.dry_run and new:
        say("\n  DRY RUN: bounced.txt was not modified.")

    total_sent = len(sent)
    total_bounced = len(bounced)
    rate = (total_bounced / total_sent) if total_sent else 0.0

    say("")
    say("  " + "-" * 40)
    say("  total sent ...... %d" % total_sent)
    say("  total bounced ... %d" % total_bounced)
    say("  bounce rate ..... %.1f%%" % (rate * 100))
    say("  " + "-" * 40)

    if total_sent and rate > BOUNCE_RATE_LIMIT:
        say("")
        say("!" * 72)
        say("!! Bounce rate is %.1f%% — above the %.0f%% limit." % (rate * 100, BOUNCE_RATE_LIMIT * 100))
        say("!! STOP. Do not send the next batch. Clean the list first:")
        say("!! a high bounce rate gets the account flagged as a spammer.")
        say("!" * 72)
        return 1

    if total_sent:
        say("\n  Bounce rate is healthy. Safe to continue with the next batch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
