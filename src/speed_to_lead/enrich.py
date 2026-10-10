"""Company enrichment from the lead's own website: title, description and buying signals.

The domain comes from a stranger's form, so the fetch is guarded: public addresses only (no internal network,
no cloud metadata endpoint), every redirect re-checked, a size cap and a short timeout.
"""

import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 400_000
SIGNAL_LINKS = {
    "pricing page": ("pricing", "plans"),
    "hiring": ("careers", "jobs", "join-us", "work-with-us"),
    "books demos": ("demo", "book-a-call", "contact-sales", "talk-to-sales"),
}


@dataclass
class Enrichment:
    domain: str = ""
    reachable: bool = False
    title: str = ""
    description: str = ""
    signals: list[str] = field(default_factory=list)
    # Filled by the AI step (llm.py) when configured
    industry: str = ""
    company_type: str = ""
    intent: str = ""        # high | medium | low
    summary: str = ""
    ai: bool = False
    error: str = ""


class BlockedHost(Exception):
    pass


def is_public(host: str, resolve=socket.getaddrinfo) -> bool:
    """True only if every address the host resolves to is a public internet address."""
    try:
        infos = resolve(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.description, self.links = "", "", []
        self._in_title = self._title_done = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and not self._title_done:  # later <title>s are SVG icon labels, not the page title
            self._in_title = True
        elif tag == "meta" and not self.description:
            if (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
                self.description = (a.get("content") or "").strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"].lower())

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title, self._title_done = False, True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class WebsiteEnricher:
    def __init__(self, client: httpx.Client | None = None, resolve=socket.getaddrinfo, max_redirects: int = 3):
        # Redirects are followed by hand so each new host goes through the same public-address check.
        self.client = client or httpx.Client(timeout=5, follow_redirects=False,
                                             headers={"User-Agent": "speed-to-lead/0.2 (lead enrichment)"})
        self.resolve = resolve
        self.max_redirects = max_redirects

    def enrich(self, domain: str) -> Enrichment:
        e = Enrichment(domain=domain)
        if not domain:
            return e
        try:
            html = self._fetch(f"https://{domain}/")
        except (httpx.HTTPError, BlockedHost) as err:
            e.error = f"{type(err).__name__}: {err}"[:200]
            return e
        p = _PageParser()
        try:
            p.feed(html)
        except Exception as err:  # noqa: BLE001 - broken HTML must not break the lead
            log.info("html parse failed for %s: %s", domain, err)
        e.reachable = True
        e.title = " ".join(p.title.split())[:200]
        e.description = " ".join(p.description.split())[:400]
        e.signals = [name for name, words in SIGNAL_LINKS.items()
                     if any(w in urlparse(href).path for href in p.links for w in words)]
        return e

    def _fetch(self, url: str) -> str:
        for _ in range(self.max_redirects + 1):
            host = urlparse(url).hostname or ""
            if urlparse(url).scheme not in ("http", "https") or not is_public(host, self.resolve):
                raise BlockedHost(f"refusing to fetch {host or url!r}: not a public address")
            with self.client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                resp.raise_for_status()
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return body[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
        raise httpx.TooManyRedirects(f"more than {self.max_redirects} redirects", request=None)
