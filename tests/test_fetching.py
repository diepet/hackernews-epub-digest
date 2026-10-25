import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from urllib3.response import HTTPResponse

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
        assert not retry.raise_on_status
        assert 403 not in retry.status_forcelist


def test_form_explanations_survive_and_empty_main_does_not_hide_them(monkeypatch):
    monkeypatch.setattr(fetching.trafilatura, "extract", lambda *args, **kwargs: None)
    html = f"""<body><form><label>SEARCH CONTROL</label><input value="PRIVATE INPUT">
    <div><p>{ARTICLE}</p></div><button>SUBMIT CONTROL</button></form>
    <main hidden><p>HIDDEN TEMPLATE</p></main><main></main></body>"""
    text = fetching.extract_html(html, "https://example.org/interactive")
    assert "batching disk writes" in text
    assert "CONTROL" not in text
    assert "PRIVATE INPUT" not in text
    assert "HIDDEN TEMPLATE" not in text


def test_primary_extractor_failure_falls_back_to_readable_article(monkeypatch):
    monkeypatch.setattr(
        fetching.trafilatura, "extract", MagicMock(side_effect=ValueError("parser failure"))
    )
    assert ARTICLE.strip() in fetching.extract_html(
        f"<article><p>{ARTICLE}</p></article>", "https://example.org"
    )


def test_structured_article_body_is_recovered_without_using_social_descriptions(monkeypatch):
    monkeypatch.setattr(fetching.trafilatura, "extract", lambda *args, **kwargs: None)
    data = {"@graph": [{"@type": "NewsArticle", "articleBody": ARTICLE}]}
    html = (
        '<meta property="og:description" content="TEASER ONLY">'
        '<script type="application/ld+json">invalid json</script>'
        f'<script type="application/ld+json">{json.dumps(data)}</script><body></body>'
    )
    assert fetching.extract_html(html, "https://example.org") == ARTICLE.strip()


def test_paid_structured_content_is_not_used_as_a_fallback(monkeypatch):
    monkeypatch.setattr(fetching.trafilatura, "extract", lambda *args, **kwargs: None)
    data = {"@type": "NewsArticle", "articleBody": ARTICLE, "isAccessibleForFree": False}
    html = f'<script type="application/ld+json">{json.dumps(data)}</script><body>TEASER</body>'
    assert fetching.extract_html(html, "https://example.org") == "TEASER"


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=123", "https://youtu.be/123"])
def test_video_pages_are_explicitly_unavailable_without_a_transcript(monkeypatch, url):
    download = MagicMock()
    monkeypatch.setattr(fetching, "download", download)
    with pytest.raises(FetchError, match="requires a transcript"):
        fetching.fetch_article_text(MagicMock(), Story(1, "Video", url), Config())
    download.assert_not_called()


def test_exhausted_http_rate_limit_keeps_status_in_failure_reason():
    response = MagicMock(status_code=429)
    session = MagicMock()
    session.get.return_value.__enter__.return_value = response
    with pytest.raises(FetchError, match="HTTP 429.*retry budget exhausted"):
        fetching.fetch_article_text(session, Story(1, "Title", "https://example.org"), Config())


def test_http_rate_limit_waits_before_first_retry():
    retry = fetching.LoggedRetry(total=2, backoff_factor=1)
    first = retry.increment(method="GET", url="/article", response=HTTPResponse(status=429))
    second = first.increment(method="GET", url="/article", response=HTTPResponse(status=429))
    assert first.get_backoff_time() == 1
    assert second.get_backoff_time() == 2


def test_invalid_http_retry_hint_uses_backoff_and_excessive_wait_is_not_shortened():
    retry = fetching.LoggedRetry(total=2, retry_after_max=30)
    assert retry.get_retry_after(HTTPResponse(headers={"Retry-After": "invalid"})) is None
    with pytest.raises(FetchError, match="skipping instead of retrying early"):
        retry.get_retry_after(HTTPResponse(headers={"Retry-After": "3600"}))


@pytest.mark.parametrize("final_status", [200, 429])
def test_http_adapter_retries_rate_limits_and_preserves_the_final_response(
    monkeypatch, final_status
):
    sleep = MagicMock()
    monkeypatch.setattr("urllib3.util.retry.time.sleep", sleep)
    statuses = [429, 429, final_status]

    class Handler(BaseHTTPRequestHandler):
        count = 0

        def do_GET(self):
            status = statuses[self.count]
            type(self).count += 1
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(ARTICLE)))
            self.end_headers()
            self.wfile.write(ARTICLE.encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/article"
        try:
            with fetching.make_session(Config(max_retries=2)) as session:
                session.trust_env = False
                if final_status == 200:
                    assert fetching.fetch_article_text(session, Story(1, "Title", url), Config())
                else:
                    with pytest.raises(FetchError, match="HTTP 429.*retry budget exhausted"):
                        fetching.fetch_article_text(session, Story(1, "Title", url), Config())
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert Handler.count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
