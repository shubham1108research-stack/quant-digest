"""Central configuration for the quant research digest."""

LOOKBACK_DAYS = 30       # every-3-days cron with a 1-month window; cross-run
                         # dedup drops the heavy overlap so emails stay net-new.

# Abstract enrichment (sources.enrich_abstracts): after the reliable OpenAlex
# lookup, at most this many journal article pages are scraped for a missing
# abstract, so a slow/blocked publisher can't stall the run.
ENRICH_SCRAPE_CAP = 60

# ---- RePEc NEP -------------------------------------------------------
NEP_CODES = ["fmk", "inv", "rmg", "ecm", "ets", "for",
             "cfn", "ino", "mst", "big", "cmp", "ain"]
NEP_URL = "https://nep.repec.org/rss/nep-{code}.rss.xml"

# ---- NBER ------------------------------------------------------------
# NBER working papers via the site's paginated JSON listing API, filtered by
# publication date AND by NBER PROGRAM. This REPLACES the old rolling new.xml
# RSS feed, which only exposed whatever was on the feed at fetch time -- so
# finance papers that scrolled off between runs were missed permanently. The
# date-window API returns EVERY paper for the window (paginated); the program
# facet then restricts to NBER's own finance programs -- an authoritative
# classification, far cleaner than a keyword guess (it also catches papers a
# keyword gate misses, e.g. a methods paper tagged Asset Pricing whose title
# has no obvious finance term). Final relevance is still the Bayesian posterior.
NBER_API = ("https://www.nber.org/api/v1/working_page_listing/contentType/"
            "working_paper/_/_/search")
NBER_PER_PAGE = 100
NBER_MAX_PAGES = 6                    # safety cap per program (~600 papers)
NBER_PROGRAMS = [                     # NBER program facet names -> finance
    "Asset Pricing",                  # (Asset Pricing only for now, per request)
]
# fallback keyword gate, used ONLY if the program query fails for some reason
NBER_FINANCE_TERMS = [
    "asset pric", "stock return", "equity", "portfolio", "factor",
    "cross-section", "cross section", "volatility", "option", "derivative",
    "hedge", "mutual fund", "risk premi", "expected return", "momentum",
    "market microstructure", "liquidity", "bond", "yield", "credit spread",
    "term structure", "exchange rate", "currency", "commodity", "futures",
    "capm", "arbitrage", "sharpe", "anomal", "return predictab", "trading",
    "financial market", "securities", "valuation", "investor", "beta",
]

# ---- Watchlist deep author pull (Crossref) --------------------------
# OpenAlex lags weeks-to-months on fresh working papers, so the OpenAlex
# watchlist pull alone misses recent-but-not-brand-new papers by watched
# authors (e.g. Kelly's "Artificial Intelligence Asset Pricing Models", on
# SSRN/NBER long before OpenAlex indexed it). A Crossref author query -- which
# covers SSRN + journals -- fills that gap, filtered to the author's own
# (first,last) name key + a finance gate to strip the common-name noise.
# The 640-day back-catalog has been SEEDED into the archive already, so ongoing
# runs only need a recent window to catch NEW papers -- much smaller/faster, no
# data lost (older watched papers are already archived, and the cross-match
# catches any watched author's paper arriving via a normal source immediately).
WATCHLIST_CROSSREF_DAYS = 90         # ongoing window (was 640 for the one-time seed)
WATCHLIST_CROSSREF_ROWS = 40         # Crossref rows fetched per author (relevance-sorted)

# ---- arXiv (Atom API, no key) ---------------------------------------
ARXIV_CATS = ["q-fin.PR", "q-fin.PM", "q-fin.ST", "q-fin.GN", "q-fin.EC",
              "q-fin.RM", "q-fin.CP", "q-fin.TR", "q-fin.MF"]
ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_MAX = 200
ARXIV_MAX_RETRIES = 4        # arXiv rate-limits (429) by IP; retry w/ backoff
# Per-category RSS fallback used when the bulk API 429s -- lighter endpoint,
# far less throttled (empties on weekends; arXiv doesn't announce Sat/Sun).
ARXIV_RSS = "https://rss.arxiv.org/rss/{cat}"

# ---- Journals via Crossref (tiered) ---------------------------------
# Finance-only, keyed to NYU Stern Finance Dept's top-tier journals list
# (2020-12-10). All ISSNs verified against Crossref by ISSN. Economics
# (Econometrica, JPE, J of Econometrics, AER/QJE/REStud) and accounting are
# deliberately excluded -- finance only. Each item is tagged T1/T2 in the email.
#
# Tier 1 -- NYU Stern's three top-tier FINANCE journals (the top-tier list also
# names 5 economics journals, excluded here per the finance-only scope).
JOURNALS_T1 = {
    "Journal of Finance": "0022-1082",
    "Review of Financial Studies": "0893-9454",
    "Journal of Financial Economics": "0304-405X",
}
# Tier 2 -- NYU Stern's "other notable" FINANCE journals + the practitioner /
# quant field journals (JPM, FAJ, Quantitative Finance, ...) a quant desk reads.
JOURNALS_T2 = {
    # NYU "other notable" finance
    "Financial Management": "0046-3892",
    "Journal of Banking and Finance": "0378-4266",
    "Journal of Corporate Finance": "0929-1199",
    "Journal of Financial and Quantitative Analysis": "0022-1090",
    "Journal of Financial Econometrics": "1479-8409",
    "Journal of Financial Intermediation": "1042-9573",
    "Journal of Financial Markets": "1386-4181",
    "Journal of Financial Services Research": "0920-8550",
    "Journal of Money, Credit and Banking": "0022-2879",
    "Review of Asset Pricing Studies": "2045-9920",
    "Review of Corporate Finance Studies": "2046-9128",
    "Review of Finance": "1572-3097",
    # practitioner / quant field finance journals (not on NYU's academic list,
    # but finance and central to a quant practitioner)
    "Quantitative Finance": "1469-7688",
    "Mathematical Finance": "0960-1627",
    "Finance and Stochastics": "0949-2984",
    "Financial Analysts Journal": "0015-198X",
    "Journal of Asset Management": "1470-8272",
    "Journal of Risk": "1465-1211",
    "Journal of Empirical Finance": "0927-5398",
    # NOTE: Journal of Portfolio Management, Journal of Derivatives, and Journal
    # of Financial Data Science are PM-Research journals -- collected via pmr()
    # below (with abstracts scraped from pm-research.com), not here.
    # EXCLUDED per finance-only scope: Econometrica, Journal of Political
    # Economy, Journal of Econometrics (economics); Management Science (NYU
    # classes it "other business disciplines"); accounting journals.
}

