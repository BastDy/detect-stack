# detect-stack

Signal-based account prioritization for B2B prospecting, in plain Python (standard library only, nothing to install).

It pulls Canadian companies from public sources, reads their websites and job boards, and ranks them **A / B / C** with a suggested outreach angle, for example:

> *Hiring "Manager, Revenue Operations Systems": interim RevOps while they recruit · 6 open sales/marketing roles: set up process and reporting before the new hires land*

## How it works

```
 SOURCES                      SIGNALS                          OUTPUT
 BetaKit funding (12 mo) ─┐   Website stack ── HubSpot, Salesforce,   ┐
 Y Combinator (Canada)  ──┼─► merge by ─►     Pipedrive, Zoho, Calendly…├─► score ─► tier A/B/C
 Your exports (Apollo…) ──┘   domain   Job boards ── Greenhouse, Lever, │    + angle
                                       │      Ashby, Workable           │    + LinkedIn page
 Job-search tracker ── exclusions ─────┘   Funding date, size, region ─┘    → CSV + summary
```

| Data | Where it comes from | Type |
|---|---|---|
| Funding rounds | BetaKit WordPress API | 3rd party |
| YC companies in Canada | yc-oss public API | 3rd party |
| Company lists | Apollo / Clay exports dropped in `data/inputs/` | 3rd party |
| Tech stack | Company website HTML, `/contact`, `/demo`, `/pricing`, and the Google Tag Manager container | 3rd party (technographic) |
| Open roles | Public ATS APIs (Greenhouse, Lever, Ashby, Workable) | 3rd party (hiring intent) |
| Companies already applied to | Local job-search tracker | 1st party |

## Scoring grid

The score has two parts, shown in the `score_profil` and `score_timing` columns:

- **Profile**: what the company is. It rarely changes.
- **Timing**: what is happening there now. A dated signal loses half its points every *half-life* days (rounded), then drops out.

| Part | Signal | Points | Half-life |
|---|---|---|---|
| Profile | SaaS or tech | +3 | |
| Profile | HubSpot **and** another CRM on the site (consolidation) | +3 | |
| Profile | 20–200 employees | +2 | |
| Profile | Uses HubSpot | +2 | |
| Profile | Ontario (+1 elsewhere in Canada) | +2 | |
| Timing | Open RevOps / Sales Ops / CRM role | +5 | (current state) |
| Timing | 3+ open sales/marketing roles | +2 | (current state) |
| Timing | Funding round (last 12 months) | +3 | 180 days |
| Timing* | Sales / marketing / revenue leadership hire (`leadership_hire`) | +3 | 60 days |
| Timing* | A CRM installed on the site: HubSpot, Salesforce, Pipedrive, Zoho, Marketo (`tool_added`) | +3 | 45 days |
| Timing* | A posting asks for another CRM than the site's (`posting_crm_mismatch`) | +3 | 90 days |
| Timing* | Several HubSpot portals, RevOps role unfilled 60+ days, first hire, process to build | +2 each | 90 days |
| Timing* | Sales hiring spike (`hiring_spike`) | +2 | 30 days |

\* Read from the history events (`timing.py`), so only in `pipeline.py`. Each event type counts once per company (the most recent one); events older than 180 days are ignored. Angles drawn from these events come before the others in `angle_suggere`, because something that just happened is the best reason to get in touch.

**A** ≥ 12 **with at least 1 Timing point** · **B** 8–11 (or ≥ 12 with no Timing) · **C** < 8. A good profile with nothing moving is not a priority. Within a tier, accounts are sorted by score, then by Timing. Companies with fewer than 10 employees, or already in the job-search tracker, are excluded.

Events from `--offres` (`posting_*`) are produced after scoring, so they count from the next run.

## Run it

```bash
python pipeline.py                 # everything: sources → score → CSV (about 2 minutes for ~300 companies)
python pipeline.py --limit 25      # quick test on 25 companies
python pipeline.py --no-betakit --no-yc   # only the exports in data/inputs/
python pipeline.py --refresh       # re-read every site and job board, even if not due
python pipeline.py --offres        # also have Claude read the full text of tier A/B sales & RevOps postings (paid, see below)
python qualify_agent.py --dry-run  # agent: qualify A/B accounts for the AI-ready CRM offer; web search only when unsure (paid)
python -m unittest discover -s tests      # tests (no network)
```

Results land in `data/output/`:

- `prospects-latest.csv`: the ranked list. Add your own columns (Notes, Status…), they are kept on the next run.
- `prospects-YYYY-MM-DD.csv`: one snapshot per run. The `nouveau` column flags companies that are new or moved up a tier since the previous run.
- `resume-YYYY-MM-DD.md`: counts, what changed since the last run, top 20, warnings.
- `signaux_recents` column: the last 3 changes of the past 30 days (tool installed, role opened, funding, tier move…).

The scripts also work on their own: `detect_stack.py acme.com`, `score_prospects.py export.csv`, `find_linkedin.py --file list.csv`, `betakit_funding.py`, `yc_companies.py`.

## Configuration

The job-search tracker is read from `../ai-job-search/job_search_tracker.csv` by default. To point elsewhere, copy `config.example.json` to `config.json` and set `job_tracker`, or pass `--tracker`. If the file is missing, the run says so loudly instead of silently skipping the exclusions.

