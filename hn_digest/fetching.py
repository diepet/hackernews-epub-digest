"""Bounded HTTP downloads, HN metadata, and article extraction."""

import io
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
import structlog
import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from trafilatura.settings import DEFAULT_CONFIG
from urllib3.util.retry import Retry

from hn_digest.config import Config
from hn_digest.logging_config import safe_url

log = structlog.get_logger(__name__)
HN_API = "https://hacker-news.firebaseio.com/v0"
CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "robot or human",
    "verify you are human",
    "security check",
)


class FetchError(RuntimeError):
    """A source could not be retrieved or contained no usable text."""


@dataclass(frozen=True)
class Story:
    id: int
    title: str
    url: str
    author: str = "Unknown"
    text: str = ""
    is_self_post: bool = False

    @property
    def discussion_url(self) -> str:
        return f"https://news.ycombinator.com/item?id={self.id}"


def http_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


class LoggedRetry(Retry):
    """Expose transient network retries without printing request headers or error bodies."""

    def increment(self, method=None, url=None, response=None, error=None, **kwargs):
        retry = super().increment(method=method, url=url, response=response, error=error, **kwargs)
        delay = retry.get_retry_after(response) if response is not None else None
        stage = "HN" if url and ("/v0/item/" in url or "topstories.json" in url) else "Source"
        log.warning(
            "HTTP request failed temporarily; retrying",
            stage=stage,
            url=safe_url(url or ""),
            retry=len(retry.history),
            reason=f"HTTP {response.status}" if response is not None else type(error).__name__,
            seconds=delay or retry.get_backoff_time(),
        )
        return retry


