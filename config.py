"""The only file you should need to edit regularly.

Every list here is compiled by main._build() into one regex with
non-alphanumeric lookarounds. Two rules follow from that, and they are not
style preferences - both were real bugs:

  1. Never add a term under three characters.
  2. Never add a term ending in punctuation.

`rs.` in PK_LOCAL matched "engineers." and "years." and tagged 81 of 83
US postings as Pakistani. `" go "` matched "we go through discovery" and put
a Paralegal listing in the results. Word boundaries fix the first kind of
mistake but not the second: "go" is also an English word, so it stays out.

A space inside a term matches any run of whitespace, hyphens or underscores,
so "full stack" also covers "full-stack". It does NOT cover "fullstack" -
closed-up spellings need their own entry.
"""

# (source_label, url)
# (source_label, url, kind) where kind is "rss" or "json".
# The third element is optional and defaults to "rss".
FEEDS = [
    # Reddit subreddits. Unauthenticated Reddit tolerates roughly one request
    # every two seconds; see HOST_MIN_INTERVAL below.
    ("r/forhire", "https://www.reddit.com/r/forhire/new/.rss?limit=50"),
    ("r/remotejs", "https://www.reddit.com/r/remotejs/new/.rss?limit=50"),
    ("r/jobbit", "https://www.reddit.com/r/jobbit/new/.rss?limit=50"),
    ("r/hiring", "https://www.reddit.com/r/hiring/new/.rss?limit=50"),
    ("r/devopsjobs", "https://www.reddit.com/r/devopsjobs/new/.rss?limit=50"),

    # Reddit sitewide search. Usually higher signal than the subreddit feeds.
    ("search:golang", "https://www.reddit.com/search.rss?q=hiring+golang&sort=new&limit=50"),
    ("search:backend", "https://www.reddit.com/search.rss?q=hiring+backend+remote&sort=new&limit=50"),
    ("search:erpnext", "https://www.reddit.com/search.rss?q=erpnext+OR+frappe&sort=new&limit=50"),
    ("search:pakistan", "https://www.reddit.com/search.rss?q=hiring+pakistan+developer&sort=new&limit=50"),

    # Boards with working RSS.
    ("wwr:backend", "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"),
    ("wwr:fullstack", "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"),

    # These two ship malformed XML. Their JSON endpoints are the fix.
    # If either 403s or changes shape, comment it out rather than fighting it.
    ("remoteok", "https://remoteok.com/api", "json"),
    ("remotive", "https://remotive.com/api/remote-jobs", "json"),

    # Himalayas. The most useful source here: every job carries
    # `timezoneRestrictions` as a list of UTC offsets, so eligibility for a
    # UTC+5 candidate is a fact rather than an inference from prose. See
    # parse_json_jobs() for how that is folded into the geo vocabulary.
    ("himalayas", "https://himalayas.app/jobs/api?limit=100", "json"),

    # Jobicy. Smaller, remote-only, carries a structured `jobGeo`.
    ("jobicy", "https://jobicy.com/api/v2/remote-jobs?count=50", "json"),

    # Mastodon hashtag timelines. Valid RSS, but items carry no <title> - the
    # whole post is in <description> - so they need parse_mastodon() to
    # synthesize one. Without it every entry dies at TITLE_ROLE; measured at
    # 0 of 20 before the adapter existed.
    #
    # Tags chosen by measurement, not by guessing. Match counts out of 20:
    #   jobsearch 9   hiring 4   rustjobs 3/9
    #   python 3      - excluded, "PEP 839: ... C API" matches via `api`
    #   hiringnow, getfedihired, techjobs, jobs, remotework, golang,
    #   javascript, webdev - all 0
    #
    # One instance only. mastodon.social, fosstodon.org and hachyderm.io all
    # returned the same lead item: federated timelines overlap almost
    # completely, so polling several is duplicate work at triple the requests.
    # #hiring is largely a subset of #jobsearch and is kept only for drift.
    ("masto:jobsearch", "https://mastodon.social/tags/jobsearch.rss", "mastodon"),
    ("masto:hiring", "https://mastodon.social/tags/hiring.rss", "mastodon"),
    ("masto:rustjobs", "https://mastodon.social/tags/rustjobs.rss", "mastodon"),

    # Hacker News "Ask HN: Who is hiring?" - one thread a month, 500-800
    # top-level comments, each one a posting in a rigid pipe-delimited format
    # the existing filters read natively. Remote-heavy and startup-heavy.
    #
    # The URL is a story SEARCH, not a thread: the thread id changes monthly.
    # It must be search_by_date filtered to the `whoishiring` account -
    # Algolia's relevance search returns the 2016 and 2020 threads first.
    ("hn:whoishiring",
     ("https://hn.algolia.com/api/v1/search_by_date"
      "?tags=story,author_whoishiring&hitsPerPage=5"), "hn"),

    # DEAD - measured, not assumed. Do not re-add without re-testing.
    #   rozee.pk       403 on every path including the homepage. Bot
    #                  protection, not a missing feed; no user-agent fixes it.
    #   mustakbil.com  no RSS (404). Homepage loads, so scraping is the only
    #                  route, against a page not built for it.
    #   brightspyre    RSS endpoint 500s.
    # Consequence: no Pakistani job board is reachable, which is why the
    # pk_local tier stays empty. Telegram channels are the remaining option.
    # ("rozee", "https://www.rozee.pk/rss/jobs"),

    # Arbeitnow returns 175 rows and was deliberately rejected: the feed is
    # German-language and mostly on-site ("Werkstudent technische
    # Dokumentation", remote: False). High volume, low signal.
    # ("arbeitnow", "https://arbeitnow.com/api/job-board-api", "json"),
]

