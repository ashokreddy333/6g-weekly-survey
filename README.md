# 6G Weekly Survey

Automated literature survey agent. Every Monday morning, it pulls fresh
research from arXiv and vendor blogs (Qualcomm, Ericsson, Nokia, Huawei,
MediaTek), filters by your 6G topic interests, summarizes each item with
an LLM, and emails an HTML digest with a PDF attachment to your team.

## Architecture

```
   ┌───────────────┐  ┌────────────────┐  ┌──────────────┐
   │  arXiv API    │  │  Vendor RSS    │  │   Vendor     │
   │ (cs.IT, ...)  │  │  (Qualcomm,    │  │   HTML       │
   │               │  │   Ericsson)    │  │  (Nokia,...) │
   └──────┬────────┘  └────────┬───────┘  └──────┬───────┘
          └────────────┬───────┴─────────────────┘
                       ▼
              ┌────────────────────┐
              │  filter & rank     │  ← topics.yaml + seen-store
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐
              │   LLM summarizer   │  ← provider-agnostic
              │  (Claude/GPT/etc)  │
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐
              │  HTML + PDF builder│
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐
              │   Gmail SMTP send  │
              └────────────────────┘
```

## One-time setup

### 1. Create the repo on GitHub
- Create a new (private) repo.
- Push these files to it.

### 2. Generate credentials

**Anthropic API key** (or OpenAI/Google — see "Switching LLM providers" below):
1. Go to https://console.anthropic.com → Settings → API Keys → Create.
2. Add ~$5–10 credit (each weekly run costs cents).
3. Save the key.

**Gmail app password:**
1. Enable 2FA on the sending Google account.
2. Go to https://myaccount.google.com/apppasswords.
3. Generate one for "Mail". Save the 16-character password (no spaces).

### 3. Add GitHub secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name           | Value                                |
|-----------------------|--------------------------------------|
| `ANTHROPIC_API_KEY`   | Your Claude API key                  |
| `GMAIL_ADDRESS`       | The sending Gmail address            |
| `GMAIL_APP_PASSWORD`  | The 16-char app password             |

### 4. Edit the configs

| File | What to change |
|---|---|
| `config/recipients.yaml` | Your email and any teammates' emails; the sending Gmail address |
| `config/topics.yaml`     | Add/remove keywords or reweight topics |
| `config/sources.yaml`    | Enable IEEE Xplore once your API key is approved; add/remove RSS feeds |
| `config/config.yaml`     | Pick LLM provider, max papers per report, etc. |

### 5. Test locally (optional but recommended)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="abcdefghijklmnop"
python -m src.main
```

You should receive the email within a few minutes.

### 6. Enable the schedule

Once you push to GitHub, Actions will run automatically every Monday at
03:00 UTC. To trigger manually anytime: **Actions → Weekly 6G Survey → Run workflow**.

## Switching LLM providers

Edit `config/config.yaml`:

```yaml
llm:
  provider: openai      # or "google" or "anthropic"
  model: gpt-4o-mini    # or leave empty for provider default
```

Then:
1. Install the corresponding SDK in `requirements.txt`
   (`openai` for ChatGPT, `google-genai` for Gemini).
2. Add the matching API key as a GitHub secret
   (`OPENAI_API_KEY` or `GOOGLE_API_KEY`) and uncomment it in
   `.github/workflows/weekly-survey.yml`.
3. Commit and re-run.

## Adding a new source

**RSS feed:** add an entry under `rss_feeds:` or `vendor_sources:` in
`config/sources.yaml`. No code change needed.

**HTML page (vendor blog):** add under `vendor_sources` with
`mode: html` and CSS selectors. To find the selectors, open the page,
right-click an article card, **Inspect**, and copy the class name.

**New API (e.g. Semantic Scholar):** create
`src/fetchers/<name>_fetcher.py` following the pattern in
`arxiv_fetcher.py`, then add one line to `src/main.py`.

## Tuning relevance

The agent reports between zero and `max_items` papers per week.
If you get too few or too many:

- **Too few:** loosen keywords in `topics.yaml`, lower `min_score` in
  `config.yaml`, or widen `lookback_days` in `sources.yaml`.
- **Too many:** lower `max_items`, raise `min_score`, or remove broad
  keywords (e.g. drop "6G" alone in favor of more specific phrases).

## Maintenance

- **Vendor HTML selectors break occasionally.** When a vendor returns 0
  items in the logs, open their listing page in a browser and re-derive
  the CSS selectors.
- **IEEE Xplore API quota:** the fetcher is disabled by default. Enable
  it once your API key is approved at https://developer.ieee.org.
- **Seen-store grows over time.** Currently we never prune it; expect
  ~MB after a few years. To reset, delete `data/seen.sqlite`.

## Cost estimate

- **GitHub Actions:** free (under 100 minutes/month for public repos;
  2000 free minutes/month for private).
- **Claude Sonnet:** ~$0.02–0.05 per run.
- **GPT-4o-mini or Gemini Flash:** ~$0.005 per run.

## Repo layout

```
6g-weekly-survey/
├── .github/workflows/weekly-survey.yml   # cron + CI
├── config/
│   ├── config.yaml         # provider, knobs
│   ├── topics.yaml         # 6G topic keywords + weights
│   ├── sources.yaml        # arxiv, vendors, IEEE, RSS
│   └── recipients.yaml     # email list + sender
├── src/
│   ├── main.py             # orchestrator
│   ├── fetchers/
│   │   ├── arxiv_fetcher.py
│   │   └── vendor_fetcher.py
│   ├── filter.py           # dedup + topic match + ranking
│   ├── llm_client.py       # Claude / GPT / Gemini behind one interface
│   ├── summarizer.py       # produces structured per-paper JSON
│   ├── report.py           # HTML email body + PDF builder
│   └── mailer.py           # Gmail SMTP sender
├── data/
│   ├── seen.sqlite         # auto-managed: items already reported
│   └── reports/            # past PDFs, committed by CI
├── requirements.txt
└── README.md
```
