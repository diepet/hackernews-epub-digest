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
import re
import markdown
from bs4 import BeautifulSoup 

from litellm import completion
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from ebooklib import epub

# --- DISABLE SSL WARNINGS ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(funcName)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION VARIABLES ---
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini/gemini-1.5-flash")
TARGET_LANGUAGE = os.environ.get("TARGET_LANGUAGE", "English")

try:
    STORY_LIMIT = int(os.environ.get("STORY_LIMIT", 10))
except ValueError:
    logger.warning("Invalid STORY_LIMIT. Defaulting to 10.")
    STORY_LIMIT = 10

try:
    LLM_REQUEST_DELAY = float(os.environ.get("LLM_REQUEST_DELAY", 0))
except ValueError:
    LLM_REQUEST_DELAY = 0

# --- NEW ENV VARIABLE ADDED HERE ---
try:
    SCRAPE_CHAR_LIMIT = int(os.environ.get("SCRAPE_CHAR_LIMIT", 60000))
except ValueError:
    logger.warning("Invalid SCRAPE_CHAR_LIMIT. Defaulting to 60000.")
    SCRAPE_CHAR_LIMIT = 60000

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
    logger.info(f"LLM Delay: {LLM_REQUEST_DELAY}s")
    logger.info(f"Scrape Limit: {SCRAPE_CHAR_LIMIT} chars") # Log the new limit
    
    if SMTP_USER: logger.info(f"SMTP User: {SMTP_USER[:3]}***")
    else: logger.error("CRITICAL: SMTP_USER is missing!")
        
    if SMTP_PASSWORD: logger.info("SMTP Password: [SET]")
    else: logger.error("CRITICAL: SMTP_PASSWORD is missing!")
        
    if PUBLISH_EMAIL: logger.info(f"Destination Email: {PUBLISH_EMAIL}")
    else: logger.error("CRITICAL: PUBLISH_EMAIL is missing!")

    logger.info("--- CONFIGURATION CHECK COMPLETE ---")

def get_top_stories(limit):
    """Fetches top stories metadata."""
    logger.info(f"Connecting to HN API to fetch top {limit} stories...")
    try:
        response = requests.get(HN_TOP_STORIES_URL, timeout=10, verify=False)
        response.raise_for_status() 
        top_ids = response.json()[:limit]
    except Exception as e:
        logger.error(f"Failed to fetch Top Stories: {e}")
        return []

    stories = []
    for index, story_id in enumerate(top_ids):
        try:
            logger.info(f"Fetching item {story_id} ({index+1}/{limit})...")
            item_resp = requests.get(HN_ITEM_URL.format(story_id), timeout=5, verify=False)
            item = item_resp.json()
            
            if item and item.get('url') and item.get('type') == 'story':
                stories.append({
                    'title': item.get('title'),
                    'url': item.get('url'),
                    'by': item.get('by')
                })
            else:
                logger.warning(f"Skipping item {story_id}: No URL or not a story.")
                
        except Exception as e:
            logger.warning(f"Error fetching story {story_id}: {e}")
            continue
    return stories

def fetch_article_text(url):
    """Downloads web page and extracts text using BeautifulSoup."""
    logger.info(f"Downloading content from: {url}")
    
    # Updated headers to emulate Chrome 143 on Windows
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Referer': 'https://news.ycombinator.com/', 
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Ch-Ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'cross-site',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
    }
    
    try:
        session = requests.Session()
        response = session.get(url, headers=headers, timeout=15, verify=False)
        
        # Retry with Mobile UA on 403
        if response.status_code == 403:
            logger.warning("Got 403. Retrying with Mobile UA...")
            mobile_headers = headers.copy()
            mobile_headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
            mobile_headers['Sec-Ch-Ua-Mobile'] = '?1'
            mobile_headers['Sec-Ch-Ua-Platform'] = '"iOS"'
            response = session.get(url, headers=mobile_headers, timeout=15, verify=False)

        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
            script.decompose()

        paragraphs = soup.find_all('p')
        if len(paragraphs) < 3:
            text_content = soup.get_text(separator=' ')
        else:
            text_content = ' '.join([p.get_text() for p in paragraphs])
        
        text_content = ' '.join(text_content.split())
        
        if len(text_content) < 300:
             logger.warning(f"Content too short ({len(text_content)} chars).")
             return None

        # --- UPDATED LINE: USING ENV VARIABLE ---
        return text_content[:SCRAPE_CHAR_LIMIT]  
        
    except Exception as e:
        logger.warning(f"Failed to scrape text from {url}: {e}")
        return None

def summarize_article(url, title):
    """Summarizes the article text using LLM."""
    logger.info(f"🤖 Processing summary for: '{title}'...")

    article_text = fetch_article_text(url)
    
    if not article_text:
        logger.warning(f"🚫 Scraping failed for '{title}'. SKIPPING LLM CALL.")
        return "_Could not scrape content from this URL. No summary available._"

    logger.info(f"Successfully scraped {len(article_text)} chars. Sending to LLM.")
    
    # Updated Prompt: Force dash bullets and explicit spacing
    prompt = f"""
    You are a helpful technical assistant.
    Task: Summarize the article text provided below.
    
    Article Title: {title}
    Source URL: {url}
    
    [START OF ARTICLE TEXT]
    {article_text}
    [END OF ARTICLE TEXT]
    
    Requirements:
    1. Make a detailed summary of the article.
    2. **Use standard dashes '-' for bullet points.** Do NOT use asterisks '*' for lists.
    3. Ensure there is a blank line before starting any list.
    4. **CRITICAL: Write the output strictly in {TARGET_LANGUAGE}.**
    
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
        logger.error(f"❌ LLM Generation failed: {e}")
        return f"**Error generating summary:** {str(e)}"

def clean_markdown_for_html(text):
    """
    Fixes common markdown issues before conversion.
    1. Converts '* ' lists to '- ' lists.
    2. Ensures newlines before lists so they render properly.
    """
    if not text: return ""
    
    # 1. Replace ' * ' at start of line with ' - '
    text = re.sub(r'(?m)^\s*\*\s+', '- ', text)
    
    # 2. Ensure empty line before a list starts
    text = re.sub(r'(?m)^([^-].*)\n-\s', r'\1\n\n- ', text)
    
    return text

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
        
        # 2. Clean Markdown (Fix asterisks and spacing)
        clean_md = clean_markdown_for_html(summary_markdown)
        
        # 3. Convert Markdown to HTML
        summary_html = markdown.markdown(clean_md, extensions=['extra', 'smarty', 'nl2br'])
        
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
        
        if LLM_REQUEST_DELAY > 0:
            logger.info(f"Sleeping for {LLM_REQUEST_DELAY}s...")
            time.sleep(LLM_REQUEST_DELAY)

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