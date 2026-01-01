import os
import smtplib
import datetime
import logging
import requests
import sys
import tempfile
import time
import ssl
import urllib3
import markdown  # <--- NEW: Import the Markdown library
from bs4 import BeautifulSoup 

# Import the generic LLM "facade" library
from litellm import completion

# Import email handling libraries
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# Import library to generate EPUB files
from ebooklib import epub

# --- DISABLE SSL WARNINGS (Unsafe Mode) ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(funcName)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# --- CONFIGURATION VARIABLES ---
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini/gemini-1.5-flash")
TARGET_LANGUAGE = os.environ.get("TARGET_LANGUAGE", "English")

try:
    STORY_LIMIT = int(os.environ.get("STORY_LIMIT", 10))
except ValueError:
    logger.warning("Invalid STORY_LIMIT format. Defaulting to 10.")
    STORY_LIMIT = 10

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com") 
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER")           
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")   
SMTP_SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL") 
PUBLISH_EMAIL = os.environ.get("PUBLISH_EMAIL")   

def check_config():
    """Logs current config."""
    logger.info("--- STARTING CONFIGURATION CHECK ---")
    logger.info(f"Target Language: {TARGET_LANGUAGE}")
    logger.info(f"Story Limit: {STORY_LIMIT}")
    logger.info(f"LLM Model: {LLM_MODEL}")
    
    if SMTP_USER:
        logger.info(f"SMTP User: {SMTP_USER[:3]}***")
    else:
        logger.error("CRITICAL: SMTP_USER is missing!")
        
    if SMTP_PASSWORD:
        logger.info("SMTP Password: [SET]")
    else:
        logger.error("CRITICAL: SMTP_PASSWORD is missing!")
        
    if PUBLISH_EMAIL:
        logger.info(f"Destination Email: {PUBLISH_EMAIL}")
    else:
        logger.error("CRITICAL: PUBLISH_EMAIL is missing!")

    logger.info("--- CONFIGURATION CHECK COMPLETE ---")

def get_top_stories(limit):
    """Fetches top stories metadata."""
    logger.info(f"Connecting to Hacker News API to fetch top {limit} stories...")
    try:
        response = requests.get(HN_TOP_STORIES_URL, timeout=10, verify=False)
        response.raise_for_status() 
        top_ids = response.json()[:limit]
    except Exception as e:
        logger.error(f"Failed to fetch Top Stories list: {e}")
        return []

    stories = []
    for index, story_id in enumerate(top_ids):
        try:
            logger.info(f"Fetching details for item {story_id} ({index+1}/{limit})...")
            item_resp = requests.get(HN_ITEM_URL.format(story_id), timeout=5, verify=False)
            item = item_resp.json()
            
            if item and item.get('url') and item.get('type') == 'story':
                stories.append({
                    'title': item.get('title'),
                    'url': item.get('url'),
                    'by': item.get('by')
                })
            else:
                logger.warning(f"Skipping item {story_id}: No URL found or not a story.")
                
        except Exception as e:
            logger.warning(f"Error fetching details for story ID {story_id}: {e}")
            continue
    return stories

def fetch_article_text(url):
    """
    Downloads the web page and extracts text using BeautifulSoup.
    Uses 'Rich Headers' to mimic a real modern browser and bypass 403 errors.
    """
    logger.info(f"Downloading content from: {url}")
    
    # 1. Masquerade as a real, modern Chrome browser on macOS
    # 2. Add 'Referer' to pretend we clicked the link from Hacker News (whitelisted by many blogs)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://news.ycombinator.com/', 
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'cross-site',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
    }
    
    try:
        # We use a session to better handle cookies if the site sets them on redirect
        session = requests.Session()
        response = session.get(url, headers=headers, timeout=15, verify=False)
        
        # Handle 403 specifically by trying a backup User-Agent (sometimes mobile works better)
        if response.status_code == 403:
            logger.warning("Got 403 with Desktop UA. Retrying with Mobile UA...")
            headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
            response = session.get(url, headers=headers, timeout=15, verify=False)

        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # REMOVE JUNK: aggressive cleaning of non-article elements
        for script in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
            script.decompose() # Rip them out of the DOM

        # Strategy: Extract text from paragraphs, but fallback to div if p is missing
        paragraphs = soup.find_all('p')
        if len(paragraphs) < 3:
            # Fallback for sites that use divs for text (like some React apps)
            text_content = soup.get_text(separator=' ')
        else:
            text_content = ' '.join([p.get_text() for p in paragraphs])
        
        # Clean up whitespace
        text_content = ' '.join(text_content.split())
        
        if len(text_content) < 300:
             logger.warning(f"Content too short ({len(text_content)} chars). Likely anti-bot blockage or paywall.")
             return None

        return text_content[:15000]
        
    except Exception as e:
        logger.warning(f"Failed to scrape text from {url}: {e}")
        return None

