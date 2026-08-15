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

    # UNVERIFIED. Pakistani job boards may or may not expose RSS.
    # Run `python verify_feeds.py` before enabling.
    # ("rozee", "https://www.rozee.pk/rss/jobs"),
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
PK_ELIGIBLE = [
    "anywhere in the world", "worldwide", "world wide", "globally remote",
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
]

# Notification order, best first. This is the line to change.
#
# Note the tension: "pk_local" first surfaces Pakistani employers, which is
# what you asked for, but those pay local rates. "pk_eligible" first surfaces
# international postings that accept candidates in Pakistan, which is the
# stronger financial path. Swap the first two entries to flip it.
TIER_ORDER = ["pk_local", "pk_eligible", "unknown", "geo_blocked"]

# Set True once you trust the GEO_BLOCKED list. False demotes them to the
# bottom instead of dropping them, so you can see what's being caught.
DROP_GEO_BLOCKED = False

# Safety valve. If a cycle produces more than this, send the top N and a
# one-line summary of the rest rather than flooding your phone.
MAX_NOTIFY_PER_CYCLE = 25
