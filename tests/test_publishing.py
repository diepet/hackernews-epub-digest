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
        assert "EPUB/other_articles.xhtml" not in archive.namelist()


@pytest.mark.parametrize("available_status", ["summary", "excerpt"])
def test_unavailable_stories_form_one_final_section_with_both_links(tmp_path, available_status):
    first_unavailable = Story(
        1, 'First unavailable <title> & "quotes"', 'https://example.org/first?a=1&b="two"'
    )
    second_unavailable = Story(3, "Second unavailable", "https://example.org/second")
    articles = [
        Article(first_unavailable, "Unavailable notice must not appear", "unavailable"),
        Article(
            Story(2, "First readable", "https://example.org/readable/1"),
            "First text.",
            available_status,
        ),
        Article(second_unavailable, "Provider failure must not appear", "unavailable"),
        Article(
            Story(4, "Second readable", "https://example.org/readable/2"),
            "Second text.",
            available_status,
        ),
    ]
    path = publishing.create_epub(
        articles, tmp_path, Config(), preview=available_status == "excerpt"
    )
    ns = {"x": "http://www.w3.org/1999/xhtml", "opf": "http://www.idpf.org/2007/opf"}
    with zipfile.ZipFile(path) as archive:
        other = ET.fromstring(archive.read("EPUB/other_articles.xhtml"))
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        package = ET.fromstring(archive.read("EPUB/content.opf"))
        titles = [
            ET.fromstring(archive.read(f"EPUB/story_{i}.xhtml")).find(".//x:h2", ns).text
            for i in range(2)
        ]
        assert titles == ["First readable", "Second readable"]
        assert "EPUB/story_2.xhtml" not in archive.namelist()
    assert other.find(".//x:h2", ns).text == "Other Articles"
    assert other.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"
    bullets = other.findall(".//x:ul/x:li", ns)
    assert len(bullets) == 2
    for bullet, story in zip(bullets, [first_unavailable, second_unavailable], strict=True):
        assert bullet.find("x:strong", ns).text == story.title
        links = bullet.findall("x:a", ns)
        assert [(link.text, link.get("href")) for link in links] == [
            ("Original article", story.url),
            ("HN discussion", story.discussion_url),
        ]
    assert "must not appear" not in "".join(other.itertext())
    nav_links = nav.findall(".//x:nav/x:ol/x:li/x:a", ns)
    assert [link.get("href") for link in nav_links] == [
        "intro.xhtml",
        "story_0.xhtml",
        "story_1.xhtml",
        "other_articles.xhtml",
    ]
    manifest = {
        item.get("id"): item.get("href") for item in package.findall("opf:manifest/opf:item", ns)
    }
    spine = [manifest[item.get("idref")] for item in package.findall("opf:spine/opf:itemref", ns)]
    assert spine[-1] == "other_articles.xhtml"


def test_compact_key_messages_keep_bold_labels_bullets_and_original_link(tmp_path):
    summary = (
        "**Una mappa confronta il valore di mercato e i dipendenti delle aziende.**\n\n"
        "- **Come funziona:** le bolle rappresentano la capitalizzazione.\n"
        "- **Messaggio chiave:** il valore per dipendente varia tra aziende e settori.\n"
        "- **Limite:** i dati sugli organici possono essere meno recenti."
    )
    story = Story(1, "Market map", "https://example.org/article?id=1&view=full")
    path = publishing.create_epub(
        [Article(story, summary, "summary")], tmp_path, Config(), preview=False
    )
    with zipfile.ZipFile(path) as archive:
        chapter = ET.fromstring(archive.read("EPUB/story_0.xhtml"))
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    bullets = chapter.findall(".//x:ul/x:li", ns)
    assert len(bullets) == 3
    assert all(bullet.find("x:strong", ns) is not None for bullet in bullets)
    assert any("Una mappa" in (node.text or "") for node in chapter.findall(".//x:p/x:strong", ns))
    links = chapter.findall(".//x:a", ns)
    assert any(link.get("href") == story.url and link.text == "Original source" for link in links)