The scoring grid can be tuned in the same file under `scoring`, without touching the code: `points` (`saas`, `size`, `hubspot`, `ontario`, `canada`, `revops_job`, `funding`, `sales_jobs`, `other_crm`, and per event: `leadership_hire`, `crm_installed`, `second_portal`, `hiring_spike`, `posting_stale`, `posting_crm_mismatch`, `posting_first_hire`, `posting_build`), `decay` (half-life in days, same event keys plus `funding`), `size` (`min`, `max`, `exclude_below`), `signals` (`sales_jobs_min`, `funding_days`, `event_days`) and `tiers` (`A`, `B`, `A_timing_min`; set it to 0 to drop the "A needs Timing" rule). Only the keys you set override the defaults (the grid above); an unknown key stops the run with an explicit error, to catch typos.

## History (data/history.db)

Every pipeline run is recorded in a local SQLite database, `data/history.db` (standard library `sqlite3`):

- **What is stored**: companies and funding rounds, every website read (stack, HubSpot portals, errors), every job-board read and each posting (with its real ATS id), every score, and the **events** derived from changes between two reads: tool installed / removed, second HubSpot portal, role opened / closed / unfilled for 60+ days, leadership hire, hiring spike, funding, tier up / down.
- **Re-read frequency**: tier A and B accounts are re-read **every day** (site and job board); others every 30 days (site) and 7 days (jobs); a failed read is retried the next day. Accounts that are not due are scored from their last read in the database. `--refresh` re-reads everything; `--db PATH` uses another database.
- **No false alarms**: a failed read never changes anything, and a tool or posting is only declared gone after two successful reads without it.
- The old JSONL site cache is imported once on the first run (no events); the pipeline no longer uses the detection / job caches (`score_prospects.py` on its own still does).
- Tables, event types and ready-made queries: see [SCHEMA.md](SCHEMA.md).
- Like everything under `data/`, the database is local only and never committed (`*.db` is gitignored too).

## Job postings read by Claude (posting_signals.py)

A job title says little. The full posting says which CRM the team runs, whether this is the first sales hire, and whether the person must build the process from scratch. `posting_signals.py` reads the full text of every open sales, RevOps and leadership posting (public Greenhouse, Lever, Ashby and Workable APIs) and asks Claude for a fixed JSON answer: CRM in use, other GTM tools, first hire, build from scratch, team growth, reporting line, target markets, the problems the hire must solve, and an **exact quote for each signal**.

The code, not the model, then turns that answer into events: `posting_crm_mismatch` (the posting asks for Salesforce while the site runs HubSpot), `posting_first_hire`, `posting_build`. They show up in `signaux_recents` and in the summary, and the CSV gets a `signaux_offres` column per company.

```bash
pip install anthropic                          # the only extra dependency; key from ANTHROPIC_API_KEY or `ant auth login`
python posting_signals.py --dry-run            # how many postings are pending, estimated cost, no API call
python posting_signals.py --domain acme.com    # one account, prints each JSON answer (to test the prompt)
python posting_signals.py                      # tier A and B accounts (--tiers all for everyone)
```

- **Each posting is read once** per prompt version (`PROMPT_VERSION`): changing the prompt and bumping the version re-reads everything, which makes prompt iterations comparable. A failed read is retried the next day.
- **Cost is tracked**: tokens per call are stored in `posting_analyses`, and each run prints what it spent. Reading the 75 open postings of tier A/B accounts is estimated at about $2.50 with `claude-opus-5-5`, once; after that only new postings are read. `--model claude-haiku-5-5` is about 30x cheaper.
- **The posting is treated as data**, not instructions, and a signal without a quote from the text is not kept.
- An invalid key, missing rights or an unknown model stops the run at the first call instead of failing on every posting.

## Reliability choices

- **Reads expire**: in the pipeline, sites and job boards follow the history schedule above; `score_prospects.py` alone caches the site stack 30 days and job postings 3 days; LinkedIn is cached 90 days. An unreachable site is retried on the next run instead of staying "error" forever.
- **One failure never stops the run**: a crashing site is logged as an error, a source that is down falls back to its last saved version.
- **Guessed job boards are verified**: a guessed slug like `greenhouse.io/bolster` only counts if the board mentions the company's domain or name.
- **Network retries** on timeouts, 429 and 5xx; no retry on dead domains.
- **Private data stays local**: everything under `data/` and `config.json` is gitignored.

## Layout

```
pipeline.py         one command for the whole flow
score_prospects.py  scoring grid, exclusions, job-board lookup
detect_stack.py     website stack detection
find_linkedin.py    company LinkedIn page
betakit_funding.py  Canadian funding rounds
yc_companies.py     Canadian YC companies
common.py           HTTP, domains, cache with expiry, job-title categories, paths
posting_signals.py  full job postings read by Claude -> buying signals and events
timing.py           Timing part of the score: history events weighted by age (half-life)
db.py               SQLite history (data/history.db) and change events, see SCHEMA.md
tests/              unit tests (no network)
data/               inputs, sources, cache, output (local only)
```

## Next

Phase 2: an AI research step on tier A and B accounts, where Claude reads the site and open roles and returns a structured brief (pain points, RevOps maturity, trigger, first-line message).