# ---- PM Research journals (pm-research.com) -------------------------
# Practitioner finance journals. Crossref lists each journal's articles by ISSN
# but PMR does NOT deposit abstracts to Crossref, so pmr() fetches each article's
# pm-research.com page and pulls the abstract from its citation_abstract meta tag
# (static HTML, no Cloudflare). All tagged tier T2. Abstracts are only fetched
# for articles not already in the archive, so re-runs don't re-scrape.
PMR_JOURNALS = {
    "Journal of Portfolio Management": "0095-4918",
    "Journal of Investing": "1068-0896",
    "Journal of Derivatives": "1074-1240",
    "Journal of Fixed Income": "1059-8596",
    "Journal of Financial Data Science": "2640-3943",
    "Journal of Alternative Investments": "1520-3255",
    "Journal of Wealth Management": "1520-4154",
    "Journal of Beta Investment Strategies": "2771-6511",
    "Practical Applications": "2329-0196",
}
PMR_MAX_PER_JOURNAL = 30    # newest articles listed per journal per run

# ---- SSRN via Crossref ----------------------------------------------
# SSRN registers DOIs under the 10.2139 prefix, indexed by Crossref same-day.
# This gets fresh SSRN papers WITHOUT scraping SSRN (which Cloudflare-blocks).
# The prefix alone is an all-discipline firehose, so each finance query narrows
# it before the LLM layer filters; queries overlap and dedup handles it.
SSRN_CROSSREF_PREFIX = "10.2139"
SSRN_ROWS = 60               # results per finance query
SSRN_QUERIES = [
    "asset pricing cross-section returns factor",
    "return predictability anomalies momentum value",
    "portfolio optimization allocation risk parity",
    "machine learning deep learning trading finance",
    "market microstructure liquidity execution high-frequency",
    "volatility variance risk premium options derivatives",
    "hedge fund mutual fund performance flows",
]

# ---- Practitioner blogosphere ---------------------------------------
QUANTOCRACY_RSS = "https://quantocracy.com/feed/"
# Direct practitioner RSS (verified 2026-07-18). Asset-manager house pages
# (AQR, Man, Research Affiliates) and SSRN eJournals have no usable RSS -- they
# are JS-rendered / scrape-blocked and would need a headless browser.
PRACTITIONER_FEEDS = {
    "Alpha Architect": "https://alphaarchitect.com/feed/",
    "Quantpedia": "https://quantpedia.com/feed/",
    "Newfound / Flirting with Models": "https://blog.thinknewfound.com/feed/",
    "Macrosynergy": "https://macrosynergy.com/research/feed/",
}

# ---- Asset-manager house research (headless Playwright) -------------
# JS-rendered sites with no RSS. Each entry: (insights URL, href regex that
# matches an article page). SSRN is intentionally excluded -- it Cloudflare-
# blocks headless browsers (verified). Best-effort: a site change/block or a
# missing browser just skips that firm, never breaks the run.
FIRM_SITES = {
    "AQR": ("https://www.aqr.com/Insights/Research",
            r"^/Insights/Research/[^/]+/[^/]+$"),
    "Man Group": ("https://www.man.com/insights",
                  r"/insights/[a-z0-9][a-z0-9-]{10,}$"),
    "Research Affiliates": ("https://www.researchaffiliates.com/insights/publications",
                            r"/insights/publications/articles/\d+-[a-z0-9-]+$"),
}
FIRM_MAX_ITEMS = 15   # newest per firm; cross-week dedup surfaces only net-new

# ---- Preprint probes via OpenAlex -----------------------------------
# label -> OpenAlex source id (Sxxxxxxxxx), or None to resolve by name at
# runtime. Resolution is logged so you can verify and hardcode it after
# run 1 (same workflow as SSRN). Each source is fetched independently; a
# 429/failure on one is logged and skipped, never killing the others.
# Keep these to genuine PREPRINT/working-paper repositories -- journals are
# already covered via Crossref, so a journal source here just adds dedup work.
OPENALEX_PREPRINT_SOURCES = {
    "SSRN": "S4210172589",                       # verified run-1 (2026-07-18)
    "Munich Personal RePEc Archive": "S4306400553",  # MPRA working papers
    "EconStor": "S4306401696",                   # ZBW open working-paper server
    # OSF Preprints (S4306401127) dropped -- all-disciplines repository that
    # brought in sociology/psychology/etc.; the three above are econ/finance.
}
# How many retries (with linear backoff) on an OpenAlex 429 before giving up.
OPENALEX_MAX_RETRIES = 4

# Restrict the topic sweep to finance/economics via primary_topic. Branch A
# (curated finance topics) uses the whole field 20 ("Economics, Econometrics
# and Finance"); branch B (broad keyword search) uses the tighter Finance
# subfield 2003 -- keyword search over all of economics otherwise returns
# economic history, philosophy-of-economics, regional-development, and spam.
OPENALEX_FINANCE_FIELD = "20"          # branch A (already topic-constrained)
OPENALEX_FULLTEXT_SUBFIELD = "2003"    # branch B: Finance only