# Minimum seconds between requests to the same host.
#
# Reddit was raised 2.5 -> 5.0 after every subreddit and search feed failed:
# five exhausted their 429 retries and four returned 403 Blocked outright.
# Be aware this only addresses the 429s. A 403 is the unauthenticated
# user-agent being refused, and no interval fixes that - OAuth via PRAW is
# the real answer if these stay dead. http_get() does not retry a 403, so a
# blocked feed costs one request, not three.
HOST_MIN_INTERVAL = {
    "www.reddit.com": 5.0,
    "reddit.com": 5.0,
    # Three tag feeds off one volunteer-run instance. Nothing has rate-limited
    # us here; this is politeness, not a fix for an observed 429.
    "mastodon.social": 2.0,
}
DEFAULT_MIN_INTERVAL = 0.5

# Base backoff when a host returns 429 without a Retry-After header.
RATE_LIMIT_BACKOFF = 10.0


# -------------------------------------------------------------- the 4-stage filter

# Stage 1. Matched against the TITLE ONLY. Any hit kills the entry.
# This does most of the work: it is what keeps recruiters, designers and
# support roles out without the stack terms in their boilerplate ever getting
# a vote.
TITLE_BLOCK_ROLE = [
    "designer", "design lead", "graphic", "illustrator", "animator",
    "photographer", "video editor", "motion",
    "marketer", "marketing", "seo", "sem", "growth hacker", "copywriter",
    "copy writer", "content writer", "content creator", "social media",
    "community manager", "brand",
    "sales", "salesforce admin", "account executive", "account manager",
    "business development", "recruiter", "recruiting", "talent acquisition",
    "sourcer", "headhunter",
    "paralegal", "attorney", "lawyer", "legal counsel", "accountant",
    "bookkeeper", "auditor", "financial analyst",
    "customer support", "customer success", "customer service",
    "support agent", "help desk", "helpdesk", "call center", "call centre",
    "virtual assistant", "executive assistant", "personal assistant",
    "data entry", "transcription", "transcriber", "translator", "voice over",
    "teacher", "tutor", "instructor", "curriculum", "nurse", "therapist",
    "driver", "warehouse", "dispatcher",
    "product manager", "project manager", "program manager", "scrum master",
    "product owner", "business analyst", "technical writer",
    "moderator", "annotator", "labeler", "labeller",
]

# Stage 1, seniority half. Kept separate because it is the tuning dial.
#
# `staff` and `principal` used to be here and were removed after measuring
# them against the corpus: between them they blocked 14 postings, 13 of which
# were real senior IC engineering roles (Coinbase, Gusto, Datadog, Twilio,
# Faire). The comp gap is large and staff titles at those companies are
# reachable, so they are worth seeing.
#
# What is left blocks the management track, not senior ICs. Those 26 blocks
# are 20 pure non-engineering roles (Internal Audit, Clearing Operations,
# Threat Assessment, Creative Director) plus 6 Engineering Manager roles,
# which are deliberately out: that is a people-management ladder, not an IC
# one. Re-add "staff"/"principal" here if the feed gets too senior.
TITLE_BLOCK_SENIORITY = [
    "manager", "director",
    "head of", "chief", "cto", "vice president", "vp of engineering",
]

TITLE_BLOCK = TITLE_BLOCK_ROLE + TITLE_BLOCK_SENIORITY

