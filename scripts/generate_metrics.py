"""
Generate the UnknuwProjects GitHub overview card (metrics.svg).

Why REST instead of GraphQL?
-----------------------------
The previous version used the GraphQL `contributionsCollection` query.
It returned 0 commits / 0 streaks for this account on every single run
since the workflow was created (verified across all generated SVGs in
git history), even though the profile shows 533 contributions in the
last year. Rather than fight an API that silently returns empty data
for this account, this version counts real activity through the REST
API, which provably returns this account's commits, issues and PRs:

- Commits: listed per repository and branch with the `author` filter
  (the account's commit emails are linked to the account, verified via
  the API), plus a fallback scan of each default branch that also
  matches the author's known names/emails, so commits made with an
  unlinked email are still counted. Bot commits are never counted.
- Issues / PRs: listed per repository with the `creator` filter, plus
  the Search API so contributions to other people's repositories
  (e.g. a PR to RanggaCasper/Black-Android) are counted too.
- Contributors: unique people who contributed to the original
  repositories (commits, issues, PRs, reviews). This is a count of
  *people*, not contributions -- the two were easy to confuse on the
  old card, so the labels now say exactly what each number is.

Counting windows (kept distinct, as requested):
- All-time: everything since the account was created
  (created_at from the API, 12 Feb 2026 for this account).
- Last 365 days: the trailing 365-day window ending today.
  The two numbers are equal until the account is older than a year,
  then they diverge -- that is expected, not a bug.

Runs with the default GITHUB_TOKEN from GitHub Actions. No personal
access token and no METRICS_TOKEN secret are needed anymore
(METRICS_TOKEN is still accepted as a fallback if set).
"""

import html
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

USER = os.getenv("METRICS_USER", "UnknuwProjects")
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("METRICS_TOKEN")

if not TOKEN:
    raise SystemExit(
        "Missing GITHUB_TOKEN (GitHub Actions provides it automatically)."
    )

API = "https://api.github.com"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
WINDOW_START = TODAY - timedelta(days=364)  # trailing 365 days, incl. today