# ---- OpenAlex topic sweep -------------------------------------------
# SEARCH SEEDS, not OpenAlex taxonomy names. Resolved once (run-1) against the
# ~4,500-topic taxonomy behind a finance-field + relevance-score gate; seeds
# that don't clear it run as precise fulltext /works searches instead.
TOPIC_SEARCH_TERMS = [
    # asset pricing core
    "asset pricing", "cross-section of stock returns", "return predictability",
    "cross-predictability", "lead-lag returns", "factor models",
    "factor investing", "anomalies", "momentum", "value premium",
    "risk premia", "expected returns",
    # portfolio
    "portfolio management", "portfolio optimization", "asset allocation",
    "risk parity", "portfolio rebalancing", "transaction costs",
    "market impact", "optimal execution",
    # methods
    "covariance estimation", "shrinkage estimation", "regime switching",
    "structural breaks", "forecast combination", "Bayesian portfolio",
    "estimation risk", "multiple testing finance", "backtest overfitting",
    # ML
    "machine learning asset pricing", "deep learning returns",
    "reinforcement learning trading", "textual analysis finance",
    "alternative data",
    # vol / derivatives / micro
    "volatility forecasting", "variance risk premium", "implied volatility",
    "option returns", "market microstructure", "liquidity",
    "high-frequency trading", "statistical arbitrage",
    # institutional
    "mutual fund performance", "fund flows", "hedge fund", "crowding",
    "short selling", "tail risk",
]

# Taxonomy survivors resolved run-1 (2026-07-18) behind the finance-field +
# score>=OPENALEX_TOPIC_MIN_SCORE gate. Only these cleared it (many seeds map
# to off-domain topics -- momentum->Physics, anomalies->CompSci -- or nothing).
# Empty this dict to force a live re-bootstrap via sources.resolve_topics().
OPENALEX_TOPIC_IDS = {
    "Financial Markets and Investment Strategies": "T10047",
    "Stochastic processes and financial applications": "T10067",
    "Financial Risk and Volatility Modeling": "T10282",
}
# Seeds already covered by the mapped topics above -- excluded from fulltext to
# avoid redundant queries.
_TOPIC_COVERED = {
    "asset pricing", "factor investing", "market microstructure", "liquidity",
    "risk premia", "volatility forecasting",
}
# Every other seed runs as a precise fulltext search.
OPENALEX_FULLTEXT_TERMS = [t for t in TOPIC_SEARCH_TERMS
                          if t not in _TOPIC_COVERED]

OPENALEX_TOPIC_MIN_SCORE = 3000   # relevance gate used during live re-bootstrap
OPENALEX_FULLTEXT_LIMIT = 25      # max works per fulltext seed

# S2 is best-effort on the free unauthenticated tier: 1 = try once, skip on a
# 429 with no wait (fast runs). Raise for retries-with-backoff if you rely on it.
S2_MAX_RETRIES = 1
SEMANTIC_SCHOLAR_QUERIES = [
    "asset pricing",
    "factor investing",
    "portfolio optimization",
    "machine learning finance",
    "market microstructure",
    "volatility risk premium",
]

# ---- LLM triage (optional; Google Gemini, free tier) ----------------
# Dormant unless GEMINI_API_KEY (or GOOGLE_API_KEY) is set; any failure
# degrades cleanly to the plain no-LLM feed. Ranks deduped items 0-100 for a
# "Top picks" section. Get a free key at https://aistudio.google.com/apikey.
LLM_MODEL = "gemini-flash-latest"   # Gemini (primary); alias -> current Flash
GROQ_MODEL = "llama-3.1-8b-instant"      # Groq: fast + high free-tier throughput
GROQ_BATCH = 5                           # smaller requests -> less likely to trip
#   Groq's low free-tier tokens-per-minute cap (was 8; 413s on the watchlist
#   chunk). A 413 now waits out the ~60s TPM window and retries (see _groq_call)
#   instead of dropping the chunk.
                                         # (413 on ~25 items) -- its provider fn
                                         # sub-chunks the batch to this size
MISTRAL_MODEL = "mistral-small-latest"   # Mistral (fallback if MISTRAL_API_KEY set)
# OpenRouter is now the PRIMARY provider, fronting DeepSeek. Chosen by a blind
# five-case bake-off (tools/bakeoff.py) against Gemini Flash, GPT-5 Mini and
# Claude Sonnet 5: every model refused the fabrication trap correctly, so the
# safety property lives in the prompt rather than the model, which left cost and
# concision to decide. DeepSeek scored joint-second on judgement at roughly a
# fifth of the price, and was the most concise. One key fronts every model, so
# swapping candidates later is a one-line change.
OPENROUTER_MODEL = "deepseek/deepseek-v3.2"
# OpenAI (paid; last in triage order so the free providers carry the 1500-item
# bulk and OpenAI cost only hits the ~250-item consensus shortlist). Reliable,
# high-quality ensemble vote. Set OPENAI_API_KEY.
OPENAI_MODEL = "gpt-5.4-mini"
LLM_RANK_BATCH = 40          # items scored per API call
LLM_MAX_RETRIES = 3          # retries (with backoff) on 429/5xx
LLM_BATCH_PAUSE = 6          # seconds between calls -- stay under free-tier RPM
# Wall-clock backstop so a run can NEVER wedge for hours when every free tier is
# rate-limited (what caused a 6h hang). When triage exceeds this, it stops and
# leaves the rest unscored for the next run -- watchlist items sort first, so
# they're always scored within the budget. 0 = no limit.
LLM_RANK_BUDGET_S = 35 * 60      # per-pass cap on the daily triage
LLM_CONSENSUS_BUDGET_S = 15 * 60  # per-pass cap on the consensus re-score
# Single wall-clock cap across EVERY LLM pass in a run (triage + consensus +
# the monthly re-scores). The per-pass budgets above don't compose -- four
# stacked passes overran the 120min job timeout by seconds -- so this global
# ceiling is the real guarantee the run finishes with room for portal + email +
# deploy. llm.start_run_budget() stamps it once at the top of scoring.
LLM_RUN_BUDGET_S = 55 * 60       # ~55 min total LLM; leaves ~65min for the rest

