# Quant Research Digest (no-LLM version)

Self-hosted weekly aggregator. Collects from RePEc NEP (12 reports), NBER,
arXiv (9 q-fin categories), Crossref (18 journals), OpenAlex (SSRN probe),
Semantic Scholar, and Quantocracy; dedupes in SQLite; emails one HTML digest
grouped by section and source; commits a permanent archive to `reports/`.

This version has NO LLM layer: no API key, no cost, but also no triage — the
email is the full deduped feed (~100-150 items/week), and asset-manager
house-research pages (AQR, Man, etc.) are not covered, since reading their
JS-heavy sites reliably requires the web-search-tool approach that was removed
with the LLM layer. Quantocracy carries part of the practitioner voice.

## Architecture

GitHub Actions (Wed 08:00 IST) -> collectors (sources.py) -> dedup (store.py,
state.db: DOI -> arXiv id -> normalized-title hash) -> HTML email (emailer.py)
-> commit state.db + reports/ back to the repo.

## Setup

1. Create a GitHub repo (private is fine). Push these files, preserving
   `.github/workflows/digest.yml`.
2. Secrets (Settings -> Secrets and variables -> Actions):
   GMAIL_ADDRESS, GMAIL_APP_PASSWORD (requires 2-Step Verification),
   DIGEST_RECIPIENT (optional; defaults to GMAIL_ADDRESS).
3. Actions tab -> Weekly Research Digest -> Run workflow (manual first run).

## Run-1 verify list

- NBER feed URL in config.py (flagged inline)
- Semantic Scholar date parameter (flagged inline in sources.py)
- OpenAlex SSRN source id -- logged by the run; then hardcode in config.py
- Crossref title->ISSN resolutions -- logged; verify once, promote into
  config.JOURNAL_ISSNS to stop re-resolving

## Notes

- Cron is UTC (02:30 UTC = 08:00 IST Wednesday).
- The weekly state commit keeps the repo active, so GitHub's ~60-day
  scheduled-workflow pause never triggers.
- SSRN coverage via OpenAlex/S2 is partial and lagged; keep a free SSRN
  eLibrary email alert running in parallel for the leading edge.
- Google Scholar is deliberately excluded: no API, blocks automation, adds
  nothing beyond the DOI aggregators.
- `reports/` accumulates every digest as HTML — a searchable archive.
- Re-adding the LLM layer later (triage, highlight summaries, firm pages via
  the Anthropic web_search tool) is a self-contained change: one llm.py
  module, two calls in main.py, one secret, one line in the workflow.
