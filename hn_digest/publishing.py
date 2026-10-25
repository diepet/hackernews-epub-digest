"""EPUB rendering and SMTP delivery. Errors propagate to the caller."""

import datetime as dt
import re
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

import markdown
import nh3
import structlog
from bs4 import BeautifulSoup
from ebooklib import epub

from hn_digest.config import Config
from hn_digest.fetching import Story

log = structlog.get_logger(__name__)
EPUB_STYLES = b"""p { margin: 0 0 0.8em; }
ul, ol { margin: 0.3em 0 1em; padding-left: 1.5em; }
li { margin-bottom: 0.7em; }
"""


@dataclass(frozen=True)
class Article:
    story: Story
    text: str
    status: str


def prepare_summary_markdown(text: str) -> str:
    """Give section labels and bullet labels reliable breaks without editing their text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Fenced examples and indented code keep their original Markdown formatting.
    chunks = re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", text)
    for index in range(0, len(chunks), 2):
        lines = []
        for line in chunks[index].split("\n"):
            heading = re.fullmatch(r"(\*\*[^*\n]+\*\*:?)(?:[ \t]+(.*))?", line)
            if heading:
                lines.extend([heading[1], ""])
                line = heading[2] or ""
            # Repair a collapsed row of labeled bullets only when a list has already begun.
            if re.match(r"^[ ]{0,3}[-*+] +\*\*", line):
                bullets = re.split(r"[ \t]+(?=[-*+] +(?:\*\*[^*\n]+:\*\*|\*\*[^*\n]+\*\*:))", line)
            else:
                bullets = [line]
            for bullet in bullets:
                label = re.fullmatch(
                    r"([ ]{0,3})([-*+] +)(\*\*[^*\n]+\*\*:?)(?:[ \t]+(.*))?", bullet
                )
                if label:
                    # A Markdown hard break survives EPUB rendering, unlike a soft newline.
                    lines.append(f"{label[1]}{label[2]}{label[3]}  ")
                    if label[4]:
                        lines.append(f"{label[1]}    {label[4]}")
                else:
                    lines.append(bullet)
        chunks[index] = "\n".join(lines)
    return "".join(chunks)


def render_markdown(text: str) -> str:
    text = prepare_summary_markdown(text)
    rendered = markdown.markdown(text, extensions=["extra", "smarty"], output_format="xhtml")
    cleaned = nh3.clean(
        rendered,
        tags={
            "p",
            "br",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
            "h2",
            "h3",
            "h4",
            "hr",
            "a",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
        },
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )
    # nh3 serializes HTML5 void tags; EPUB chapters must be well-formed XHTML.
    return BeautifulSoup(cleaned, "html.parser").decode(formatter="minimal")


def summary_body_word_count(text: str) -> int:
    """Count prose and takeaway labels, excluding tags and standalone section headings."""
    body = "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith("Tags:")
        and not re.fullmatch(r"\*\*[^*]+\*\*:?[ \t]*", line.strip())
    )
    plain = BeautifulSoup(render_markdown(body), "html.parser").get_text(" ", strip=True)
    return len(plain.split())


def create_epub(
    articles: list[Article], output_dir: Path, config: Config, *, preview: bool
) -> Path:
    if not articles:
        raise ValueError("Cannot create an empty digest")
    started = time.monotonic()
    log.info(
        "Building EPUB",
        stage="EPUB",
        stories=len(articles),
        language=config.target_language,
        summaries=sum(a.status == "summary" for a in articles),
        unavailable=sum(a.status == "unavailable" for a in articles),
    )
    now = dt.datetime.now(dt.UTC)
    edition = now.date().isoformat()
    book = epub.EpubBook()
    identifier = f"hn-digest-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    book.set_identifier(identifier)
    book.set_title(f"Hacker News Daily ({config.target_language}) - {edition}")
    book.set_language(config.language_code)
    book.add_author("Hacker News EPUB Digest")
    stylesheet = epub.EpubItem(
        uid="digest-style",
        file_name="styles/digest.css",
        media_type="text/css",
        content=EPUB_STYLES,
    )
    book.add_item(stylesheet)
    intro = epub.EpubHtml(title="Daily Digest", file_name="intro.xhtml", lang=config.language_code)
    intro.add_item(stylesheet)
    intro.content = (
        f"<h1>Hacker News Daily Digest</h1><p>{edition}</p>"
        f"<p>Language: {escape(config.target_language)}</p><p>Stories: {len(articles)}</p>"
        + (
            "<p>Scraping preview: source excerpts without AI summaries or translation.</p>"
            if preview
            else ""
        )
    )
    book.add_item(intro)
    chapters = [intro]
    available_articles = [article for article in articles if article.status != "unavailable"]
    unavailable_articles = [article for article in articles if article.status == "unavailable"]
    for index, article in enumerate(available_articles):
        log.debug(
            "Adding EPUB chapter",
            stage="EPUB",
            chapter=index + 1,
            title=article.story.title,
            content_status=article.status,
        )
        story = article.story
        if article.status == "summary":
            body = render_markdown(article.text)
        else:
            label = "Source excerpt (not an AI summary)"
            paragraphs = "".join(f"<p>{escape(p)}</p>" for p in article.text.split("\n\n"))
            body = f"<p><em>{label}</em></p>{paragraphs}"
        chapter = epub.EpubHtml(
            title=story.title, file_name=f"story_{index}.xhtml", lang=config.language_code
        )
        chapter.add_item(stylesheet)
        chapter.content = (
            f"<h2>{escape(story.title)}</h2>"
            f'<p><a href="{escape(story.url, quote=True)}">Original source</a> | '
            f'<a href="{escape(story.discussion_url, quote=True)}">HN discussion</a></p>'
            f"<p>Submitted by {escape(story.author)}</p><hr/>{body}"
        )
        book.add_item(chapter)
        chapters.append(chapter)
    if unavailable_articles:
        log.info(
            "Adding Other Articles section at the end of the EPUB",
            stage="EPUB",
            stories=len(unavailable_articles),
        )
        other_articles = epub.EpubHtml(
            title="Other Articles", file_name="other_articles.xhtml", lang="en"
        )
        other_articles.add_item(stylesheet)
        items = []
        for article in unavailable_articles:
            story = article.story
            items.append(
                f"<li><strong>{escape(story.title)}</strong> — "
                f'<a href="{escape(story.url, quote=True)}">Original article</a> | '
                f'<a href="{escape(story.discussion_url, quote=True)}">HN discussion</a></li>'
            )
        other_articles.content = "<h2>Other Articles</h2><ul>" + "".join(items) + "</ul>"
        book.add_item(other_articles)
        chapters.append(other_articles)
    book.toc = tuple(chapters)
    book.spine = ["nav", *chapters]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{identifier}.epub"
    epub.write_epub(str(path), book, {"raise_exceptions": True})
    log.info(
        "EPUB saved",
        stage="EPUB",
        path=str(path),
        stories=len(articles),
        size_bytes=path.stat().st_size,
        duration_seconds=round(time.monotonic() - started, 2),
    )
    return path


def send_email(path: Path, config: Config) -> None:
    started = time.monotonic()
    log.info(
        "Preparing EPUB email attachment",
        stage="Email",
        filename=path.name,
        size_bytes=path.stat().st_size,
    )
    sender = config.smtp_sender_email or config.smtp_user
    message = EmailMessage()
    message["From"] = sender
    message["To"] = config.publish_email
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message["Subject"] = f"Hacker News Digest - {dt.datetime.now(dt.UTC).date()}"
    message.set_content("Your daily Hacker News digest is attached as an EPUB.")
    message.add_attachment(
        path.read_bytes(), maintype="application", subtype="epub+zip", filename=path.name
    )
    context = ssl.create_default_context()
    log.info(
        "Connecting to mail server",
        stage="Email",
        port=config.smtp_port,
        security="SSL/TLS" if config.smtp_port == 465 else "STARTTLS",
        timeout_seconds=config.smtp_timeout,
    )
    # Do not retry SMTP sends: a timeout after DATA may still mean the message was delivered.
    if config.smtp_port == 465:
        server = smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=config.smtp_timeout, context=context
        )
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.smtp_timeout)
    with server:
        if config.smtp_port != 465:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            log.info("Encrypted mail connection established", stage="Email")
        log.info("Authenticating with mail server", stage="Email")
        server.login(config.smtp_user, config.smtp_password)
        log.info("Submitting digest to mail server", stage="Email")
        refused = server.send_message(message, from_addr=sender, to_addrs=[config.publish_email])
        if refused:
            raise RuntimeError("SMTP refused the digest recipient")
    log.info(
        "Mail server accepted the digest; inbox/device delivery is not confirmed",
        stage="Email",
        duration_seconds=round(time.monotonic() - started, 2),
    )