# ---- Ensemble consensus (shortlist only) ----------------------------
# The bulk triage (llm.rank) scores every item with the first available
# provider. Then llm.consensus() re-scores only the SHORTLIST -- items the
# triage found promising -- with ALL configured providers together and combines
# their independent votes: median of the 0-3 rubric levels, majority
# antecedent_match/novelty_type. The "do they converge" test: if the providers
# DISAGREE on contribution by more than CONSENSUS_AGREE_SPREAD levels the item
# is marked provisional (uncertain) rather than trusted -- consensus is only
# used when the models actually agree. Keeps the 3-4x cost on the few hundred
# items that decide Monthly/Recent, not the ~1500-item firehose.
CONSENSUS_MIN_RELEVANCE = 2   # shortlist: triage relevance level >= this
CONSENSUS_MIN_CONTRIB = 2     # AND triage contribution level >= this
CONSENSUS_MAX_ITEMS = 250     # hard cap on shortlist (cost bound); best first
CONSENSUS_AGREE_SPREAD = 1    # max contribution-level spread to count as converged
CONSENSUS_MAX_BATCHES = 6     # per-run consensus batch budget (backfill path)
TOP_PICKS = 20               # how many top-ranked items to feature in the email
MIN_SHOW_SCORE = 20          # hide items the LLM scored below this from the email
                             # (0-19 = off-topic/noise band); the portal keeps all

# ---- Prominence tiering ---------------------------------------------
# The email is divided into Tier 1 / Tier 2 / rest. An item is Tier 1 if it's a
# T1 journal, a prominent author (OpenAlex h-index >= PROM_H1), or a must-read
# by the LLM (score >= RANK_T1); Tier 2 is the analogous middle band.
# NOTE: rank_score is now the anchored `relevance` LEVEL (0-3) rescaled to
# 0/33/67/100 (llm.rank), not a continuous guess -- these thresholds happen to
# land exactly on that discrete scale: 85 admits only level 3 (100), 60 admits
# level 2+ (67/100), MIN_SHOW_SCORE=20 admits level 1+ (33/67/100).
PROM_H1 = 40                 # author h-index for "prominent" (tier 1)
PROM_H2 = 20                 # author h-index for "established" (tier 2)
RANK_T1 = 85                 # LLM score that alone earns tier 1
RANK_T2 = 60                 # LLM score that alone earns tier 2
RANK_INTERESTS = (
    "- Empirical asset pricing: cross-section of returns, factor models, "
    "anomalies, return predictability, risk premia, expected returns.\n"
    "- Portfolio construction: optimization, allocation, risk parity, "
    "rebalancing, transaction costs, optimal execution.\n"
    "- Quant methods: covariance/shrinkage estimation, regime switching, "
    "forecast combination, estimation risk, multiple testing, backtest "
    "overfitting.\n"
    "- Machine learning in finance: ML asset pricing, deep learning for "
    "returns, reinforcement-learning trading, textual/alternative data.\n"
    "- Volatility & derivatives: vol forecasting, variance risk premium, "
    "implied vol, option returns, market microstructure, liquidity, HFT, "
    "statistical arbitrage.\n"
    "- Institutional: fund performance, flows, hedge funds, crowding, short "
    "selling, tail risk.\n"
    "Prefer novel, rigorous, and practitioner-implementable work; deprioritize "
    "incremental results and papers with no finance angle."
)

# ---- Monthly top-50 picks: (subjective quality + real citations + real velocity)
#      x robustness x bounded reputation
# Design intent: the LLM is confined to purely SUBJECTIVE judgment (is this
# novel, does it generalize, is it useful/testable) -- everything quantifiable
# from real data (citation count, citation velocity, author/venue track record)
# is computed in code, never guessed by the LLM. Each calendar month's
# Monthly-tab list is the top MONTHLY_TOP_N papers, ranked by:
#
#   composite = (QUALITY_WEIGHT*base_quality + CITES_WEIGHT*paper_cites_norm
#                + VELOCITY_WEIGHT*velocity_norm) * R * M_rep
#
# (scoring.composite_entries). Any term whose data is unavailable (a paper too
# new to have citations/velocity yet) is excluded and its weight redistributed
# onto base_quality -- never penalised with a 0 for simply being new.
#
#   base_quality (SUBJECTIVE, LLM) -- weighted avg of three ANCHORED 0-3 rubric
#     levels (rescaled 0-100), never continuous guesses: generality (does the
#     mechanism travel across assets/strategies/regimes), contribution (novel
#     mechanism vs. incremental, capped when `provisional`), testability
#     (falsifiable/cheap to test with public data). Weights: AXIS_WEIGHTS.
#
#   paper_cites_norm (QUANTITATIVE, code) -- THIS paper's own Semantic Scholar
#     citation count (log, min-max in-pool). Direct empirical evidence the
#     field engaged with and built on this specific paper -- not a reputation
#     proxy, so it gets a real, meaningful weight, not a bounded nudge.
#
#   velocity_norm (QUANTITATIVE, code) -- THIS paper's real citation velocity
#     (S2 citationCount / years since publication, log, min-max in-pool),
#     computed only for papers >=1 year old with a known citation count. A
#     fast-accumulating paper is weighted differently than a slow-burn paper
#     with the same raw count -- purely arithmetic, no LLM estimate involved.
#
#   R (code, from LLM-EXTRACTED facts) -- soft, abstract-derived robustness
#     DISCOUNT (never a bonus; floor ROBUSTNESS_FLOOR). Multiplies by the
#     matching factor in ROBUSTNESS_DISCOUNTS for each flag the LLM found
#     EXPLICITLY stated in the abstract (isolated_backtest_only,
#     no_costs_mentioned, extreme_claimed_sharpe, weak_stat_support). A null
#     flag (abstract simply didn't say) is never penalised -- absence of
#     information isn't evidence of a problem, only an affirmatively-detected
#     one is. (The LLM only extracts whether the text states these facts; the
#     discount arithmetic itself is deterministic code, not an LLM opinion.)
#
#   M_rep (QUANTITATIVE, code) -- bounded REPUTATION multiplier in
#     [1-CRED_BOUND, 1+CRED_BOUND], i.e. [0.85, 1.15], from S2 author h-index +
#     JOURNAL_IMPACT ONLY (never this paper's own citations, which live in the
#     weighted blend above instead): these are priors about the AUTHOR's/
#     VENUE's general track record, not evidence about this specific paper, so
#     they can only nudge, never carry. Computed from whichever of the two
#     inputs are available; missing ones just drop from the average.
#
# relevance is a GATE, not a ranking weight (item needs relevance level >= 1 to
# be composite-eligible at all) -- it, topic, and the robustness flags are also
# kept on the item for the email/Recent/Archive/seminal-promotion paths.
MONTHLY_TOP_N = 50
AXIS_WEIGHTS = {                     # LLM subjective axes -> base_quality
    "generality": 0.40,
    "contribution": 0.35,
    "testability": 0.25,
}
QUALITY_WEIGHT = 0.45                # weight on base_quality (subjective, LLM)
CITES_WEIGHT = 0.30                  # weight on this paper's own citation count (real)
VELOCITY_WEIGHT = 0.25               # weight on this paper's own citation velocity (real)
CRED_BOUND = 0.15                    # M_rep (author h-index + journal IF) in [0.85, 1.15]
ROBUSTNESS_DISCOUNTS = {             # applied only when the LLM flags it True
    "isolated_backtest_only": 0.85,
    "no_costs_mentioned": 0.90,
    "extreme_claimed_sharpe": 0.85,
    "weak_stat_support": 0.85,
}
ROBUSTNESS_FLOOR = 0.5               # R never discounts below this

