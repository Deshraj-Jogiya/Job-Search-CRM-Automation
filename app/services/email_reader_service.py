"""
Read-only access to the job-search mailbox over IMAP (email_sync_service.py decides what to do with what is read).

Gmail with an app password (IMAP_HOST / IMAP_USER / IMAP_PASSWORD in .env). The mailbox is opened with EXAMINE, never
SELECT, and every fetch uses BODY.PEEK, so reading never marks a message read, moves it or deletes it; nothing here
issues STORE, COPY, MOVE or EXPUNGE. A test runs a fake server and fails on any command that could change the mailbox.

Two passes keep what is downloaded small: headers for every new message first, then the body only for messages the
caller's `wants` filter keeps (a known ATS sender, a tracked company's domain, someone already contacted, or a subject
that looks like a job email). Nothing is stored here.
"""

from __future__ import annotations

import email
import html
import imaplib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

# Gmail's "All Mail" holds archived mail too (an archived rejection still counts); INBOX is the fallback elsewhere.
_MAILBOXES = ('"[Gmail]/All Mail"', "INBOX")
_READ_ONLY_COMMANDS = {"LOGIN", "LIST", "EXAMINE", "UID", "LOGOUT", "CAPABILITY", "NOOP"}
MAX_TEXT_CHARS = 6000  # enough for any real status email; long newsletters are cut, not parsed whole


class EmailReaderError(Exception):
    pass


@dataclass
class RawEmail:
    uid: int
    message_id: str
    sender_name: str
    sender_email: str
    subject: str
    received_at: datetime  # naive UTC, like every other timestamp in this app
    to_addresses: list[str] = field(default_factory=list)
    thread_id: str | None = None  # Gmail X-GM-THRID in hex, for a link straight to the thread
    text: str = ""

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rsplit("@", 1)[-1].lower() if "@" in self.sender_email else ""


def is_configured() -> bool:
    return bool(os.getenv("IMAP_HOST") and os.getenv("IMAP_USER") and os.getenv("IMAP_PASSWORD"))


def mailbox_owner() -> str:
    return (os.getenv("IMAP_USER") or "").strip().lower()


def gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(style|script|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t ͏‌​﻿]+")


def html_to_text(markup: str) -> str:
    text = _STYLE_RE.sub(" ", markup)
    text = _BREAK_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub(" ", text))
    return clean_text(text)


def clean_text(text: str) -> str:
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.replace("\r", "").split("\n")]
    out = "\n".join(line for line in lines if line)
    return out[:MAX_TEXT_CHARS]


def body_text(msg: email.message.Message) -> str:
    """Plain text of a message: the text/plain part when there is one, else the HTML part stripped of tags."""
    plain, markup = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = decoded
        elif ctype == "text/html" and markup is None:
            markup = decoded
    if plain and plain.strip():
        return clean_text(plain)
    return html_to_text(markup) if markup else ""


def parse_headers(uid: int, raw_header: bytes, thread_id: str | None = None) -> RawEmail:
    msg = email.message_from_bytes(raw_header)
    name, addr = (getaddresses([msg.get("From", "")]) or [("", "")])[0]
    try:
        received = parsedate_to_datetime(msg.get("Date"))
        if received.tzinfo:
            received = received.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        received = datetime.utcnow()
    return RawEmail(
        uid=uid,
        message_id=(msg.get("Message-ID") or f"<uid-{uid}@local>").strip(),
        sender_name=_decode(name),
        sender_email=(addr or "").strip().lower(),
        subject=_decode(msg.get("Subject")),
        received_at=received,
        to_addresses=[a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a],
        thread_id=thread_id,
    )


_FETCH_META_RE = re.compile(rb"UID (\d+)")
_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")