def make_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; HackerNewsEPUBDigest/0.2)",
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    retry = LoggedRetry(
        total=config.max_retries,
        backoff_factor=1,
        backoff_max=10,
        retry_after_max=30,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    for scheme in ("https://", "http://"):
        session.mount(scheme, HTTPAdapter(max_retries=retry))
    return session


def download(session: requests.Session, url: str, config: Config) -> tuple[bytes, str, str]:
    if not http_url(url):
        raise FetchError("Unsupported source URL")
    started = time.monotonic()
    with session.get(
        url, timeout=(config.http_timeout, config.http_timeout), stream=True
    ) as response:
        response.raise_for_status()
        length = response.headers.get("Content-Length", "")
        if length.isdecimal() and int(length) > config.max_download_bytes:
            raise FetchError("Source exceeds MAX_DOWNLOAD_BYTES")
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > config.max_download_bytes:
                raise FetchError("Source exceeds MAX_DOWNLOAD_BYTES")
            if time.monotonic() - started > config.http_timeout * (config.max_retries + 2):
                raise FetchError("Source exceeded download time limit")
            chunks.append(chunk)
        content_type = response.headers.get("Content-Type", "").lower()
        # Item metadata is logged at DEBUG; article downloads are visible at INFO.
        emit = log.debug if url.startswith(HN_API + "/") else log.info
        emit(
            "Download complete",
            stage="Source",
            url=safe_url(response.url),
            http_status=response.status_code,
            content_type=content_type or "not provided",
            size_bytes=size,
            retries=len(getattr(getattr(response.raw, "retries", None), "history", ()) or ()),
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return b"".join(chunks), content_type, response.url


def get_top_stories(session: requests.Session, config: Config) -> list[Story]:
    started = time.monotonic()
    log.info("Fetching top stories from Hacker News", stage="HN", requested=config.story_limit)
    content, _, _ = download(session, f"{HN_API}/topstories.json", config)
    ids = json.loads(content)
    if not isinstance(ids, list) or not ids:
        raise FetchError("HN returned no top story IDs")
    stories = []
    checked = 0
    log.info("Top story IDs received", stage="HN", available=len(ids))
    # Replace deleted/unavailable entries instead of silently returning fewer stories.
    for story_id in ids[: config.story_limit * 3]:
        checked += 1
        if not isinstance(story_id, int) or isinstance(story_id, bool):
            continue
        try:
            content, _, _ = download(session, f"{HN_API}/item/{story_id}.json", config)
            item = json.loads(content)
            if not isinstance(item, dict) or item.get("deleted") or item.get("dead"):
                continue
            if (
                item.get("type") != "story"
                or not isinstance(item.get("title"), str)
                or not item["title"]
            ):
                continue
            self_post = not item.get("url")
            url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            if not http_url(url) or (self_post and not item.get("text")):
                continue
            stories.append(
                Story(
                    story_id,
                    item["title"],
                    url,
                    item.get("by") or "Unknown",
                    item.get("text") or "",
                    self_post,
                )
            )
            log.debug(
                "Story metadata retrieved",
                stage="HN",
                story_id=story_id,
                title=item["title"],
                url=safe_url(url),
                author=item.get("by") or "Unknown",
            )
        except (requests.RequestException, ValueError, FetchError) as exc:
            log.warning(
                "HN item unavailable; trying the next item",
                stage="HN",
                story_id=story_id,
                error_type=type(exc).__name__,
            )
        if len(stories) == config.story_limit:
            break
    if not stories:
        raise FetchError("No usable HN stories were returned")
    log.info(
        "HN metadata retrieved",
        stage="HN",
        checked=checked,
        selected=len(stories),
        skipped=checked - len(stories),
        duration_seconds=round(time.monotonic() - started, 2),
    )
    return stories


def extract_html(content: bytes | str, url: str) -> str:
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(title.startswith(marker) for marker in CHALLENGE_TITLES):
        raise FetchError("Source returned an access challenge")
    for node in soup(["script", "style", "nav", "footer", "aside", "iframe", "noscript", "form"]):
        node.decompose()
    # Prefer the GitHub README over repository navigation and file listings.
    readme = soup.select_one("article.markdown-body")
    if readme:
        log.info("Extracting GitHub README", stage="Source", method="GitHub README")
        return readme.get_text("\n", strip=True)
    cleaned = str(soup)
    extraction_config = deepcopy(DEFAULT_CONFIG)
    extraction_config["DEFAULT"]["MAX_TREE_SIZE"] = "100000"
    text = trafilatura.extract(
        cleaned,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        config=extraction_config,
    )
    if text and len(text.strip()) >= 200:
        log.info("HTML article extracted", stage="Source", method="Trafilatura")
        return text
    log.info("Using HTML extraction fallback", stage="Source", method="Beautiful Soup")
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return main.get_text("\n", strip=True)


def extract_pdf(content: bytes, config: Config) -> str:
    reader = PdfReader(io.BytesIO(content))
    parts = []
    size = 0
    for page in reader.pages[: config.pdf_page_limit]:
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > config.max_download_bytes:
            raise FetchError("PDF page content stream is too large")
        text = page.extract_text() or ""
        parts.append(text)
        size += len(text)
        if size >= config.scrape_char_limit:
            break
    log.info(
        "PDF text extracted",
        stage="Source",
        pages_read=len(parts),
        total_pages=len(reader.pages),
        characters=size,
        limited=len(parts) < len(reader.pages),
    )
    return "\n\n".join(parts)


def fetch_article_text(session: requests.Session, story: Story, config: Config) -> str:
    try:
        if story.is_self_post:
            log.info(
                "Extracting HN post text from API metadata", stage="Source", method="HN self post"
            )
            text = BeautifulSoup(story.text, "html.parser").get_text("\n", strip=True)
        else:
            log.info("Downloading article", stage="Source", url=safe_url(story.url))
            content, content_type, final_url = download(session, story.url, config)
            if content.lstrip().startswith(b"%PDF-") or "application/pdf" in content_type:
                text = extract_pdf(content, config)
            elif "text/plain" in content_type:
                log.info("Decoding plain text source", stage="Source", method="plain text")
                text = content.decode("utf-8", errors="replace")
            elif not content_type or "html" in content_type:
                text = extract_html(content, final_url)
            else:
                raise FetchError("Unsupported source content type")
        text = re.sub(r"[\t\r ]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if len(text) < (40 if story.is_self_post else 200):
            raise FetchError("Source contains too little readable text")
        kept = text[: config.scrape_char_limit]
        if len(kept) < len(text):
            log.info(
                "Source shortened to SCRAPE_CHAR_LIMIT",
                stage="Source",
                extracted_characters=len(text),
                kept_characters=len(kept),
            )
        return kept
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise FetchError(f"Source returned HTTP {status}") from exc
    except (requests.RequestException, ValueError) as exc:
        # Do not put upstream exception bodies (possibly containing credentials) in the digest/log.
        raise FetchError(f"Source request failed ({type(exc).__name__})") from exc