# How many days of the archive ship in the default-loaded docs/data.json
# (Recent/For You/Practitioners). The full archive keeps growing forever, but
# shipping the whole thing to the browser on every page load doesn't scale --
# it's split into docs/archive.json instead, lazy-fetched only when the
# Archive tab is actually opened. See portal.build().
PORTAL_RECENT_WINDOW_DAYS = 60

# Topic taxonomy for the portal's Archive tab (LLM assigns one per paper).
# Mirrors the Classics canon topics, plus a catch-all.
TOPICS = [
    "Asset Pricing & Factor Models",
    "Portfolio Construction & Optimization",
    "Market Efficiency & Behavioral",
    "Derivatives & Option Pricing",
    "Volatility Modeling",
    "Fixed Income & Credit",
    "Market Microstructure & Liquidity",
    "Fund Performance & Institutional",
    "Machine Learning in Finance",
    "Financial Econometrics",
    "Macro & Monetary",
    "Other",
]

# Approximate 2-year impact factors for the tracked journals (public/JCR ~2023,
# editable). Keys MUST match the labels in JOURNALS_T1/T2 and PMR_JOURNALS. Used
# only for the journal_if sub-score (value / table-max); items from an unlisted
# venue (preprints, blogs) get 0.
JOURNAL_IMPACT = {
    # Tier 1
    "Journal of Finance": 7.5,
    "Review of Financial Studies": 6.8,
    "Journal of Financial Economics": 9.6,
    # Tier 2 -- academic finance
    "Financial Management": 3.0,
    "Journal of Banking and Finance": 3.6,
    "Journal of Corporate Finance": 5.5,
    "Journal of Financial and Quantitative Analysis": 3.5,
    "Journal of Financial Econometrics": 2.5,
    "Journal of Financial Intermediation": 4.0,
    "Journal of Financial Markets": 2.5,
    "Journal of Financial Services Research": 1.5,
    "Journal of Money, Credit and Banking": 2.0,
    "Review of Asset Pricing Studies": 2.5,
    "Review of Corporate Finance Studies": 2.5,
    "Review of Finance": 4.4,
    "Quantitative Finance": 1.8,
    "Mathematical Finance": 2.0,
    "Finance and Stochastics": 1.6,
    "Financial Analysts Journal": 3.5,
    "Journal of Asset Management": 1.5,
    "Journal of Risk": 0.8,
    "Journal of Empirical Finance": 2.3,
    # PM-Research practitioner journals
    "Journal of Portfolio Management": 1.5,
    "Journal of Investing": 0.5,
    "Journal of Derivatives": 0.6,
    "Journal of Fixed Income": 0.5,
    "Journal of Financial Data Science": 1.0,
    "Journal of Alternative Investments": 0.8,
    "Journal of Wealth Management": 0.4,
    "Journal of Beta Investment Strategies": 0.3,
    "Practical Applications": 0.2,
}

