"""Gmail delivery: send, draft, follow up in-thread, detect replies."""

import base64
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path

log = logging.getLogger(__name__)


class Mailer:
    def __init__(self, service, cv_path: Path | None):
        self.service = service
        self.cv_path = cv_path
        self.me = service.users().getProfile(userId="me").execute()["emailAddress"]

    def _raw(self, to: str, subject: str, body: str, attach_cv: bool, headers: dict | None = None) -> str:
        msg = MIMEMultipart()
        msg["to"], msg["from"], msg["subject"] = to, self.me, subject
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if attach_cv and self.cv_path:
            part = MIMEApplication(self.cv_path.read_bytes(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=self.cv_path.name)
            msg.attach(part)
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def send(self, to, subject, body, attach_cv=True, thread_id=None, headers=None) -> dict:
        payload = {"raw": self._raw(to, subject, body, attach_cv, headers)}
        if thread_id:
            payload["threadId"] = thread_id
        return self.service.users().messages().send(userId="me", body=payload).execute()

    def draft(self, to, subject, body, attach_cv=True) -> dict:
        payload = {"message": {"raw": self._raw(to, subject, body, attach_cv)}}
        d = self.service.users().drafts().create(userId="me", body=payload).execute()
        return {"id": d["id"], "threadId": d["message"].get("threadId")}

    def followup(self, app_row, subject, body) -> dict:
        """Reply inside the original thread so the recruiter sees the context."""
        thread = self.service.users().threads().get(
            userId="me", id=app_row["thread_id"], format="metadata",
            metadataHeaders=["Message-ID"]).execute()
        first = thread["messages"][0]
        mid = next((h["value"] for h in first["payload"]["headers"] if h["name"].lower() == "message-id"), None)
        headers = {"In-Reply-To": mid, "References": mid} if mid else None
        return self.send(app_row["to_email"], subject, body, attach_cv=False,
                         thread_id=app_row["thread_id"], headers=headers)

    # ── replies ──────────────────────────────────────────────────────────────
    def recent_inbound_thread_ids(self, days: int = 3) -> set[str]:
        """Threads that received a message from someone else recently (one cheap search)."""
        ids, token = set(), None
        while True:
            r = self.service.users().messages().list(
                userId="me", q=f"newer_than:{days}d -from:me", maxResults=500, pageToken=token).execute()
            ids.update(m["threadId"] for m in r.get("messages", []))
            token = r.get("nextPageToken")
            if not token:
                return ids

    def inbound_messages(self, thread_id: str) -> list[dict]:
        """Messages in a thread not sent by me: id, from, subject, message_id, text."""
        thread = self.service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        out = []
        for m in thread.get("messages", []):
            h = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
            addr = parseaddr(h.get("from", ""))[1].lower()
            if addr == self.me.lower():
                continue
            out.append({"id": m["id"], "from": addr, "subject": h.get("subject", ""),
                        "message_id": h.get("message-id"), "text": _plain_text(m["payload"]) or m.get("snippet", "")})
        return out

    def draft_reply(self, thread_id: str, to: str, subject: str, body: str, in_reply_to: str | None) -> str:
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        headers = {"In-Reply-To": in_reply_to, "References": in_reply_to} if in_reply_to else None
        raw = self._raw(to, subject, body, attach_cv=False, headers=headers)
        d = self.service.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": thread_id}}).execute()
        return d["id"]

    # ── digest & confirmations ───────────────────────────────────────────────
    def send_html_to_self(self, subject: str, html: str, text: str):
        msg = MIMEMultipart("alternative")
        msg["to"], msg["from"], msg["subject"] = self.me, self.me, subject
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service.users().messages().send(userId="me", body={"raw": raw}).execute()

    def search_subjects(self, query: str, limit: int = 100) -> list[str]:
        r = self.service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        subjects = []
        for m in r.get("messages", []):
            full = self.service.users().messages().get(
                userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject"]).execute()
            subjects += [h["value"] for h in full["payload"].get("headers", []) if h["name"] == "Subject"]
        return subjects

    def thread_status(self, thread_id: str) -> str:
        """'replied', 'bounced' or 'waiting'."""
        thread = self.service.users().threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=["From"]).execute()
        for m in thread.get("messages", []):
            sender = next((h["value"] for h in m["payload"]["headers"] if h["name"] == "From"), "")
            addr = parseaddr(sender)[1].lower()
            if addr == self.me.lower():
                continue
            if addr.startswith("mailer-daemon") or addr.startswith("postmaster"):
                return "bounced"
            return "replied"
        return "waiting"


def _plain_text(payload: dict) -> str:
    """Extract the text/plain body of a Gmail message payload (falls back to stripped HTML)."""
    import html as htmllib
    import re

    def walk(p):
        yield p
        for c in p.get("parts", []) or []:
            yield from walk(c)

    plain = html_part = ""
    for part in walk(payload):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
        if part.get("mimeType") == "text/plain" and not plain:
            plain = text
        elif part.get("mimeType") == "text/html" and not html_part:
            html_part = text
    if plain:
        return plain
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html_part))
