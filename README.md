# Hacker News EPUB Digest

A daily automation script that fetches the top stories from Hacker News, scrapes the article content, summarizes it using AI (via `LiteLLM`), compiles an EPUB "newspaper," and delivers it to your email (Kindle, phone, or tablet).

It runs automatically on GitHub Actions for **free** (or low cost depending on your AI provider).

## 🚀 Features

* **Automated Daily Delivery:** Runs every morning via GitHub Actions (cron schedule).
* **Smart Scraping:** Fetches full article text, bypassing common anti-bot protections (User-Agent rotation, Rich Headers).
* **AI Summaries:** Uses LLMs (Gemini, OpenAI, Claude, etc.) to generate concise, technical summaries.
* **Markdown Support:** Renders bold text, lists, and proper formatting in the final EPUB.
* **Cost & Quota Management:**
    * `LLM_REQUEST_DELAY`: Prevents hitting API rate limits (429 errors).
    * `SCRAPE_CHAR_LIMIT`: Truncates long articles to save tokens and costs.
* **Customizable Logging:** Fine-tune verbosity using `LOG_LEVEL` variables for easier debugging.
* **Multi-Language Support:** Can translate and summarize content in **Italian**, Spanish, French, etc.
* **Secure Email:** Supports standard SMTP with SSL (465) or STARTTLS (587).

## 🛠️ Configuration

This project is configured entirely via **Environment Variables**.
If running on GitHub, add these to **Settings -> Secrets and variables -> Actions**.

### 1. General Settings

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | *Required* | The LLM provider and model by using the LiteLLM syntax (e.g., `gemini/gemini-3-flash`, `openai/gpt-4o`). [Click here](https://docs.litellm.ai/docs/providers) for the full list. |
| `PUBLISH_EMAIL` | *Required* | Destination email address (e.g., `yourname@kindle.com`). |
| `TARGET_LANGUAGE` | `English` | The language for the output summaries (e.g., `Italian`, `German`). |
| `STORY_LIMIT` | `10` | Number of top stories to process per run. |
| `LLM_REQUEST_DELAY` | `0` | Seconds to sleep between articles (e.g., `5` or `10`). |
| `SCRAPE_CHAR_LIMIT` | `60000` | Max characters to send to the LLM per article. |
| `LOG_LEVEL` | `INFO` | Logging level for the main script (e.g., `DEBUG` for verbose output). |
| `ROOT_LOG_LEVEL` | `INFO` | Logging level for external libraries (requests, urllib3, etc.). |

### 2. SMTP / Email Configuration

Configure your email provider (Gmail, Outlook, AWS SES, etc.).

| Variable | Example Value | Description |
| :--- | :--- | :--- |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP Server address. |
| `SMTP_PORT` | `465` | Port number (usually `465` for SSL or `587` for STARTTLS). |
| `SMTP_USER` | `you@gmail.com` | SMTP Username/Email. |
| `SMTP_PASSWORD` | `abcd 1234 ...` | SMTP Password (use an **App Password** for Gmail). |
| `SMTP_SENDER_EMAIL`| `bot@my-domain.com` | (Optional) Custom "From" address. Defaults to `SMTP_USER`. |

### 3. AI Provider Configuration (LiteLLM)

This project uses [LiteLLM](https://docs.litellm.ai/), so it supports 100+ models. Choose **one** provider below.

#### 🟢 Option A: Google Gemini (Free Tier Available)
| Variable | Value | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `gemini/gemini-1.5-flash` | Prefix with `gemini/`. |
| `GEMINI_API_KEY` | `AIzaSy...` | Your Google AI Studio API Key. |

#### 🔵 Option B: OpenAI
| Variable | Value | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `gpt-4o` | Standard OpenAI model name. |
| `OPENAI_API_KEY` | `sk-proj...` | Your OpenAI API Key. |

#### 🏢 Option C: Azure OpenAI
| Variable | Value | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `azure/my-deployment` | Prefix with `azure/` + your **Deployment Name**. |
| `AZURE_API_KEY` | `...` | Your Azure API Key. |
| `AZURE_API_BASE` | `https://my-resource.openai.azure.com/` | Your Azure Endpoint. |
| `AZURE_API_VERSION` | `2024-02-15-preview` | API Version. |

---

## 📦 How to Run Locally

1.  **Clone the repo**
    ```bash
    git clone [https://github.com/YOUR_USERNAME/hackernews-epub-digest.git](https://github.com/YOUR_USERNAME/hackernews-epub-digest.git)
    cd hackernews-epub-digest
    ```

2.  **Install dependencies** (using [uv](https://github.com/astral-sh/uv))
    ```bash
    uv sync
    ```

3.  **Set environment variables** (Linux/Mac example)
    ```bash
    # AI
    export GEMINI_API_KEY="your_key"
    export LLM_MODEL="gemini/gemini-1.5-flash"
    
    # Email
    export SMTP_HOST="smtp.gmail.com"
    export SMTP_PORT="465"
    export SMTP_USER="your@gmail.com"
    export SMTP_PASSWORD="your_app_password"
    export PUBLISH_EMAIL="kindle@kindle.com"
    
    # Tuning and Logging
    export STORY_LIMIT="5"
    export LOG_LEVEL="DEBUG"
    ```

4.  **Run the script**
    ```bash
    uv run python main.py
    ```

## ☁️ Deployment (GitHub Actions)

This repository includes a `.github/workflows/schedule.yml` that runs automatically with the needed environment variables for Gemini.

1.  **Fork** this repository.
2.  Go to **Settings** -> **Secrets and variables** -> **Actions**.
3.  Add the secrets listed in the **Configuration** section.
4.  Go to the **Actions** tab and enable workflows.
5.  The script will run daily at **06:00 CET** (05:00 UTC).

## 📄 License
MIT