# ---- Bayesian novelty prior (cross-examine new papers against history) ----
# The contribution.provisional flag -- which decides whether a contribution==3
# is allowed to count at full weight (scoring.composite_entries caps provisional
# ones at 2, portal Recent requires non-provisional, monthly.promote_seminal
# requires non-provisional) -- is set by a calibrated Bayesian posterior instead
# of an LLM guess (llm.rank):
#
#   prior(topic)  = P(a paper in this topic is seminal-caliber), a Beta-Binomial
#                   empirical-Bayes estimate (A0 + canon_count) / (A0 + B0 +
#                   archive_volume) from the seminal canon (canon.CANON) vs. our
#                   scored-archive volume. NUMBERS BELOW ARE PRECOMPUTED by
#                   tools/gen_novelty_prior.py -- regenerate (do not compute
#                   live: the Day-0/cleared archive would divide by zero).
#                   Caveat: archive_volume is a proxy for total papers in a
#                   topic, so priors are directionally right, not perfectly
#                   calibrated.
#   LR(match)     = likelihood ratio from the LLM's independent 3-way antecedent
#                   classification (NOT reused from contribution.level -- that
#                   would be circular).
#   posterior     = prior_odds * LR / (1 + prior_odds * LR); the paper is
#                   non-provisional only when posterior >= NOVELTY_CONFIDENCE.
NOVELTY_PRIOR_FALLBACK = 0.0476     # base rate A0/(A0+B0) for an unlisted topic
NOVELTY_PRIOR = {
    "Asset Pricing & Factor Models": 0.2136,        # canon=21, archive_vol=82
    "Portfolio Construction & Optimization": 0.102,  # canon=9, archive_vol=77
    "Market Efficiency & Behavioral": 0.1562,       # canon=9, archive_vol=43
    "Derivatives & Option Pricing": 0.1471,         # canon=9, archive_vol=47
    "Volatility Modeling": 0.1698,                  # canon=8, archive_vol=32
    "Fixed Income & Credit": 0.125,                 # canon=7, archive_vol=43
    "Market Microstructure & Liquidity": 0.1034,    # canon=8, archive_vol=66
    "Fund Performance & Institutional": 0.065,      # canon=7, archive_vol=102
    "Machine Learning in Finance": 0.197,           # canon=12, archive_vol=45
    "Financial Econometrics": 0.0614,               # canon=6, archive_vol=93
    "Macro & Monetary": 0.0106,                     # canon=0, archive_vol=73
    "Other": 0.0016,                                # canon=0, archive_vol=612
}
NOVELTY_LR = {                       # likelihood ratio by LLM antecedent verdict
    "matches_known": 0.15,           # core method is a cataloged framework -> against novelty
    "ambiguous": 1.0,                # some resemblance, can't tell -> uninformative
    "no_antecedent": 6.0,            # new mechanism, no identifiable antecedent -> for novelty
}
NOVELTY_CONFIDENCE = 0.28            # posterior >= this => contribution counts as non-provisional
#   Tuned so a confident no_antecedent verdict clears in every genuine quant
#   topic (incl. high-volume Financial Econometrics ~0.28 / Fund Performance
#   ~0.29), while non-core Macro (~0.06) and Other (~0.01) still require external
#   corroboration, and ambiguous/matches_known verdicts never clear anywhere.

# ---- Bayesian relevance posterior (replaces the flat relevance-level rescale) --
# The old `rank_score` (relevance.level/3*100, only 4 possible values -- 0/33/
# 67/100) drove both the displayed rating AND the Recent/Monthly gates, and a
# hard gate showing a constant ceiling value reads as broken, not selective.
# Same Bayesian shape as the novelty posterior above, but blending TWO real
# signals instead of one:
#   prior(topic) = (A0 + canon_weight*canon_count + archive_hits) /
#                  (A0 + B0 + canon_weight*canon_count + archive_total)
#                  -- archive_hits/archive_total: how often work in this topic
#                  has historically been judged core-fit (real data, not
#                  circular -- it's the PRIOR run's judgments, not this item's);
#                  canon_count: how canon-dense the topic is, same as novelty.
#                  NUMBERS BELOW ARE PRECOMPUTED by tools/gen_relevance_prior.py
#                  -- regenerate (do not compute live: divide-by-zero on a
#                  cleared archive, and drift run-to-run otherwise).
#   LR(category)  = likelihood ratio from the LLM's independent 3-way
#                   relevance_category verdict (core_fit/adjacent/off_topic) --
#                   NOT the relevance.level rubric, which stays a separate,
#                   graded (0-3) judgment used only for its "why" and as an
#                   axis fallback level.
#   posterior     = prior_odds * LR / (1 + prior_odds * LR); this posterior IS
#                   rank_score (rescaled to 0-100) and the Recent/Monthly gate,
#                   replacing the old exact relevance-level==3 requirement with
#                   posterior >= RELEVANCE_CONFIDENCE.
RELEVANCE_PRIOR_FALLBACK = 0.2
RELEVANCE_PRIOR = {
    "Asset Pricing & Factor Models": 0.6019,  # canon=21, archive_core=42/77
    "Portfolio Construction & Optimization": 0.4696,  # canon=9, archive_core=43/96
    "Market Efficiency & Behavioral": 0.2,  # canon=9, archive_core=3/51
    "Derivatives & Option Pricing": 0.56,  # canon=9, archive_core=31/56
    "Volatility Modeling": 0.5484,  # canon=8, archive_core=24/44
    "Fixed Income & Credit": 0.2364,  # canon=7, archive_core=4/38
    "Market Microstructure & Liquidity": 0.4286,  # canon=8, archive_core=29/73
    "Fund Performance & Institutional": 0.2136,  # canon=7, archive_core=13/86
    "Machine Learning in Finance": 0.3373,  # canon=12, archive_core=14/61
    "Financial Econometrics": 0.0965,  # canon=6, archive_core=3/98
    "Macro & Monetary": 0.0263,  # canon=0, archive_core=0/66
    "Other": 0.0188,  # canon=0, archive_core=11/682
}
RELEVANCE_LR = {                     # likelihood ratio by LLM relevance_category verdict
    "core_fit": 6.0,                 # squarely the kind of result this digest is for
    "adjacent": 1.0,                 # related but not central -> uninformative
    "off_topic": 0.15,               # not finance / no testable content -> against
}
RELEVANCE_CONFIDENCE = 0.75          # Recent's gate: relevance posterior >= this
#   Raised to 0.75 (per request) so Recent surfaces only high-confidence
#   relevant work -- core_fit in the strong-prior areas (Asset Pricing ~0.90,
#   Derivatives ~0.88, Volatility ~0.88, Portfolio ~0.84, Microstructure ~0.82,
#   ML ~0.75) clears, while weaker-topic core_fit (Financial Econometrics
#   ~0.39, Fund Performance ~0.62) no longer does. Only gates Recent's display
#   (the trusted watched-author lane still uses relevance_category=core_fit
#   directly, so a Kelly/Feng paper isn't held to the 75% bar).

