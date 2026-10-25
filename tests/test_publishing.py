import smtplib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from hn_digest import publishing
from hn_digest.config import Config
from hn_digest.fetching import Story
from hn_digest.publishing import Article


def test_epub_is_valid_xhtml_and_escapes_metadata(tmp_path):
    story = Story(1, 'Title <test> & "quotes"', "https://example.org/?a=1&b=2", author="A&B")
    article = Article(
        story,
        "A **technical** summary.\n\n- First point\n- Second point\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n---",
        "summary",
    )
    path = publishing.create_epub(
        [article], tmp_path, Config(target_language="Italian", language_code="it"), preview=False
    )
    with zipfile.ZipFile(path) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        for name in archive.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx")):
                ET.fromstring(archive.read(name))
        chapter = archive.read("EPUB/story_0.xhtml").decode()
        assert "&lt;test&gt; &amp;" in chapter
        assert "<strong>technical</strong>" in chapter
        assert "<script" not in chapter
        assert "javascript:" not in chapter
        assert 'xml:lang="it"' in chapter
        assert "HN discussion" in chapter


@pytest.mark.parametrize("port", [465, 587])
def test_smtp_uses_tls_timeout_and_epub_attachment(monkeypatch, tmp_path, port):
    path = tmp_path / "digest.epub"
    path.write_bytes(b"epub-content")
    config = Config(
        smtp_port=port,
        smtp_user="user@example.org",
        smtp_password="test-password",
        publish_email="reader@example.org",
    )
    constructor = MagicMock()
    server = constructor.return_value
    server.send_message.return_value = {}
    monkeypatch.setattr(publishing.smtplib, "SMTP_SSL" if port == 465 else "SMTP", constructor)
    publishing.send_email(path, config)
    assert constructor.call_args.kwargs["timeout"] == config.smtp_timeout
    if port == 587:
        server.starttls.assert_called_once()
    else:
        assert constructor.call_args.kwargs["context"].check_hostname
    message = server.send_message.call_args.args[0]
    attachment = next(message.iter_attachments())
    assert attachment.get_content_type() == "application/epub+zip"
    assert attachment.get_payload(decode=True) == b"epub-content"
    assert message["Date"] and message["Message-ID"]


def test_smtp_auth_failure_is_not_swallowed(monkeypatch, tmp_path):
    path = tmp_path / "digest.epub"
    path.write_bytes(b"epub")
    constructor = MagicMock()
    constructor.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication failed"
    )
    monkeypatch.setattr(publishing.smtplib, "SMTP_SSL", constructor)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        publishing.send_email(path, replace(Config(), smtp_password="wrong"))