# Stage 2. Matched against the TITLE ONLY. The title must read as an
# engineering role or the entry is dropped.
#
# This gate exists because matching stack terms against the body alone let
# "Paralegal, Litigation" through - the firm's boilerplate mentioned Python.
# The title decides the role; the body only confirms the stack.
TITLE_ROLE = [
    "engineer", "engineering", "developer", "development", "programmer",
    "coder", "architect", "swe", "sde", "devops", "sre",
    # Plurals are spelled out because _build()'s trailing (?![a-z0-9])
    # lookaround makes "developer" fail against "Developers". Board titles are
    # singular so this never surfaced, but Hacker News headers are routinely
    # plural - "Wine, 3D Graphics, and General Open Source Developers" - and
    # every one of them was being dropped at the TITLE_ROLE stage.
    # Widening the lookaround to allow a trailing "s" was the alternative and
    # is worse: it would make "sale" match "sales" in TITLE_BLOCK.
    "engineers", "developers", "programmers", "architects",
    "backend", "back end", "frontend", "front end", "fullstack", "full stack",
    "software", "web developer", "webdev", "api", "platform", "infrastructure",
    "data engineer", "machine learning engineer", "ml engineer",
    "tech lead", "technical lead", "cto",  # cto also blocks in stage 1; stage 1 wins
]

# Stage 3a. Matched against the TITLE ONLY. Any hit kills the entry.
#
# These are scoped to the title because they are subject markers, not
# disqualifiers. Every one of them was moved here from the title+body list
# after it started eating real leads: "wordpress" in the body of Lemon.io's
# and A.Team's postings is one entry in a long list of stacks they staff for,
# and it was killing "Senior AI Engineer" and a $90-170/hr contract role. In
# the title it means what it says - the job is a WordPress job.
EXCLUDE_TITLE = [
    "wordpress", "shopify", "webflow", "wix", "squarespace", "godaddy",
    "for hire", "seeking work", "looking for work", "available for work",
    "my portfolio", "hire me",
    # The fediverse's job-seeking hashtag. Mastodon surfaced candidates
    # advertising themselves ("I'm looking for a Rust job", "time to find my
    # next .NET development role") which read as engineering roles and passed
    # every stage. They phrase it too many ways to enumerate, but they all tag
    # it, so the tag is the reliable marker.
    "getfedihired",
]

# Stage 3b. Matched against TITLE + BODY. Any hit kills the entry.
#
# Only things that disqualify you no matter where they are written. A US
# citizenship requirement buried in paragraph nine is still a hard no.
EXCLUDE = [
    "on-site only", "onsite only", "must be located in", "must reside in",
    "us citizen", "green card", "security clearance", "w2 only",
    "unpaid", "equity only", "no pay", "revenue share", "commission only",
    "profit share", "pay after", "paid in exposure",
]

# Stage 4. Matched against TITLE + BODY. At least one hit required.
#
# Note "go" is absent on purpose - see the module docstring. Golang postings
# are caught by the spelled-out variants, and by the rest of the stack a Go
# shop inevitably lists.
STACK = [
    "golang", "go developer", "go engineer", "go backend", "goroutine",
    "python", "django", "fastapi", "flask", "celery", "sqlalchemy",
    "node.js", "nodejs", "node js", "express.js", "nestjs", "deno", "bun",
    "typescript", "javascript", "react", "react.js", "reactjs", "next.js",
    "nextjs", "vue", "vue.js", "svelte", "angular", "tailwind", "remix",
    "postgres", "postgresql", "mysql", "mariadb", "sqlite", "mongodb",
    "redis", "elasticsearch", "clickhouse", "dynamodb", "supabase",
    "firebase", "prisma",
    "kafka", "rabbitmq", "nats", "temporal", "grpc", "graphql", "rest api",
    "protobuf", "websocket",
    "microservice", "microservices", "distributed system",
    "distributed systems", "event driven", "message queue",
    "kubernetes", "docker", "terraform", "ansible", "helm", "containerized",
    "aws", "gcp", "azure", "cloudflare", "digitalocean", "heroku", "fly.io",
    "ci/cd", "github actions", "gitlab ci", "jenkins", "observability",
    "erpnext", "frappe",
    "rust", "elixir", "scala", "kotlin", "java", "ruby on rails", "rails",
    "laravel", "php", "dotnet", ".net", "c#",
]

# Entries older than this are ignored even if unseen (protects against
# a feed reordering and dumping stale posts on you).
MAX_AGE_DAYS = 7

# Rows older than this get pruned from the DB.
RETAIN_DAYS = 60


# ---------------------------------------------------------------- geo priority