# ---- Backward monthly backfill (progressive history) ----------------
# Every run refreshes the current month, then processes ONE earlier month,
# walking back to BACKFILL_FLOOR. BACKFILL_LLM_BATCHES caps LLM scoring calls
# spent on the backfill per run (after the present digest) -- a month too big
# for the budget resumes next run from state.db (month_progress). During
# catch-up run the workflow daily (LLM free-tier limits reset daily).
BACKFILL_FLOOR = "2010-01"          # earliest month to backfill (inclusive)
BACKFILL_LLM_BATCHES = 8            # LLM scoring batches/run for the backfill
S2_BATCH_SIZE = 500                 # Semantic Scholar /paper/batch hard cap

# Promote a present-run paper into the Classics "modern" (emerging-seminal) list
# when the LLM rates its contribution at the top anchor (non-provisional) AND
# its composite clears this bar.
SEMINAL_CONTRIB_MIN = 3      # contribution level (of 0-3), must be non-provisional
SEMINAL_COMPOSITE_MIN = 80

# ---- Email -----------------------------------------------------------
SUBJECT_PREFIX = "[Research Digest]"
# Link shown in the email to browse the full archive portal. Set to your hosted
# URL (GitHub Pages https://<user>.github.io/quant-digest/, Netlify, Cloudflare
# Pages) once docs/ is published; the local fallback works with
# `py -m http.server 8000 --directory docs`. Empty hides the button.
PORTAL_URL = "https://quant-digest-e62.pages.dev"  # Cloudflare Pages (login-walled)

# ---- P2: Author watchlist (track specific researchers, never miss them) ----
# Papers by these people are pulled directly from OpenAlex each run regardless
# of whether a source feed carried them, and are never dropped by the LLM batch
# budget. The seed list below is resolved to OpenAlex author ids ONCE by
# tools/gen_watchlist.py (which writes docs/watchlist.json); the daily run just
# reads that file. The roster also grows automatically -- see auto-promotion.
#
# A watchlisted paper still gets a relevance score, but that score is only a
# DISPLAYED LABEL (a "relevance NN%" tag), never a filter: everything a watched
# author publishes is surfaced so you can judge it yourself.
WATCHLIST_LOOKBACK_DAYS = 60          # OpenAlex per-author window (was 120)
WATCHLIST_MAX_PER_AUTHOR = 8          # cap new works pulled per author per run
# Deep-pull only a ROTATING slice of the roster each run (the slow part is the
# per-author OpenAlex+Crossref calls). The cross-match against collected items
# stays FULL every run, so a watched author's paper from any normal source is
# never missed; the deep pull just catches OpenAlex/Crossref-only papers, and
# with a 90-day window + every-3-day runs each author is still checked many
# times before a paper ages out. 0 = no rotation (pull all every run).
WATCHLIST_PER_RUN = 40                # ~2 slices over the 77-author roster
# Auto-promotion: after a quarterly refresh, anyone who recurred in our OWN
# archive at least this many times above this composite last quarter joins the
# roster (source="auto"). Unbounded growth, no pruning, individuals only.
WATCHLIST_PROMOTE_MIN_PAPERS = 3
WATCHLIST_PROMOTE_MIN_COMPOSITE = 70

# Seed roster: {name, hint}. `hint` disambiguates the OpenAlex name search
# (an institution or subfield) -- gen_watchlist.py picks the economics/finance
# author with the strongest match. Canon authors (canon.py surname hints) are
# folded in automatically by the generator, so they need not be repeated here.
WATCHLIST_SEED = [
    # ML / empirical asset pricing
    {"name": "Bryan Kelly", "hint": "Yale asset pricing machine learning"},
    {"name": "Dacheng Xiu", "hint": "Chicago Booth machine learning"},
    {"name": "Shihao Gu", "hint": "empirical asset pricing machine learning"},
    {"name": "Stefan Nagel", "hint": "Chicago Booth asset pricing"},
    {"name": "Serhiy Kozak", "hint": "Maryland stochastic discount factor"},
    {"name": "Stefano Giglio", "hint": "Yale factor models"},
    {"name": "Markus Pelger", "hint": "Stanford deep learning asset pricing"},
    {"name": "Guanhao Feng", "hint": "City University Hong Kong deep factors"},
    {"name": "Semyon Malamud", "hint": "EPFL finance machine learning"},
    {"name": "Doron Avramov", "hint": "Reichman machine learning anomalies"},
    {"name": "Andreas Neuhierl", "hint": "Washington University asset pricing"},
    # Factors / anomalies / cross-section
    {"name": "Robert Novy-Marx", "hint": "Rochester profitability factor"},
    {"name": "Kent Daniel", "hint": "Columbia momentum"},
    {"name": "Kewei Hou", "hint": "Ohio State q-factor"},
    {"name": "Lu Zhang", "hint": "Ohio State q-factor investment"},
    {"name": "Zhiguo He", "hint": "Stanford intermediary asset pricing"},
    {"name": "Andrew Y. Chen", "hint": "Federal Reserve replication anomalies"},
    {"name": "Campbell Harvey", "hint": "Duke multiple testing factors"},
    {"name": "Christopher Polk", "hint": "LSE asset pricing"},
    {"name": "Jules van Binsbergen", "hint": "Wharton asset pricing"},
    # Trend / managed futures / multi-asset
    {"name": "Lasse Heje Pedersen", "hint": "AQR Copenhagen factors liquidity"},
    {"name": "Tobias Moskowitz", "hint": "Yale time series momentum"},
    {"name": "Andrea Frazzini", "hint": "AQR betting against beta"},
    {"name": "Nick Baltas", "hint": "trend following risk parity multi-asset"},
    {"name": "Antti Ilmanen", "hint": "AQR expected returns"},
    # Volatility / derivatives
    {"name": "Torben Andersen", "hint": "Northwestern realized volatility"},
    {"name": "Tim Bollerslev", "hint": "Duke volatility GARCH"},
    {"name": "Peter Carr", "hint": "NYU derivatives"},
    {"name": "Ian Dew-Becker", "hint": "Northwestern variance risk premium"},
    {"name": "Grigory Vilkov", "hint": "Frankfurt implied volatility"},
    # Fixed income / credit / macro-finance
    {"name": "Ralph Koijen", "hint": "Chicago Booth demand system asset pricing"},
    {"name": "Sydney Ludvigson", "hint": "NYU macro finance risk premia"},
    {"name": "Monika Piazzesi", "hint": "Stanford term structure"},
    {"name": "Anna Cieslak", "hint": "Duke monetary policy bonds"},
    {"name": "Hanno Lustig", "hint": "Stanford exchange rates bonds"},
    # Econometrics / forecasting / Bayesian
    {"name": "Francis X. Diebold", "hint": "Pennsylvania forecasting"},
    {"name": "Allan Timmermann", "hint": "UCSD forecast combination"},
    {"name": "Andrew Patton", "hint": "Duke financial econometrics"},
    {"name": "Barbara Rossi", "hint": "Pompeu Fabra forecasting"},
    {"name": "Dimitris Korobilis", "hint": "Glasgow Bayesian VAR"},
    {"name": "Gary Koop", "hint": "Strathclyde Bayesian econometrics"},
    # Microstructure / liquidity
    {"name": "Terrence Hendershott", "hint": "Berkeley high frequency trading"},
    {"name": "Albert J. Menkveld", "hint": "Vrije Universiteit market microstructure"},
    {"name": "Marcos Lopez de Prado", "hint": "machine learning finance backtesting"},
    # Behavioral / limits to arbitrage
    {"name": "Nicholas Barberis", "hint": "Yale behavioral finance"},
    {"name": "David Hirshleifer", "hint": "USC behavioral finance"},
    {"name": "Robin Greenwood", "hint": "Harvard limits arbitrage"},
    {"name": "Kelly Shue", "hint": "Yale behavioral finance"},
    # Practitioner-academic (prolific)
    {"name": "David Blitz", "hint": "Robeco factor investing"},
    {"name": "Pim van Vliet", "hint": "Robeco low volatility"},
    {"name": "Guido Baltussen", "hint": "Robeco Erasmus factor premia"},
    {"name": "Rob Arnott", "hint": "Research Affiliates smart beta"},
    {"name": "Victor DeMiguel", "hint": "London Business School portfolio optimization"},
]