S = requests.Session()
S.headers.update(
    {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
)


def get(url, params=None):
    response = S.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def paginated(url, params=None, max_pages=50):
    """Yield pages of a list endpoint, newest first, up to max_pages."""
    page = 1

    while page <= max_pages:
        options = dict(params or {})
        options.update({"per_page": 100, "page": page})

        batch = get(url, options).json()

        if not isinstance(batch, list) or not batch:
            break

        yield batch

        if len(batch) < 100:
            break

        page += 1


def parse_date(value):
    """'2026-11-18T00:54:16Z' -> date. Returns None for bad input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).date()
    except ValueError:
        return None


def is_mine_by_login(item_user):
    login = (item_user or {}).get("login") or ""
    return login.casefold() == USER.casefold()


# ---------------------------------------------------------------------------
# Repositories, user profile, languages
# ---------------------------------------------------------------------------


def repositories():
    result = []
    for batch in paginated(f"{API}/users/{USER}/repos", {"type": "owner"}):
        result.extend(batch)
    return result


def account_info():
    return get(f"{API}/users/{USER}").json()


def languages_of(repos):
    languages = {}
    for repo in repos:
        for name, amount in get(repo["languages_url"]).json().items():
            languages[name] = languages.get(name, 0) + int(amount)
    return languages


def branch_names(repo):
    names = []
    for batch in paginated(
        f"{API}/repos/{USER}/{repo['name']}/branches", max_pages=1
    ):
        for branch in batch:
            if branch.get("name"):
                names.append(branch["name"])
    return names[:20]


# ---------------------------------------------------------------------------
# Activity counting (commits, issues, PRs)
# ---------------------------------------------------------------------------


class Activity:
    """Deduplicated activity: commit SHAs, issue/PR ids, per-day counts."""

    def __init__(self):
        self.commit_shas = set()
        self.issue_ids = set()
        self.days = {}  # date -> number of actions that day
        self.commits = 0
        self.issues = 0
        self.pull_requests = 0
        self.known_emails = set()
        self.known_names = {"unknuw", "unknuwprojects"}

    def add_day(self, day):
        if day:
            self.days[day] = self.days.get(day, 0) + 1

    def add_commit(self, sha, day, git_author):
        if not sha or sha in self.commit_shas:
            return
        self.commit_shas.add(sha)
        self.commits += 1
        self.add_day(day)
        email = (git_author or {}).get("email")
        if email:
            self.known_emails.add(email.casefold())
        name = (git_author or {}).get("name")
        if name:
            self.known_names.add(name.casefold())

    def add_issue_item(self, item_id, day, is_pull_request):
        if not item_id or item_id in self.issue_ids:
            return
        self.issue_ids.add(item_id)
        if is_pull_request:
            self.pull_requests += 1
        else:
            self.issues += 1
        self.add_day(day)

    def totals(self):
        all_time = self.commits + self.issues + self.pull_requests
        last_365 = sum(
            count for day, count in self.days.items() if day >= WINDOW_START
        )
        return all_time, last_365


def collect_commits(repos, activity):
    """Primary pass: commits authored by the account, all branches."""
    for repo in repos:
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        for branch in branch_names(repo) or [repo.get("default_branch")]:
            if not branch:
                continue
            for batch in paginated(
                f"{repo_url}/commits",
                {"sha": branch, "author": USER},
                max_pages=20,
            ):
                for commit in batch:
                    details = commit.get("commit") or {}
                    git_author = details.get("author") or {}
                    activity.add_commit(
                        commit.get("sha"),
                        parse_date(git_author.get("date")),
                        git_author,
                    )


def collect_unlinked_commits(repos, activity):
    """
    Fallback pass: default-branch commits whose GitHub login is not the
    account (unlinked email) but whose git author name/email matches the
    identity seen in the primary pass. Capped to the 300 newest commits
    per repository; the primary pass already covers the normal case.
    """
    for repo in repos:
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        for batch in paginated(f"{repo_url}/commits", max_pages=3):
            for commit in batch:
                if commit.get("sha") in activity.commit_shas:
                    continue
                if is_mine_by_login(commit.get("author")):
                    continue  # already covered by the primary pass
                details = commit.get("commit") or {}
                git_author = details.get("author") or {}
                email = (git_author.get("email") or "").casefold()
                name = (git_author.get("name") or "").casefold()
                if email in activity.known_emails or name in activity.known_names:
                    activity.add_commit(
                        commit.get("sha"),
                        parse_date(git_author.get("date")),
                        git_author,
                    )


def collect_own_issues_and_prs(repos, activity):
    """Issues/PRs created by the account in its own repositories."""
    for repo in repos:
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        for batch in paginated(
            f"{repo_url}/issues", {"state": "all", "creator": USER}
        ):
            for item in batch:
                activity.add_issue_item(
                    item.get("id"),
                    parse_date(item.get("created_at")),
                    "pull_request" in item,
                )


def collect_external_issues_and_prs(activity):
    """
    Issues/PRs created by the account anywhere on GitHub, including
    other people's repositories. Best effort: if Search is unavailable
    or rate-limited, the per-repository counts above still stand.
    """
    for kind in ("issue", "pr"):
        try:
            response = get(
                f"{API}/search/issues",
                {
                    "q": f"author:{USER} type:{kind}",
                    "per_page": 100,
                    "page": 1,
                },
            ).json()
        except requests.HTTPError as exc:
            print(f"Search API unavailable for type:{kind} ({exc}); skipping.")
            continue
        total = response.get("total_count", 0)
        items = response.get("items", [])
        print(
            f"Search author:{USER} type:{kind}: {total} total, "
            f"{len(items)} returned."
        )
        for item in items:
            activity.add_issue_item(
                item.get("id"),
                parse_date(item.get("created_at")),
                kind == "pr",
            )


# ---------------------------------------------------------------------------
# Contributors (people, not contributions)
# ---------------------------------------------------------------------------


def unique_contributors(repos):
    """
    Count unique GitHub accounts across the original (non-fork)
    repositories: commit authors, issue/PR authors, PR reviewers.
    Anonymous contributors are excluded.
    """
    people = set()

    def add(login):
        if login:
            people.add(login.casefold())

    for repo in repos:
        if repo.get("private") or repo.get("fork"):
            continue
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        print(f"Scanning contributors: {repo['name']}")

        for branch in branch_names(repo) or [repo.get("default_branch")]:
            if not branch:
                continue
            for batch in paginated(
                f"{repo_url}/commits", {"sha": branch}, max_pages=20
            ):
                for commit in batch:
                    add((commit.get("author") or {}).get("login"))

        for batch in paginated(f"{repo_url}/issues", {"state": "all"}):
            for item in batch:
                add((item.get("user") or {}).get("login"))
                if "pull_request" in item:
                    for review_batch in paginated(
                        f"{repo_url}/pulls/{item['number']}/reviews"
                    ):
                        for review in review_batch:
                            add((review.get("user") or {}).get("login"))

    return len(people)


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


def streaks(days, start_date):
    """Current + longest streak in days, from account creation to today.

    A day counts if it has at least one action. The current streak
    stays alive if the latest action was today or yesterday.
    """
    if not days:
        return 0, 0

    longest = run = 0
    cursor = start_date
    while cursor <= TODAY:
        if days.get(cursor, 0) > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        cursor += timedelta(days=1)

    cursor = TODAY
    if days.get(cursor, 0) == 0:
        cursor -= timedelta(days=1)
    current = 0
    while cursor >= start_date and days.get(cursor, 0) > 0:
        current += 1
        cursor -= timedelta(days=1)

    return current, longest


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_size(kb):
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def render(
    repos,
    originals,
    langs,
    followers,
    contributors,
    activity,
    created_date,
):
    total_bytes = sum(langs.values()) or 1
    top = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:6]

    ys = [456, 495, 534, 573, 612, 651]
    bars = [440, 479, 518, 557, 596, 635]
    colors = ["#75f0c2", "#64c9ff", "#b4a1ff", "#ffbd69", "#f4d36b", "#ff8d9b"]

    rows = []
    for i in range(6):
        if i < len(top):
            name, amount = top[i]
            pct = amount / total_bytes * 100
            label = html.escape(name[:18])
            value = f"{pct:.2f}%"
            width = max(3, 430 * pct / 100)
        else:
            label = "—"
            value = "—"
            width = 0
        rows.append(f"""
  <text x="84" y="{ys[i]}" class="white">{label}</text>
  <rect x="230" y="{bars[i]}" width="430" height="13" rx="6.5" fill="#2b3b50"/>
  <rect x="230" y="{bars[i]}" width="{width:.1f}" height="13" rx="6.5" fill="{colors[i]}"/>
  <text x="680" y="{ys[i]-4}" class="white" text-anchor="end">{value}</text>
""")

    repo_count = len(repos)
    original_count = len(originals)
    fork_count = repo_count - original_count
    stars = sum(int(x.get("stargazers_count", 0)) for x in repos)
    forks = sum(int(x.get("forks_count", 0)) for x in repos)
    original_size = format_size(sum(int(x.get("size", 0)) for x in originals))

    total_all, total_last_365 = activity.totals()
    current, longest = streaks(activity.days, created_date)
    created_label = created_date.strftime("%d %b %Y").upper()
    today = NOW.strftime("%d %b %Y").upper()

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="850" viewBox="0 0 1200 850" role="img" aria-labelledby="title desc">

<title id="title">UnknuwProjects GitHub Overview</title>
<desc id="desc">Live GitHub dashboard with repositories, contributions, languages and contribution streaks.</desc>

<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#101b2b"/>
    <stop offset="1" stop-color="#111326"/>
  </linearGradient>

  <linearGradient id="accent">
    <stop stop-color="#75f0c2"/>
    <stop offset="1" stop-color="#64c9ff"/>
  </linearGradient>

  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
    <path d="M32 0H0V32" fill="none" stroke="#9bb2ca" stroke-opacity=".07"/>
  </pattern>

  <style>
    .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
    .sans{{font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
    .muted{{fill:#91a4bb}}
    .white{{fill:#edf5ff}}
    .label{{font-size:14px;letter-spacing:1.5px;fill:#91a4bb}}
    .value{{font-size:31px;font-weight:700;fill:#f2f7ff}}
    .panel{{fill:#172438;stroke:#2a3b52;stroke-width:1}}
  </style>
</defs>

<rect width="1200" height="850" rx="26" fill="url(#bg)"/>
<rect width="1200" height="850" rx="26" fill="url(#grid)"/>

<path d="M0 150H1200" stroke="#33445b"/>
<path d="M0 0H1200V8H0Z" fill="url(#accent)"/>

<g transform="translate(54 48)">
  <circle cx="24" cy="24" r="23" fill="#1e3446" stroke="#75f0c2" stroke-width="1.5"/>

  <path d="M13 25l8-9 7 7 8-8M14 34h20"
        fill="none" stroke="#75f0c2" stroke-width="2.5" stroke-linecap="round"/>

  <text x="66" y="20" class="sans white" font-size="25" font-weight="750" letter-spacing="1.5">UNKNUWPROJECTS</text>
  <text x="67" y="47" class="mono muted" font-size="13">SYSTEMS / TOOLS / EXPERIMENTS</text>

  <rect x="900" y="4" width="190" height="34" rx="17" fill="#173b39" stroke="#2c756b"/>
  <circle cx="920" cy="21" r="4" fill="#75f0c2"/>

  <text x="934" y="26" class="mono" fill="#9ef2d4" font-size="12">BUILDING IN PUBLIC</text>
  <text x="900" y="57" class="mono muted" font-size="11">LIVE PROFILE · {today}</text>
</g>

<g>
  <rect x="54" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="330" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="606" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="882" y="180" width="264" height="132" rx="16" class="panel"/>

  <g class="mono">
    <text x="78" y="216" class="label">PUBLIC REPOSITORIES</text>
    <text x="78" y="267" class="value">{repo_count:,}</text>
    <text x="78" y="291" class="muted" font-size="12">{original_count} original · {fork_count} forks · {original_size}</text>

    <text x="354" y="216" class="label">STARS RECEIVED</text>
    <text x="354" y="267" class="value">{stars:,}</text>
    <text x="354" y="291" class="muted" font-size="12">Across all repositories</text>

    <text x="630" y="216" class="label">FOLLOWERS</text>
    <text x="630" y="267" class="value">{followers:,}</text>
    <text x="630" y="291" class="muted" font-size="12">People following the profile</text>

    <text x="906" y="216" class="label">FORKS RECEIVED</text>
    <text x="906" y="267" class="value">{forks:,}</text>
    <text x="906" y="291" class="muted" font-size="12">Across all repositories</text>
  </g>
</g>

<rect x="54" y="340" width="690" height="450" rx="18" class="panel"/>

<text x="84" y="382" class="sans white" font-size="19" font-weight="700">LANGUAGE FOOTPRINT</text>
<text x="84" y="407" class="mono muted" font-size="12">Share of code across original repositories</text>

<g class="mono" font-size="14">
{''.join(rows)}
</g>

<rect x="766" y="340" width="380" height="450" rx="18" class="panel"/>

<text x="796" y="382" class="sans white" font-size="19" font-weight="700">CONTRIBUTION PULSE</text>
<text x="796" y="407" class="mono muted" font-size="12">Since {created_label} · commits + issues + PRs</text>

<g class="mono">

  <text x="796" y="452" class="muted" font-size="12">ALL-TIME CONTRIBUTIONS</text>
  <text x="1110" y="452" class="white" font-size="20" text-anchor="end">{total_all:,}</text>

  <path d="M796 466H1116" stroke="#30435a"/>

  <text x="796" y="493" class="muted" font-size="12">LAST 365 DAYS</text>
  <text x="1110" y="493" fill="#9ef2d4" font-size="20" text-anchor="end">{total_last_365:,}</text>

  <path d="M796 507H1116" stroke="#30435a"/>

  <text x="796" y="534" class="muted" font-size="12">CURRENT STREAK</text>
  <text x="1110" y="534" fill="#9ef2d4" font-size="20" text-anchor="end">{current} days</text>

  <path d="M796 548H1116" stroke="#30435a"/>

  <text x="796" y="575" class="muted" font-size="12">LONGEST STREAK</text>
  <text x="1110" y="575" class="white" font-size="20" text-anchor="end">{longest} days</text>

  <path d="M796 589H1116" stroke="#30435a"/>

  <text x="796" y="616" class="muted" font-size="12">COMMITS</text>
  <text x="1110" y="616" class="white" font-size="20" text-anchor="end">{activity.commits:,}</text>

  <path d="M796 630H1116" stroke="#30435a"/>

  <text x="796" y="657" class="muted" font-size="12">ISSUES OPENED</text>
  <text x="1110" y="657" class="white" font-size="20" text-anchor="end">{activity.issues:,}</text>

  <path d="M796 671H1116" stroke="#30435a"/>

  <text x="796" y="698" class="muted" font-size="12">PULL REQUESTS OPENED</text>
  <text x="1110" y="698" class="white" font-size="20" text-anchor="end">{activity.pull_requests:,}</text>

  <path d="M796 712H1116" stroke="#30435a"/>

  <text x="796" y="739" class="muted" font-size="12">CONTRIBUTORS (PEOPLE)</text>
  <text x="1110" y="739" class="white" font-size="20" text-anchor="end">{contributors:,}</text>

</g>

<text x="54" y="824" class="mono muted" font-size="11">LIVE DATA VIA GITHUB REST API / GENERATED BY GITHUB ACTIONS</text>
<text x="1146" y="824" class="mono muted" font-size="11" text-anchor="end">UNKNUW · {today}</text>

</svg>"""


def main():
    info = account_info()
    created_date = parse_date(info.get("created_at")) or TODAY
    followers = int(info.get("followers", 0))
    if info.get("name"):
        print(f"Account: {USER} ({info['name']}), created {created_date}")

    repos = repositories()
    public = [r for r in repos if not r.get("private")]
    originals = [r for r in public if not r.get("fork")]
    print(
        f"Repositories: {len(public)} public "
        f"({len(originals)} original, {len(public) - len(originals)} forks)"
    )

    languages = languages_of(originals)

    activity = Activity()
    if info.get("name"):
        activity.known_names.add(info["name"].casefold())

    print("Counting commits (author filter, all branches)...")
    collect_commits(public, activity)
    print(
        f"  {activity.commits} commits, "
        f"{len(activity.known_emails)} known author email(s)."
    )

    print("Counting commits with unlinked emails (default branches)...")
    before = activity.commits
    collect_unlinked_commits(public, activity)
    print(f"  +{activity.commits - before} unlinked-email commits.")

    print("Counting issues and PRs in own repositories...")
    collect_own_issues_and_prs(public, activity)
    print("Counting issues and PRs anywhere on GitHub (Search API)...")
    collect_external_issues_and_prs(activity)

    print("Counting contributors (people) in original repositories...")
    contributor_total = unique_contributors(originals)

    svg = render(
        public,
        originals,
        languages,
        followers,
        contributor_total,
        activity,
        created_date,
    )
    Path("metrics.svg").write_text(svg, encoding="utf-8")

    total_all, total_last_365 = activity.totals()
    current, longest = streaks(activity.days, created_date)
    print(
        f"Updated {USER}: {len(public)} public repos; "
        f"{total_all} all-time contributions "
        f"({activity.commits} commits, {activity.issues} issues, "
        f"{activity.pull_requests} PRs); "
        f"{total_last_365} in the last 365 days; "
        f"{contributor_total} contributors; "
        f"current streak {current} days; longest streak {longest} days."
    )


if __name__ == "__main__":
    main()
