# Hacker News EPUB Digest

A daily automation script that fetches the top stories from Hacker News, summarizes them using AI, creates an EPUB "newspaper," and delivers it to your Kindle (or any ebook reader) via email.

It runs automatically on GitHub Actions for **free**.

## 🚀 Features

* **Automated Daily Delivery:** Runs every morning at 6:00 AM CET.
* **AI Summaries:** Extracts the core value of articles using Large Language Models.
* **Model Agnostic:** Works with OpenAI, Azure, Google Gemini, Anthropic, and more via `LiteLLM`.
* **Multi-Language Support:** Can translate and summarize content in **Italian**, Spanish, French, etc.
* **Kindle Compatible:** Generates a standard `.epub` file properly formatted for e-readers.
* **Zero Cost:** Designed to run on the GitHub Actions free tier.

## 🛠️ Configuration

This project uses `LiteLLM` as a bridge, allowing you to switch AI providers just by changing environment variables.

### 1. General Settings (All Providers)

These variables control the behavior of the script and email delivery.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TARGET_LANGUAGE` | `English` | The language for the summaries (e.g., `Italian`, `German`). |
| `STORY_LIMIT` | `10` | The number of stories to fetch and summarize (e.g., `5`, `20`). |
| `SMTP_HOST` | `smtp.gmail.com` | Your email provider's SMTP server. |
| `SMTP_PORT` | `465` | Your email provider's SSL port. |
| `SMTP_USER` | *Required* | The email address used to login (e.g., `you@gmail.com`). |
| `SMTP_PASSWORD` | *Required* | App Password for the sender email (NOT your login password). |
| `SMTP_SENDER_EMAIL`| *Optional* | The 'From' address (usually same as `SMTP_USER`). |
| `PUBLISH_EMAIL` | *Required* | The destination email (e.g., `you@kindle.com`). |

### 2. AI Provider Configuration

Choose **one** of the following providers and set the corresponding secrets/variables.

#### 🟢 Option A: Google Gemini (Free Tier Available)
| Variable | Value Example | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `gemini/gemini-1.5-flash` | Must start with `gemini/`. |
| `GEMINI_API_KEY` | `AIzaSy...` | Your Google AI Studio API Key. |

#### 🔵 Option B: OpenAI (Standard)
| Variable | Value Example | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `gpt-4o` | The model name (e.g., `gpt-4o`, `gpt-3.5-turbo`). |
| `OPENAI_API_KEY` | `sk-proj...` | Your OpenAI API Key. |

#### 🏢 Option C: Azure OpenAI (Enterprise)
| Variable | Value Example | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `azure/my-gpt4-deployment` | Must start with `azure/` followed by your **deployment name**. |
| `AZURE_API_KEY` | `a1b2c3d4...` | Your Azure resource API Key. |
| `AZURE_API_BASE` | `https://my-org.openai.azure.com/` | Your Azure Endpoint URL. |
| `AZURE_API_VERSION` | `2024-02-15-preview` | The API version targeted. |

#### 🟠 Option D: Anthropic (Claude)
| Variable | Value Example | Description |
| :--- | :--- | :--- |
| `LLM_MODEL` | `anthropic/claude-3-haiku-20240307` | Must start with `anthropic/`. |
| `ANTHROPIC_API_KEY` | `sk-ant...` | Your Anthropic API Key. |

---

## 📦 How to Run Locally

1.  **Clone the repo**
    ```bash
    git clone [https://github.com/YOUR_USERNAME/hackernews-epub-digest.git](https://github.com/YOUR_USERNAME/hackernews-epub-digest.git)
    cd hackernews-epub-digest
    ```

2.  **Install dependencies** (using uv)
    ```bash
    uv sync
    ```

3.  **Set environment variables** (Linux/Mac example for Gemini)
    ```bash
    export GEMINI_API_KEY="your_key_here"
    export SMTP_USER="your_email@gmail.com"
    export SMTP_PASSWORD="your_app_password"
    export PUBLISH_EMAIL="your_kindle@kindle.com"
    export TARGET_LANGUAGE="Italian"
    export STORY_LIMIT="5"
    ```

4.  **Run the script**
    ```bash
    uv run python main.py
    ```

## ☁️ Deployment (GitHub Actions)

This repository includes a `.github/workflows/schedule.yml` file that runs the script automatically.

1.  **Fork** this repository.
2.  Go to **Settings** -> **Secrets and variables** -> **Actions**.
3.  Add the **Secrets** matching the variables above (e.g., `GEMINI_API_KEY`, `SMTP_PASSWORD`, `SMTP_USER`, `PUBLISH_EMAIL`).
4.  Go to the **Actions** tab in GitHub and enable workflows.
5.  The script will run automatically every day at 05:00 UTC (06:00 CET).

## 📄 License
MIT