def summarize_article(url, title):
    """
    Summarizes the article text using LLM.
    Returns Markdown text.
    """
    logger.info(f"🤖 Processing summary for: '{title}'...")

    article_text = fetch_article_text(url)
    
    if not article_text:
        logger.warning(f"🚫 Scraping failed for '{title}'. SKIPPING LLM CALL.")
        return "_Could not scrape content from this URL. No summary available._"

    logger.info(f"Successfully scraped {len(article_text)} chars. Sending to LLM.")
    prompt = f"""
    You are a helpful technical assistant.
    Task: Summarize the article text provided below.
    
    Article Title: {title}
    Source URL: {url}
    
    [START OF ARTICLE TEXT]
    {article_text}
    [END OF ARTICLE TEXT]
    
    Requirements:
    1. Make a good and detailed summary of the article.
    2. **CRITICAL: Write the output strictly in {TARGET_LANGUAGE}.**
    
    Output Format:
    - Return strictly Markdown formatted text.
    """
    
    try:
        response = completion(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ LLM Generation failed for '{title}'. Error: {e}")
        return f"**Error generating summary:** {str(e)}"

def create_epub(stories):
    """Compiles stories into EPUB."""
    if not stories:
        logger.warning("No stories provided. Aborting.")
        return None

    date_str = datetime.date.today().strftime("%Y-%m-%d")
    temp_dir = os.path.join(tempfile.gettempdir(), "hackernews-epub-digest-files")
    os.makedirs(temp_dir, exist_ok=True) 
    
    filename = f'hn_digest_{date_str}.epub'
    full_path = os.path.join(temp_dir, filename)
    
    logger.info(f"📚 Starting EPUB book creation. Target file: {full_path}")
    
    book = epub.EpubBook()
    book.set_identifier(f'hn-digest-{date_str}')
    book.set_title(f'Hacker News Daily ({TARGET_LANGUAGE}) - {date_str}')
    book.set_language(TARGET_LANGUAGE.lower()[:2])
    book.add_author('Daily Bot')

    chapters = []
    
    intro = epub.EpubHtml(title='Cover', file_name='intro.xhtml', lang='en')
    intro.content = (
        f"<h1>Hacker News Daily Digest</h1>"
        f"<p><b>Date:</b> {date_str}</p>"
        f"<p><b>Language:</b> {TARGET_LANGUAGE}</p>"
        f"<p><b>Stories:</b> {len(stories)}</p>"
    )
    book.add_item(intro)
    chapters.append(intro)

    for i, story in enumerate(stories):
        logger.info(f"Processing Story {i+1}/{len(stories)}: {story['title']}")
        
        # 1. Get Markdown Summary from LLM
        summary_markdown = summarize_article(story['url'], story['title'])
        
        # 2. Convert Markdown to HTML
        # We use extensions for extra features like tables or fenced code blocks if needed
        summary_html = markdown.markdown(summary_markdown, extensions=['extra', 'smarty'])
        
        content = f"""
            <h2>{story['title']}</h2>
            <p><b>Link:</b> <a href="{story['url']}">{story['url']}</a></p>
            <p><i>By: {story.get('by', 'Unknown')}</i></p>
            <hr/>
            <div class="summary">
                {summary_html}
            </div>
        """
        
        chapter = epub.EpubHtml(title=story['title'], file_name=f'chap_{i}.xhtml', lang='en')
        chapter.content = content
        book.add_item(chapter)
        chapters.append(chapter)
        
        time.sleep(1)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav'] + chapters

    logger.info("Writing EPUB file to disk...")
    try:
        epub.write_epub(full_path, book, {})
        logger.info(f"✅ EPUB successfully created at: {full_path}")
        return full_path
    except Exception as e:
        logger.error(f"❌ Failed to write EPUB file: {e}")
        return None

def send_email(file_path):
    """Sends EPUB via email."""
    if not SMTP_USER or not SMTP_PASSWORD or not PUBLISH_EMAIL:
        logger.error("Missing email credentials. Cannot send email.")
        return

    logger.info(f"📧 Preparing to send email to {PUBLISH_EMAIL}...")
    
    msg = MIMEMultipart()
    sender = SMTP_SENDER_EMAIL if SMTP_SENDER_EMAIL else SMTP_USER
    msg['From'] = sender
    msg['To'] = PUBLISH_EMAIL
    msg['Subject'] = f"Hacker News Digest - {datetime.date.today()}"

    try:
        with open(file_path, "rb") as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(file_path)}')
        msg.attach(part)
    except Exception as e:
        logger.error(f"Failed to attach file: {e}")
        return

    context = ssl.create_default_context()
    try:
        logger.info(f"Connecting to SMTP server {SMTP_HOST}:{SMTP_PORT}...")
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(sender, PUBLISH_EMAIL, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls(context=context)
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(sender, PUBLISH_EMAIL, msg.as_string())
        logger.info("✅ Email sent successfully!")
    except Exception as e:
        logger.error(f"❌ Failed to send email: {e}")

if __name__ == "__main__":
    check_config()
    logger.info("🚀 Starting Daily Digest Automation")
    top_stories = get_top_stories(STORY_LIMIT)
    if top_stories:
        epub_path = create_epub(top_stories)
        if epub_path:
            send_email(epub_path)
    else:
        logger.warning("No stories found.")
    logger.info("🏁 Run complete.")