# ---- Desk sleeves: the SECOND classification -------------------------
# The existing rubric answers "is this good quant research?" -- deliberately
# desk-agnostic. This answers the separate question "which parts of MY book does
# this touch, and how usable is it here?". The two are orthogonal: a first-rate
# microstructure paper scores high on the first and 1 on desk_fit.
#
# MULTI-LABEL, and that is the whole design. Sleeves overlap in reality -- a
# paper on Eurozone bond convenience yields is genuinely carry AND rates; one on
# currency exposure under interest-parity deviations is genuinely carry AND fx.
# Earlier single-label attempts all failed the same way: forcing a paper to LOSE
# one true tag to WIN another, then treating the loss as a misclassification.
# There is no boundary to police once a paper can hold every tag that fits.
#
# So each definition below is an independent membership test -- "does this paper
# involve X?" -- not a slot competing with the others.
SLEEVES = {
    "trend_cta": (
        "time-series momentum, trend-following or managed-futures systems, "
        "volatility targeting and vol scaling, crisis alpha and long-convexity "
        "payoffs, breakout and moving-average rules, CTA replication"),
    "carry": (
        "the RETURN EARNED FROM HOLDING an asset -- a yield, roll or interest "
        "differential. FX carry and the forward premium, deviations from "
        "interest parity, term premium treated as a harvestable return, "
        "commodity roll yield, convenience yield, normal backwardation, credit "
        "carry. Tag this whenever a yield/roll differential is a subject of the "
        "paper, in ANY asset class, even if the paper is also about that asset "
        "class's modelling. NOT carry: a pricing or valuation SPREAD between "
        "two otherwise similar securities (a green-bond premium, an issuer "
        "premium, a liquidity discount) -- that is a relative-value or credit "
        "question, not a return earned from holding"),
    "fx": (
        "currencies -- exchange-rate determination or forecasting, the dollar "
        "factor, intervention, EM currencies, PPP, currency hedging"),
    "rates_credit": (
        "interest rates and credit -- yield-curve and term-structure modelling, "
        "duration and convexity, credit spreads, default and sovereign risk, "
        "monetary transmission to bond markets"),
    "commodities": (
        "commodity markets -- theory of storage, hedging pressure, inventories, "
        "energy, metals, agriculture, futures curve shape"),
    "macro_regime": (
        "the macro state -- business cycle, recessions, monetary and fiscal "
        "policy, inflation dynamics and expectations, regime switching, "
        "nowcasting from macro data, positioning and flow data"),
    "cross_asset": (
        "allocation across asset classes -- risk parity, stock-bond correlation "
        "and its regimes, multi-asset portfolio construction, diversification"),
    "vol_options": (
        "volatility and options -- implied and realized volatility, the variance "
        "risk premium, option-implied information, volatility surfaces, VIX"),
    "equity_xs": (
        "the equity cross-section -- equity factors and anomalies, "
        "characteristics, factor zoo and replication"),
    "microstructure": (
        "market plumbing -- order flow, limit-order books, liquidity, market "
        "impact, transaction-cost modelling, high-frequency trading"),
    "other": (
        "none of the above -- corporate finance, banking, real estate, "
        "crypto-specific, ESG, household finance, or outside finance entirely"),
}
SLEEVES_MAX = 3          # tags per paper; more than this is not a judgement

# desk_fit -- how usable this is on a systematic macro / CTA desk, judged
# SEPARATELY from research quality.
DESK_FIT_ANCHORS = (
    "3 = directly usable on a systematic macro/CTA desk: a tradeable signal, a "
    "portfolio/risk technique, or a method the desk would actually apply.\n"
    "2 = relevant background -- context, a stylised fact, or an input the desk "
    "would want to know, but not directly applicable.\n"
    "1 = adjacent quant finance, outside the desk's sleeves (e.g. a strong "
    "equity-anomaly or microstructure paper).\n"
    "0 = not relevant to this desk at all."
)
