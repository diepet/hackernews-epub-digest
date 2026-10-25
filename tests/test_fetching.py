import io
import json
from unittest.mock import MagicMock

import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hn_digest import fetching
from hn_digest.config import Config
from hn_digest.fetching import FetchError, Story

ARTICLE = (
    "A database engine reduces latency by batching disk writes and preserving transactions. " * 12
)


def test_extraction_discards_navigation_and_preserves_article():
    html = f"""<html><head><title>Database design</title></head><body>
    <nav>MENU JUNK</nav><aside>ADVERTISEMENT JUNK</aside>
    <article><h1>A new database</h1><p>{ARTICLE}</p><p>{ARTICLE}</p></article>
    <footer>FOOTER JUNK</footer><script>SCRIPT JUNK</script></body></html>"""
    text = fetching.extract_html(html, "https://example.org/article")
    assert "batching disk writes" in text
    assert "JUNK" not in text


def test_github_readme_selected_over_file_list():
    html = f'<body><div>File list</div><article class="markdown-body"><h1>README</h1><pre>{ARTICLE}</pre></article></body>'
    text = fetching.extract_html(html, "https://github.com/example/repo")
    assert "README" in text
    assert "File list" not in text


def test_access_challenge_is_not_an_article():
    with pytest.raises(FetchError, match="access challenge"):
        fetching.extract_html(
            f"<title>Just a moment...</title><p>{ARTICLE}</p>", "https://example.org"
        )


def test_ask_hn_uses_post_text_without_fetching(monkeypatch):
    download = MagicMock(side_effect=AssertionError("Network must not be used"))
    monkeypatch.setattr(fetching, "download", download)
    story = Story(
        1,
        "Ask HN",
        "https://news.ycombinator.com/item?id=1",
        text=f"<p>{ARTICLE}</p>",
        is_self_post=True,
    )
    assert fetching.fetch_article_text(MagicMock(), story, Config()).startswith("A database")
    download.assert_not_called()


def test_binary_content_not_sent_to_llm(monkeypatch):
    monkeypatch.setattr(
        fetching, "download", lambda *args: (b"binary", "image/png", "https://example.org")
    )
    with pytest.raises(FetchError, match="content type"):
        fetching.fetch_article_text(MagicMock(), Story(1, "Image", "https://example.org"), Config())


def test_plain_text_truncated(monkeypatch):
    monkeypatch.setattr(
        fetching, "download", lambda *args: (ARTICLE.encode(), "text/plain", "https://example.org")
    )
    text = fetching.fetch_article_text(
        MagicMock(), Story(1, "Text", "https://example.org"), Config(scrape_char_limit=300)
    )
    assert len(text) == 300


def pdf_bytes():
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in (ARTICLE, "SECOND PAGE CONTENT " * 20):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pdf_is_extracted_with_page_limit(monkeypatch):
    monkeypatch.setattr(
        fetching,
        "download",
        lambda *args: (pdf_bytes(), "application/octet-stream", "https://example.org/paper"),
    )
    text = fetching.fetch_article_text(
        MagicMock(), Story(1, "Paper", "https://example.org/paper"), Config(pdf_page_limit=1)
    )
    assert "database engine" in text
    assert "SECOND PAGE" not in text


def test_top_stories_replace_deleted_items_and_include_self_posts(monkeypatch):
    items = {
        "topstories.json": [1, 2, 3, 4],
        "1.json": {"deleted": True},
        "2.json": {"type": "story", "title": "Ask HN", "text": ARTICLE},
        "3.json": {"type": "story", "title": "Article", "url": "https://example.org"},
    }
    monkeypatch.setattr(
        fetching,
        "download",
        lambda session, url, config: (
            json.dumps(items[url.rsplit("/", 1)[-1]]).encode(),
            "application/json",
            url,
        ),
    )
    stories = fetching.get_top_stories(MagicMock(), Config(story_limit=2))
    assert [story.id for story in stories] == [2, 3]
    assert stories[0].is_self_post
    assert stories[0].discussion_url.endswith("id=2")


def test_empty_hn_response_fails(monkeypatch):
    monkeypatch.setattr(
        fetching, "download", lambda *args: (b"[]", "application/json", "https://example.org")
    )
    with pytest.raises(FetchError, match="no top story IDs"):
        fetching.get_top_stories(MagicMock(), Config())


def test_stream_size_limit_and_response_cleanup():
    response = MagicMock()
    response.headers = {}
    response.iter_content.return_value = [b"123", b"456"]
    session = MagicMock()
    session.get.return_value.__enter__.return_value = response
    with pytest.raises(FetchError, match="MAX_DOWNLOAD_BYTES"):
        fetching.download(session, "https://example.org", Config(max_download_bytes=5))
    session.get.return_value.__exit__.assert_called_once()
    assert "verify" not in session.get.call_args.kwargs


def test_http_error_propagates():
    session = MagicMock()
    session.get.return_value.__enter__.return_value.raise_for_status.side_effect = (
        requests.HTTPError("503")
    )
    with pytest.raises(requests.HTTPError):
        fetching.download(session, "https://example.org", Config())


def test_http_retries_are_bounded_and_only_apply_to_get():
    with fetching.make_session(Config(max_retries=2)) as session:
        retry = session.get_adapter("https://").max_retries
        assert retry.total == 2
        assert retry.allowed_methods == {"GET"}
        assert retry.retry_after_max == 30
