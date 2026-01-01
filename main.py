import os
import smtplib
import datetime
import logging
import requests
import sys
import tempfile
import time
import ssl  # <--- NEW: Added SSL library for secure context

# Import the generic LLM "facade" library
from litellm import completion

# Import email handling libraries
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# Import library to generate EPUB files
from ebooklib import epub

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

# AI Provider Setup (Default: Gemini)
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini/gemini-1.5-flash")

# Language Setup (Default: English)
TARGET_LANGUAGE = os.environ.get("TARGET_LANGUAGE", "English")

# Story Limit (Default: 10)
try:
    STORY_LIMIT = int(os.environ.get("STORY_LIMIT", 10))
except ValueError:
    logger.warning("Invalid STORY_LIMIT format. Defaulting to 10.")
    STORY_LIMIT = 10

# Hacker News API Endpoints (Firebase)
HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"

# Email / SMTP Configuration
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com") 
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER")           # Required: Login email
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")   # Required: App Password
SMTP_SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL") # Optional: 'From' header
PUBLISH_EMAIL = os.environ.get("PUBLISH_EMAIL")   # Required: Destination (Kindle)

def check_config():
    """
    Logs the current configuration to ensure environment variables are loaded.
    Masks passwords for security.
    """
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
    """
    Fetches the metadata for the top 'limit' stories from Hacker News.
    """
    logger.info(f"Connecting to Hacker News API to fetch top {limit} stories...")
    
    try:
        response = requests.get(HN_TOP_STORIES_URL, timeout=10)
        response.raise_for_status() 
        top_ids = response.json()[:limit]
        logger.info(f"Retrieved Top ID list: {top_ids}")
    except Exception as e:
        logger.error(f"Failed to fetch Top Stories list: {e}")
        return []

    stories = []
    for index, story_id in enumerate(top_ids):
        try:
            logger.info(f"Fetching details for item {story_id} ({index+1}/{limit})...")
            item_resp = requests.get(HN_ITEM_URL.format(story_id), timeout=5)
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
            
    logger.info(f"Successfully retrieved {len(stories)} valid stories.")
    return stories