# Signals that this is a Pakistani employer / local market posting.
#
# `rs.` used to live here. It matched "engineers.", "years.", "hours." and
# tagged 81 of the 83 pk_local rows in the corpus. `multan` was fine as a
# word but matched "siMULTANeously" as a substring - word boundaries save it.
# `" pst "` was here for Pakistan Standard Time and is gone for good: in job
# postings PST means Pacific, so it labelled US-timezone roles as local.
PK_LOCAL = [
    "pakistan", "pakistani", "karachi", "lahore", "islamabad", "rawalpindi",
    "faisalabad", "peshawar", "multan", "sialkot", "hyderabad sindh",
    "rozee", "mustakbil", "brightspyre", "bayt",
    "pkr", "rupees", "pakistan standard time",
]

# Strong, explicit signals that a candidate in Pakistan is eligible.
# Keep these phrases specific. Bare "remote" means nothing.
# `worldwide` and `world wide` were removed after measuring them: between them
# they promoted 46 of 649 corpus rows, and the term was never describing where
# the candidate may live. It was company boilerplate -
#
#   "...impact millions of users worldwide"        (MapTiler, Remote in EUROPE)
#   "...organizations in the U.S. and worldwide"   (Amwell)
#   "...delivering AI services worldwide"          (Azumo, LATIN AMERICA x4)
#
# - which put four Latin-America-only roles and a Europe-only role at the very
# top of the notification order once pk_eligible started leading TIER_ORDER.
#
# This is the `go` trap from the module docstring, not the `rs.` one: the word
# boundaries worked correctly and matched the word that was actually written.
# "worldwide" is simply ordinary marketing English as well as a region marker.
# "anywhere in the world" survives because no company writes that about its
# customers. The reliable eligibility signal now comes from Himalayas'
# structured timezoneRestrictions instead of from prose - see timezone_marker()
# in main.py.
PK_ELIGIBLE = [
    "anywhere in the world", "globally remote",
    "fully remote, anywhere", "remote - anywhere", "any timezone",
    "any time zone", "location independent", "work from anywhere",
    "south asia", "apac", "asia pacific", "gmt+5", "utc+5",
    # "emea" was here and is deliberately gone: it means Europe/Middle East/
    # Africa, and Pakistan is South Asia - outside it. It was promoting 4
    # corpus postings to pk_eligible on a region that does not include you.
    # The Middle East terms below stay; those are genuinely reachable.
    "middle east", "gulf", "uae", "dubai", "saudi",
]

# Explicit geographic exclusion. These postings are almost always a waste
# of your time even though they say "remote" in the title.
GEO_BLOCKED = [
    "us only", "usa only", "u.s. only", "us-only", "united states only",
    "us based", "u.s. based", "usa based", "based in the us",
    "must be located in the us", "must reside in the us",
    "authorized to work in the united states", "work authorization in the us",
    "eu only", "europe only", "eu-based", "europe-based", "based in europe",
    "uk only", "uk-based", "must be based in the uk", "right to work in the uk",
    "canada only", "canadian residents", "australia only",
    "latam only", "latin america only",
    "eastern time zone only", "pacific time only", "est only", "pst only",
    "must be in a us timezone", "overlap with pacific time",
    # Synthetic marker, not prose. parse_json_jobs() emits this when a board
    # publishes a timezone whitelist that excludes UTC+5. Nothing in a real
    # posting says "timezone restricted" - it exists so structured data can
    # reach the geo classifier through the same vocabulary as everything else.
    "timezone restricted",
]

# Notification order, best first. This is the line to change.
#
# pk_eligible leads deliberately. Two reasons, both measured:
#
#   1. Supply. Every Pakistani job board is unreachable (see FEEDS), so
#      pk_local has no source feeding it and sits empty. Ordering a tier first
#      does nothing when nothing lands in it.
#   2. Pay. International postings that accept a candidate in Pakistan are the
#      stronger financial path than local-market listings.
#
# Himalayas is what makes this tier trustworthy: its timezoneRestrictions
# field turns "does this accept UTC+5" from a guess about prose into a fact.
# If Telegram channels are added and pk_local starts filling up, swap the
# first two entries back.
TIER_ORDER = ["pk_eligible", "pk_local", "unknown", "geo_blocked"]

# Set True once you trust the GEO_BLOCKED list. False demotes them to the
# bottom instead of dropping them, so you can see what's being caught.
DROP_GEO_BLOCKED = False

# Safety valve. If a cycle produces more than this, send the top N and a
# one-line summary of the rest rather than flooding your phone.
MAX_NOTIFY_PER_CYCLE = 25
