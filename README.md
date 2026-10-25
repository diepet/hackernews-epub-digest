# Hacker News EPUB Digest

Fetch top Hacker News stories, extract the articles, summarize them with a model supported by [LiteLLM](https://docs.litellm.ai/docs/providers), and email an EPUB to your reader.

Requires **Python 3.12–3.14** and [uv](https://docs.astral.sh/uv/). The repository selects Python 3.12 and includes a dependency lockfile.

## Run locally

```bash
uv sync --locked

# Fetch real HN stories and build an EPUB from source excerpts.
# No model credentials, AI charges, or outgoing email.
uv run --frozen python main.py --no-llm
```

The EPUB and `run-report.json` are written to `output/`. Use `--output-dir PATH` to choose another directory. Local editions accumulate until removed.

For AI summaries or delivery, copy `.env.example` to `.env` and fill in your settings. uv loads the file only when explicitly requested:

```bash
# Real AI summaries, without sending email (provider usage may be billed).
uv run --frozen --env-file .env python main.py --dry-run

# Generate summaries and email the EPUB.
uv run --frozen --env-file .env python main.py
```

`--no-llm` always disables email, including when email credentials are present. Its source excerpts keep their original language. `--dry-run` disables email but calls the configured AI provider.

## Configuration

Settings come from environment variables. Empty numeric settings use their defaults; invalid, negative, or out-of-range values fail before work starts.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_MODEL` | Required for AI | LiteLLM model identifier. The workflow defaults to `gemini/gemma-3-27b-it`; confirm availability and quota in your provider account. |
| `TARGET_LANGUAGE` | `English` | Language name or BCP 47 tag, such as `Italian`, `German`, or `en-US`. |
| `STORY_LIMIT` | `10` | Number of stories, from 1 to 50. |
| `SCRAPE_CHAR_LIMIT` | `60000` | Maximum source characters per story, from 300 to 500000. |
| `LLM_RPM_LIMIT` | `0` | Maximum outbound AI requests in a rolling 60-second window. Zero disables this local limit. |
| `LLM_TPM_LIMIT` | `0` | Token budget in a rolling 60-second window. Zero disables this local limit. |
| `LLM_MAX_OUTPUT_TOKENS` | `1800` | Maximum tokens generated per summary, from 100 to 32000. Also reserved when enforcing TPM. |
| `LLM_MAX_WAIT_SECONDS` | `300` | Maximum accepted provider cooldown, from 60 to 600 seconds. A longer requested wait stops the run. |
| `HTTP_TIMEOUT` | `20` | HTTP connect/read timeout in seconds; downloads also have a total time bound. |
| `LLM_TIMEOUT` | `90` | Timeout per model request in seconds. |
| `SMTP_TIMEOUT` | `30` | SMTP socket timeout in seconds. |
| `MAX_RETRIES` | `2` | Retry allowance per HTTP/AI request, from 0 to 5. AI retries throttling, connection errors, timeouts, and transient server errors. Retries also consume the local RPM/TPM budget. |
| `MAX_DOWNLOAD_BYTES` | `10000000` | Limit on downloaded and decompressed response bytes. |
| `PDF_PAGE_LIMIT` | `30` | Maximum PDF pages to extract. |
| `LOG_LEVEL` | `INFO` | Application log verbosity. Logs are structured JSON. |
| `ROOT_LOG_LEVEL` | `WARNING` | Library log verbosity. |

Supply the credential for the selected provider, for example `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`. Azure also requires `AZURE_API_KEY`, `AZURE_API_BASE`, and the API version supported by your deployment in `AZURE_API_VERSION`.

### AI throttling

Set the limits from your model and provider account. For example, for an account allowing 10 requests and 15000 tokens per minute:

```dotenv
LLM_RPM_LIMIT=10
LLM_TPM_LIMIT=15000
LLM_MAX_OUTPUT_TOKENS=1800
MAX_RETRIES=2
```

`LLM_REQUEST_DELAY` has been replaced by these limits. A local nonzero value produces a migration warning and no fixed delay; update the GitHub variables or your `.env` file.

The script sends requests immediately while there is room in both budgets and waits for older requests to leave the rolling window when needed. A token reservation includes **the complete prompt estimate plus 15% headroom, and the maximum output allowance**. [LiteLLM chooses a tokenizer using the model name and falls back to tiktoken for unsupported models](https://docs.litellm.ai/docs/completion/token_usage). Counts are therefore estimates, particularly for custom deployments and models without an exact tokenizer. If token counting fails, the script uses a conservative UTF-8 byte estimate.

Provider-reported token usage is recorded after successful responses and increases the reservation if it exceeds the estimate. Reservations are not reduced for short responses, because some providers charge the output allowance against their rate limit. This input-plus-output budget is deliberately conservative for providers that limit only input tokens.

If an article cannot fit in one minute's token budget, its source text is shortened before generation. If the prompt and output allowance leave too little room for source text, the run fails with a configuration error. `SCRAPE_CHAR_LIMIT` remains a separate ceiling on extracted source text; zero is still invalid.

On a provider rate-limit error, the script uses `Retry-After`, recognized reset headers, or Google's structured `RetryInfo` when LiteLLM preserves them. Without a usable hint it waits 60 seconds, then 120 seconds on a second retry. This recovery also works with both local limits set to zero. `MAX_RETRIES` bounds the attempts; `LLM_MAX_WAIT_SECONDS` prevents long provider cooldowns from hanging the job. Recognized daily quota and billing failures stop immediately. Automatic SDK retries are disabled so every attempt goes through the same limiter.

Limits are tracked within one running digest. They cannot account for other applications using the same provider quota or discover your account's limits automatically. Actual provider limits remain authoritative. The run report's `llm` section records outbound attempts, observed rate limits, wait time, and reported token usage. The Actions job still has a 30-minute overall timeout.

| Email variable | Default | Purpose |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP host. |
| `SMTP_PORT` | `465` | Implicit TLS on 465; STARTTLS on other ports, usually 587. |
| `SMTP_USER` | Required for delivery | SMTP login. |
| `SMTP_PASSWORD` | Required for delivery | SMTP password or provider app password. |
| `SMTP_SENDER_EMAIL` | `SMTP_USER` | Sender address. |
| `PUBLISH_EMAIL` | Required for delivery | One destination address. |

For Kindle, approve the sender in your Amazon account's personal document settings. SMTP acceptance confirms submission to the mail server; it does not confirm arrival in the inbox or on the device.

## Scraping and failure handling

- Trafilatura extracts article text, headings, lists, and tables, with a Beautiful Soup fallback and a GitHub README preference.
- Text PDFs and plain text are supported. Ask HN and other self posts use the HN API's post text, without mixing in discussion comments.
- TLS certificates are verified. Connections are reused; retries, response sizes, PDF pages, and timeouts are bounded. In environments using a custom certificate authority, set `REQUESTS_CA_BUNDLE` to the trusted CA bundle.
- Access challenges and unsupported binary content are rejected. Paywalls, browser-only pages, and scanned PDFs can remain unreadable; this scraper does not run JavaScript, bypass access controls, or perform OCR.
- A blocked article gets an explicit unavailable chapter and source/discussion links. A digest with at least one usable story can still be produced.
- No usable stories, model configuration errors, EPUB failures, or SMTP failures produce a nonzero exit status. Partial AI failures also return nonzero, even if the available summaries were delivered. Individual blocked sources are recorded as a partial run and do not fail an otherwise useful digest.
- `run-report.json` records counts, error stages, EPUB filename, and whether SMTP accepted the message. The EPUB is retained if delivery fails. AI error bodies and credentials are not included in the book or report.
- SMTP sends are not automatically retried because a connection failure after submission can otherwise produce duplicate mail.

## GitHub Actions

The daily workflow is [`.github/workflows/schedule.yaml`](.github/workflows/schedule.yaml). It runs at **03:17 UTC** (04:17 CET / 05:17 CEST). GitHub can delay scheduled runs.

1. Add provider and email credentials under **Settings → Secrets and variables → Actions → Secrets**.
2. Set optional settings such as `LLM_MODEL`, `TARGET_LANGUAGE`, and `STORY_LIMIT` under **Variables**. For Azure, set `AZURE_API_VERSION` as a variable and the endpoint/key as secrets.
3. Open **Actions → Daily HN Digest** and enable the workflow if disabled.
4. Use **Run workflow → scrape-only** to check sources without credentials. Use **dry-run** to check real AI summaries. Download the EPUB and report from the run artifact.
5. Use **deliver** for a real delivery check. Scheduled runs use delivery mode automatically. Enable GitHub's Actions failure notifications to receive alerts for failed runs.

Artifacts are retained for seven days, including on failed runs if files were generated. Runs have a 30-minute job limit, and concurrent delivery jobs are serialized. The separate checks workflow runs linting and offline regression tests without secrets.

### Why deliveries stopped

The maintenance review on **2026-10-25** found the deployed workflow in state **`disabled_inactivity`**, with the last scheduled run on **2026-03-06**. This explains the subsequent missing deliveries. The old script also caught delivery errors without failing the process, so historical green runs do not establish successful mail delivery.

[GitHub disables schedules in public repositories after 60 days without repository activity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Enabling the workflow restores the schedule, but this policy can stop it again. Check the workflow state when mail stops. For unattended operation independent of repository activity, run the same command from cron/systemd on an always-on machine or use an external scheduler. This project does not generate artificial commits to keep the repository active.

The GitHub account available during the review had read-only repository access, and provider/SMTP credentials were not available locally. The review could inspect workflow history and test retrieval/EPUB generation, but could not publish changes, enable the schedule, or confirm real AI/email delivery.

Initial validation on that date: **42 offline tests passed**; a live preview extracted **10 of 10** current top stories and generated an EPUB whose XHTML, navigation, and metadata parsed successfully. Separate live checks extracted an arXiv PDF, a plain-text document, and an Ask HN post. The current LiteLLM SDK was also exercised with a mock response; this does not establish provider authentication, quota, or live model availability.

After publishing the changes, an account with repository write access can restore the workflow and start an AI preview:

```bash
gh workflow enable schedule.yaml --repo diepet/hackernews-epub-digest
gh workflow run schedule.yaml --repo diepet/hackernews-epub-digest -f mode=dry-run
```

Inspect the preview artifact, then select `deliver` in the workflow UI to check actual delivery.

## Development

```bash
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
```

Offline tests use synthetic HTML/PDF data and mocked external services. Live retrieval is a separate check through `--no-llm`. Runtime modules are in `hn_digest/`; `main.py` is the command entry point.

To update dependencies, run `uv lock --upgrade`, `uv sync --locked`, and the checks above. GitHub Actions installs from the committed lockfile rather than selecting new library versions on every daily run.