class _ReadOnlyIMAP:
    """Thin wrapper that refuses anything but read commands, so a later edit can't quietly start changing mail."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        if name.upper() not in _READ_ONLY_COMMANDS:
            raise EmailReaderError(f"IMAP command {name} is not allowed: the reader is read-only.")
        return getattr(self._conn, name)

    def examine(self, mailbox: str):
        return self._conn.select(mailbox, readonly=True)  # imaplib's readonly select sends EXAMINE

    def uid(self, command: str, *args):
        if command.upper() not in ("SEARCH", "FETCH"):
            raise EmailReaderError(f"IMAP UID {command} is not allowed: the reader is read-only.")
        if command.upper() == "FETCH" and args and b"BODY[" in str(args[-1]).encode() and b"PEEK" not in str(args[-1]).encode():
            raise EmailReaderError("Fetching a body without BODY.PEEK would mark the message read.")
        return self._conn.uid(command, *args)


def _connect(connect=None):
    if not is_configured():
        raise EmailReaderError("Email reading isn't set up: IMAP_HOST, IMAP_USER and IMAP_PASSWORD are needed in .env.")
    try:
        conn = connect() if connect else imaplib.IMAP4_SSL(os.getenv("IMAP_HOST"), 993, timeout=60)
        conn.login(os.getenv("IMAP_USER"), os.getenv("IMAP_PASSWORD"))
    except (imaplib.IMAP4.error, OSError) as e:
        raise EmailReaderError(f"Couldn't sign in to the mailbox: {e}") from e
    return _ReadOnlyIMAP(conn)


def _open_mailbox(imap) -> tuple[str, int]:
    for box in _MAILBOXES:
        status, data = imap.examine(box)
        if status == "OK":
            status, validity = imap._conn.response("UIDVALIDITY")
            try:
                return box, int(validity[0]) if validity and validity[0] else 0
            except (TypeError, ValueError):
                return box, 0
    raise EmailReaderError("Couldn't open the mailbox read-only.")


def fetch_new(since_uid: int | None, since_date: date, uidvalidity: int | None, wants, connect=None,
              limit: int = 2000) -> tuple[list[RawEmail], int, int]:
    """Messages newer than `since_uid` (or received on/after `since_date` on the first run, or after the server
    renumbered the mailbox). `wants(raw_email) -> bool` decides, from headers alone, which bodies are downloaded.
    Returns (emails with bodies, highest uid seen, uidvalidity)."""
    imap = _connect(connect)
    try:
        box, validity = _open_mailbox(imap)
        if since_uid and uidvalidity and validity and validity == uidvalidity:
            status, data = imap.uid("SEARCH", None, f"UID {since_uid + 1}:*")
        else:
            status, data = imap.uid("SEARCH", None, f'SINCE {since_date.strftime("%d-%b-%Y")}')
        if status != "OK":
            raise EmailReaderError("Mailbox search failed.")
        uids = [int(u) for u in (data[0] or b"").split() if not since_uid or int(u) > since_uid or validity != uidvalidity]
        uids = sorted(uids)[-limit:]
        highest = max(uids) if uids else (since_uid or 0)
        gmail = "Gmail" in box

        headers: list[RawEmail] = []
        for start in range(0, len(uids), 200):
            chunk = ",".join(str(u) for u in uids[start:start + 200])
            items = "(UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])" if gmail \
                else "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
            status, rows = imap.uid("FETCH", chunk, items)
            if status != "OK":
                continue
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row[0], row[1]
                uid_match = _FETCH_META_RE.search(meta)
                if not uid_match:
                    continue
                thr = _THRID_RE.search(meta)
                headers.append(parse_headers(int(uid_match.group(1)), raw, format(int(thr.group(1)), "x") if thr else None))

        kept = [h for h in headers if wants(h)]
        by_uid = {h.uid: h for h in kept}
        for start in range(0, len(kept), 25):
            chunk = ",".join(str(h.uid) for h in kept[start:start + 25])
            status, rows = imap.uid("FETCH", chunk, "(UID BODY.PEEK[])")
            if status != "OK":
                continue
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                uid_match = _FETCH_META_RE.search(row[0])
                h = by_uid.get(int(uid_match.group(1))) if uid_match else None
                if h is not None:
                    h.text = body_text(email.message_from_bytes(row[1]))
        return kept, highest, validity
    finally:
        try:
            imap.logout()
        except Exception:
            pass
