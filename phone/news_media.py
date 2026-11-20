"""Select an actual image or video advertised by a public article."""

from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from news import NoRedirect


ARTICLE_HOSTS = {
    "pg21.ru": {"pg21.ru", "cdn.pg21.ru", "img.pg21.ru"},
    "moygorod.online": {"moygorod.online"},
    "chebnovosti.rchuv.ru": {"chebnovosti.rchuv.ru", "fs02.rchuv.ru"},
    "max.ru": {"i.oneme.ru", "v.oneme.ru"},
}
PLACEHOLDER = re.compile(r"(?:og[-_]?default|placeholder|no[-_]?photo|favicon|\blogo\b|default[-_]?og)", re.I)


class ArticleVisuals(HTMLParser):
    def __init__(self):
        super().__init__()
        self.photos = []
        self.videos = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "meta":
            name = data.get("property") or data.get("name")
            if name in ("og:video", "og:video:url", "og:video:secure_url"):
                self.videos.append(data.get("content"))
            elif name in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
                self.photos.append(data.get("content"))


def select_visual(html, article_url):
    allowed = ARTICLE_HOSTS.get(urlsplit(article_url).netloc, set())
    visual = ArticleVisuals()
    visual.feed(html)
    for kind, urls in (("video", visual.videos), ("photo", visual.photos)):
        for value in urls:
            if not value:
                continue
            full_url = urljoin(article_url, value.strip())
            parts = urlsplit(full_url)
            if (parts.scheme != "https" or parts.netloc not in allowed or parts.fragment
                    or PLACEHOLDER.search(parts.path) or len(full_url) > 1800):
                continue
            if kind == "video" and not parts.path.lower().endswith(".mp4"):
                continue
            if kind == "photo" and parts.netloc != "i.oneme.ru" and not parts.path.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp")):
                continue
            return kind, full_url
    return None


def fetch_visual(article_url):
    parts = urlsplit(article_url)
    if parts.scheme != "https" or parts.netloc not in ARTICLE_HOSTS:
        return None
    request = Request(article_url, headers={"User-Agent": "CheTamCheboksary/1.2"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            html = response.read(750_000).decode("utf-8", errors="replace")
        return select_visual(html, article_url)
    except (OSError, ValueError):
        return None
