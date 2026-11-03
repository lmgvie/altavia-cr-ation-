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