def test_section_heading_followed_immediately_by_bullets_renders_a_real_list(tmp_path):
    summary = (
        "Tags: Software, Database, Ricerca\n\n"
        "**Idea centrale**\nUn risultato importante.\n\n"
        "**Messaggi chiave**\n"
        "- **Prova:** Due misure indipendenti.\n"
        "- **Limite:** Campione piccolo.\n\n"
        "**Contesto e dettagli**\nServe ulteriore ricerca."
    )
    path = publishing.create_epub(
        [Article(Story(1, "Research", "https://example.org"), summary, "summary")],
        tmp_path,
        Config(),
        preview=False,
    )
    with zipfile.ZipFile(path) as archive:
        chapter = ET.fromstring(archive.read("EPUB/story_0.xhtml"))
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    assert len(chapter.findall(".//x:ul/x:li", ns)) == 2
    assert publishing.summary_body_word_count(summary) == 13


@pytest.mark.parametrize("separator", ["\n", "\r\n", " "])
def test_section_and_bullet_labels_have_visible_breaks_in_epub(tmp_path, separator):
    summary = (
        "Tags: Software, DevOps, Compilatori\n\n"
        f"**Idea centrale**{separator}Farol indica i rischi di una modifica.\n\n"
        f"**Messaggi chiave**{separator}"
        "- **Analisi deterministica**: Esamina le dipendenze del codice.\n"
        "- **Triage mirato:** Ordina i file per rischio.\n\n"
        f"**Contesto e dettagli**{separator}Il report aiuta il revisore."
    )
    path = publishing.create_epub(
        [Article(Story(1, "Farol", "https://example.org/farol"), summary, "summary")],
        tmp_path,
        Config(),
        preview=False,
    )
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    with zipfile.ZipFile(path) as archive:
        chapter = ET.fromstring(archive.read("EPUB/story_0.xhtml"))
        assert archive.read("EPUB/styles/digest.css") == publishing.EPUB_STYLES
    stylesheet = chapter.find("x:head/x:link[@rel='stylesheet']", ns)
    assert stylesheet is not None and stylesheet.get("href") == "styles/digest.css"
    paragraphs = chapter.findall(".//x:p", ns)
    central = next(p for p in paragraphs if "".join(p.itertext()) == "Idea centrale")
    context = next(p for p in paragraphs if "".join(p.itertext()) == "Contesto e dettagli")
    assert "".join(paragraphs[paragraphs.index(central) + 1].itertext()).startswith("Farol indica")
    assert "".join(paragraphs[paragraphs.index(context) + 1].itertext()).startswith("Il report")
    bullets = chapter.findall(".//x:ul/x:li", ns)
    assert len(bullets) == 2
    for bullet, explanation in zip(
        bullets, ["Esamina le dipendenze", "Ordina i file"], strict=True
    ):
        assert bullet.find("x:strong", ns) is not None
        line_break = bullet.find("x:br", ns)
        assert line_break is not None
        assert explanation in (line_break.tail or "")


def test_collapsed_bold_bullets_are_separated_and_inline_emphasis_is_preserved():
    summary = (
        "**Messaggi chiave** - **Primo:** Una spiegazione. - **Secondo**: Un limite.\n\n"
        "Una frase con **enfasi interna** e `x - **literal:** y`."
    )
    rendered = ET.fromstring("<body>" + publishing.render_markdown(summary) + "</body>")
    bullets = rendered.findall("ul/li")
    assert len(bullets) == 2
    assert [bullet.find("strong").text for bullet in bullets] == ["Primo:", "Secondo"]
    prose = rendered.findall("p")[-1]
    assert prose.find("br") is None
    assert prose.find("code").text == "x - **literal:** y"


def test_markdown_list_spacing_does_not_rewrite_code_examples():
    code = "```text\n**Heading**\n- literal example\n```"
    rendered = publishing.render_markdown(code)
    assert "<ul>" not in rendered
    assert "**Heading**\n- literal example" in rendered


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
