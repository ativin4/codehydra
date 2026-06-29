import html as html_module
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List, Dict

_TAGS_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _assert_public_url(url: str) -> None:
    """Raises ValueError if url resolves to a private/internal IP."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed, got: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host {host!r}: {e}") from e
    for info in addr_info:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_reserved or ip.is_link_local or ip.is_loopback:
            raise ValueError(f"URL resolves to non-public address {ip}")


def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return plain text (HTML tags stripped)."""
    _assert_public_url(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CodeHydra)"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", errors="replace")
    text = _TAGS_RE.sub(" ", raw)
    text = html_module.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_chars]


class _DDGParser(HTMLParser):
    """Parse DuckDuckGo Lite result rows into {title, url, snippet} dicts."""

    def __init__(self):
        super().__init__()
        self.results: List[Dict] = []
        self._buf = ""
        self._in_link = False
        self._in_snippet = False
        self._current: Dict = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("class") == "result-link":
            self._in_link = True
            self._current = {"url": a.get("href", ""), "title": "", "snippet": ""}
            self._buf = ""
        elif tag == "td" and a.get("class") == "result-snippet":
            self._in_snippet = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self._current["title"] = html_module.unescape(self._buf.strip())
            self._in_link = False
        elif tag == "td" and self._in_snippet:
            self._current["snippet"] = html_module.unescape(
                _WS_RE.sub(" ", self._buf).strip()
            )
            self._in_snippet = False
            if self._current.get("title"):
                self.results.append(dict(self._current))

    def handle_data(self, data):
        if self._in_link or self._in_snippet:
            self._buf += data


def search(query: str, max_results: int = 5) -> List[Dict]:
    """Search via DuckDuckGo Lite (no API key). Returns [{title, url, snippet}]."""
    # DDG Lite requires POST — GET returns the homepage.
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(
        "https://lite.duckduckgo.com/lite/",
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", errors="replace")
    parser = _DDGParser()
    parser.feed(raw)
    return parser.results[:max_results]


def format_results(results: List[Dict]) -> str:
    if not results:
        return "_No results found._"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"**{i}. {r.get('title', '').strip()}**")
        lines.append(f"<{r.get('url', '')}>")
        if r.get("snippet"):
            lines.append(r["snippet"])
        lines.append("")
    return "\n".join(lines)