def summarize_article(url, title):
    """
    Uses the configured LLM (via LiteLLM) to summarize the article context.
    """
    logger.info(f"🤖 Generating AI summary for: '{title}'...")
    
    prompt = f"""
    You are a helpful technical assistant to summarize the article inside a web page. 
    Task: Summarize the article located at the URL below.
    
    Article Title: {title}
    Article URL: {url}
    
    Requirements:
    1. Highlight the key messages and most important parts.
    2. **CRITICAL: Write the output strictly in {TARGET_LANGUAGE}.**
    """
    
    start_time = time.time()
    try:
        response = completion(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        
        summary_text = response.choices[0].message.content
        duration = time.time() - start_time
        logger.info(f"✅ Summary generated in {duration:.2f}s for '{title}'")
        return summary_text
        
    except Exception as e:
        logger.error(f"❌ LLM Generation failed for '{title}'. Error: {e}")
        return f"Could not generate summary. Error: {str(e)}"

def create_epub(stories):
    """
    Compiles the list of stories into an EPUB file saved in the OS temp folder.
    Returns the file path.
    """
    if not stories:
        logger.warning("No stories provided to create_epub. Aborting.")
        return None

    date_str = datetime.date.today().strftime("%Y-%m-%d")
    
    # Define Temp Directory and File Path
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
    
    # Intro
    intro = epub.EpubHtml(title='Cover', file_name='intro.xhtml', lang='en')
    intro.content = (
        f"<h1>Hacker News Daily Digest</h1>"
        f"<p><b>Date:</b> {date_str}</p>"
        f"<p><b>Language:</b> {TARGET_LANGUAGE}</p>"
        f"<p><b>Model:</b> {LLM_MODEL}</p>"
        f"<p><b>Stories:</b> {len(stories)}</p>"
    )
    book.add_item(intro)
    chapters.append(intro)

    # Stories
    for i, story in enumerate(stories):
        logger.info(f"Processing Story {i+1}/{len(stories)}: {story['title']}")
        
        summary = summarize_article(story['url'], story['title'])
        
        # Simple HTML formatting for the summary
        summary_html = summary.replace('\n- ', '<li>').replace('\n', '</li>')
        if '<li>' in summary_html: 
            summary_html = f"<ul>{summary_html}</li></ul>"
        else:
            summary_html = f"<p>{summary}</p>"
        
        content = f"""
            <h2>{story['title']}</h2>
            <p><b>Link:</b> <a href="{story['url']}">{story['url']}</a></p>
            <p><i>By: {story.get('by', 'Unknown')}</i></p>
            <hr/>
            <h3>Summary ({TARGET_LANGUAGE})</h3>
            {summary_html}
        """
        
        chapter = epub.EpubHtml(title=story['title'], file_name=f'chap_{i}.xhtml', lang='en')
        chapter.content = content
        book.add_item(chapter)
        chapters.append(chapter)
        
        # Small sleep to avoid hitting LLM rate limits too fast
        time.sleep(1)

    # Structure
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
    """
    Sends the generated EPUB file via email.
    Supports both SSL (Port 465) and STARTTLS (Port 587).
    """
    if not SMTP_USER or not SMTP_PASSWORD or not PUBLISH_EMAIL:
        logger.error("Missing email credentials. Cannot send email.")
        return

    logger.info(f"📧 Preparing to send email to {PUBLISH_EMAIL}...")
    
    msg = MIMEMultipart()
    sender = SMTP_SENDER_EMAIL if SMTP_SENDER_EMAIL else SMTP_USER
    
    msg['From'] = sender
    msg['To'] = PUBLISH_EMAIL
    msg['Subject'] = f"Hacker News Digest - {datetime.date.today()}"

    # Attach the EPUB file
    try:
        with open(file_path, "rb") as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
        
        encoders.encode_base64(part)
        part.add_header(
            'Content-Disposition',
            f'attachment; filename={os.path.basename(file_path)}',
        )
        msg.attach(part)
        logger.info("File attached successfully.")
    except Exception as e:
        logger.error(f"Failed to read or attach file: {e}")
        return

    # Connect to SMTP Server
    context = ssl.create_default_context()
    
    try:
        logger.info(f"Connecting to SMTP server {SMTP_HOST}:{SMTP_PORT}...")
        
        # LOGIC SWITCH: Handle Port 465 (Implicit SSL) vs 587 (STARTTLS)
        if SMTP_PORT == 465:
            # Implicit SSL
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                logger.info("Logging in (SSL)...")
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(sender, PUBLISH_EMAIL, msg.as_string())
        else:
            # STARTTLS (Port 587 or others)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                # server.set_debuglevel(1) # Uncomment to see deep network logs
                logger.info("Starting TLS...")
                server.starttls(context=context) # Upgrade the connection
                logger.info("Logging in (STARTTLS)...")
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(sender, PUBLISH_EMAIL, msg.as_string())
        
        logger.info("✅ Email sent successfully!")
    except Exception as e:
        logger.error(f"❌ Failed to send email: {e}")

# --- MAIN EXECUTION FLOW ---
if __name__ == "__main__":
    check_config()
    
    logger.info("🚀 Starting Daily Digest Automation")
    
    # 1. Get Stories
    top_stories = get_top_stories(STORY_LIMIT)
    
    if top_stories:
        # 2. Create EPUB (includes Summarization)
        epub_path = create_epub(top_stories)
        
        if epub_path:
            # 3. Send Email
            send_email(epub_path)
        else:
            logger.error("EPUB creation failed. Skipping email.")
    else:
        logger.warning("No stories found. Exiting.")

    logger.info("🏁 Run complete.")