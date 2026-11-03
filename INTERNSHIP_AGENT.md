# Finance internship agent

A loop agent that finds finance firms inside a market range you define, scores
them against your criteria, writes a tailored application for each one, and
sends 30+ applications a day from your Gmail, with your CV attached.

```
           ┌──────────────── every few minutes ────────────────┐
           ▼                                                   │
  check replies ─► follow-ups due? ─► target reached? ── yes ──► sleep until tomorrow
                                           │ no
                                           ▼
                        pipeline < buffer? ─► DISCOVER (Claude + web search, next region)
                                           │                     │
                                           │              SCORE (criteria.yaml, plain code)
                                           ▼
                 best qualified firm whose office is open now
                                           │
                       WRITE letter (Claude) ─► SEND / DRAFT / PREVIEW ─► wait 4–9 min
```

## What each part does

| Step | File | How |
|---|---|---|
| Discover | `internship_agent/discovery.py` | Claude searches the web one region at a time (France → UK → Switzerland/Lux → … → Asia-Pacific) and returns firms in your target segments, each with an email address **it read on a public page** plus that page's URL. |
| Score | `internship_agent/scoring.py` | Plain code, no AI. A firm is rejected if it is in an excluded segment, the email domain doesn't match the firm's website, the address is a personal or no-reply address, or there is no source URL. The rest get a score out of 100 (segment, size, internship signal, country, contact quality, language). Only firms at `min_score` or above get contacted. |
| Write | `internship_agent/writer.py` | One letter per firm, 120–180 words, written in the local language if you speak it (otherwise English). It opens with a verified fact about the firm and only uses the highlights you put in `criteria.yaml`. Letters with placeholders are thrown out and rewritten. |
| Deliver | `internship_agent/mailer.py` | Gmail API: sends the letter with your CV attached, or saves it as a draft. Follow-ups go in the same email thread. It also spots replies and bounces. |
| Loop | `internship_agent/agent.py` | Sends only during the recipient's local office hours on weekdays, waits a random 4–9 minutes between emails, stops at `daily_target`, never goes over `hard_daily_cap` (follow-ups count toward it), and sends at most one follow-up per firm, after 8 days. |
| State | `internship_agent/data/pipeline.sqlite3` | Every firm found, its score, why it was rejected, every email sent. No firm is contacted twice. |

## Setup (about 15 minutes)

1. **Install**
   ```bash
   pip install -r requirements.txt
   ```
2. **Claude API key.** Get one at console.anthropic.com, then:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
3. **Gmail.** Follow `SETUP_GOOGLE_API.md` to get `credentials.json`. The agent keeps its own token
   (`token_internship.json`) with send, draft and read scopes, so the existing `relance.py` keeps working.
   On first run a browser window asks you to authorise it.
4. **Your CV.** Put the PDF at `internship_agent/cv/CV.pdf`. This folder is gitignored.
5. **Your criteria.** Edit `internship_agent/criteria.yaml`:
   - `candidate`: name, school, dates, languages, signature, and above all **`highlights`**. The writer
     will never claim anything that isn't listed there.
   - `market`: the segments, size range, regions and priority countries the agent focuses on.
   - `scoring`: weights and `min_score`.
   - `schedule`: `daily_target` (35 by default), caps, gaps, office hours.

## Running it

Start in **`dry_run`** mode (the default). It finds and scores real firms and writes real letters, but
sends nothing. Letters are saved to `logs/letters/<date>/<domain>.txt`.

```bash
python -m internship_agent run --limit 10
```

Read those letters. When you're happy with them, set `engine.mode` in `criteria.yaml`:

- `draft`: the agent fills your Gmail Drafts folder and you click send. Use this for the first days.
- `send`: fully automatic.

Then re-queue the firms you previewed and start the loop:

```bash
python -m internship_agent reset-previews
```

```bash
python -m internship_agent run
```

`run` keeps going day after day, so leave it running on a machine that stays on (a small VPS, or
`tmux`/`screen`). If you'd rather use cron, `run --once` does one day's batch and exits:

```bash
crontab -e   # then add:  5 8 * * 1-5 cd /path/to/altavia-cr-ation- && python -m internship_agent run --once
```

Other commands:

```bash
python -m internship_agent status
```

```bash
python -m internship_agent discover --region "United Kingdom"
```

```bash
python -m internship_agent seed my_companies.csv
```

`seed` imports a list you built yourself (from a school alumni directory, a fair, LinkedIn), scored
with the same rules. See `internship_agent/seed_companies.example.csv` for the columns.

## Running unattended (24/7, no intervention)

Once it's in `send` mode, `python -m internship_agent run` needs nothing from you. It loops every day,
and if something fails (an API outage, the network dropping) it waits 5 min, 10 min, … up to 1 h and
tries again. Four things to set up once:

1. **A machine that never sleeps.** A laptop that closes stops the agent. Use a small Linux VPS
   (any provider's cheapest plan is enough) or a Raspberry Pi. Copy the repo, `credentials.json`,
   `token_internship.json` and your CV there.
2. **Auto-restart.** `deploy/internship-agent.service` is a systemd service that starts the agent on
   boot and restarts it if it crashes. The install steps are at the top of the file.
3. **Gmail login that doesn't expire.** In Google Cloud Console → *OAuth consent screen*, click
   **Publish app** (status "In production"). While the app is in "Testing", Google expires the
   login every **7 days** and the agent stops sending until you log in again. For personal use you
   don't need Google's verification; you'll just see an "unverified app" warning once.
   Do the first login on your own computer (it opens a browser), then copy `token_internship.json`
   to the server.
4. **A spending limit.** Set a monthly limit in the Anthropic Console (Settings → Limits), so the
   bill can't grow without you knowing.

Replies arrive in your normal Gmail inbox. Check `python -m internship_agent status` or the logs
(`journalctl -u internship-agent -f`) from time to time.

## Safeguards and why they matter

- **No guessed emails.** Discovery is told never to build `firstname.lastname@` addresses, and scoring
  rejects any address without a source URL or whose domain doesn't match the firm's website. Guessed
  addresses bounce, and bounces get your Gmail account flagged.
- **Recruiting inboxes first.** `careers@`, `recrutement@`, `hr@` score highest. Named people are
  turned off by default (`allow_named_contacts: false`).
- **Volume caps.** 35 personalised emails a day spread over office hours is well under Gmail's limits
  and looks like a person sending, not a bulk mailer. Raising `hard_daily_cap` a lot is what gets
  accounts suspended.
- **Every letter is different and grounded in facts.** That's both what gets replies and what keeps
  you out of spam folders.
- **Refusal fallback.** If a request is declined, the API retries it on a fallback model
  (`refusal_fallbacks: true`, using the `server-side-fallback-2026-07-01` beta). Set it to `false` to turn it off.

## Cost

Each discovery round is one Claude Opus 5.5 call with up to 25 web searches, and each letter is one
short call. Expect a few dollars a day at 35 applications. Check the Usage page in the Anthropic
Console after the first day. To spend less, lower `discovery_effort` / `writing_effort` to `low`, or
set `engine.model` to `claude-sonnet-5-5`.

## Tests

```bash
python -m unittest discover tests
```

The tests use a fake Claude and a fake Gmail. They check that the agent reaches 35 sends with no
duplicates, stops when discovery finds nothing, counts follow-ups toward the hard cap, never follows
up after a reply, sends nothing in dry-run mode, and that the scoring and office-hours rules work.
