import hashlib
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "proton.me", "live.com"}


class Lead(BaseModel):
    """One website form submission."""

    email: EmailStr
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    company: str = Field(default="", max_length=200)
    website: str = Field(default="", max_length=300)
    phone: str = Field(default="", max_length=50)
    message: str = Field(default="", max_length=5000)
    source: str = Field(default="website", max_length=100)
    # Set by the form so a resent webhook is recognised as the same submission
    submission_id: str = Field(default="", max_length=200)

    @field_validator("first_name", "last_name", "company", "website", "phone", "message", "source", "submission_id")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()

    @property
    def domain(self) -> str:
        """Company domain: from the website if given, else from a work email."""
        if self.website:
            host = urlparse(self.website if "//" in self.website else f"//{self.website}").hostname or ""
            return host.removeprefix("www.").lower()
        d = self.email.split("@")[1].lower()
        return "" if d in FREE_MAIL else d

    @property
    def key(self) -> str:
        """Idempotency key: the form's submission id, else a hash of the content."""
        if self.submission_id:
            return f"sub:{self.submission_id}"
        raw = f"{self.email.lower()}|{self.message}|{self.source}"
        return "hash:" + hashlib.sha256(raw.encode()).hexdigest()[:32]

    @property
    def deal_name(self) -> str:
        who = self.company or self.domain or f"{self.first_name} {self.last_name}".strip() or self.email
        return f"{who} – inbound ({self.source})"
