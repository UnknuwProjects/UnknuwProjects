"""
Generate the UnknuwProjects overview card (metrics.svg).

Data comes from the REST API. The old GraphQL version returned 0
contributions for this account, so it was dropped.

- Commits are counted per repo and branch, filtered by GitHub
  login. A second pass over each default branch catches commits
  made with an email that is not linked to the account.
- Issues and PRs are counted in my own repos, and in the parent
  repos of my forks.
- Contributors means people: unique accounts that committed,
  opened issues or PRs, or reviewed PRs in original repos.

Two windows: all-time (since the account was created) and the
last 365 days. They match until the account is a year old.

Set a read-only token as the METRICS_TOKEN secret so private
repos are counted too. The default GITHUB_TOKEN only sees
public repos, which makes the numbers too low.
"""

import html
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

USER = os.getenv("METRICS_USER", "UnknuwProjects")
TOKEN = os.getenv("METRICS_TOKEN") or os.getenv("GITHUB_TOKEN")

if not TOKEN:
    raise SystemExit("Missing token: set METRICS_TOKEN or GITHUB_TOKEN.")

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
    # With a personal token, /user/repos also returns private repos.
    # The default Actions token cannot use it, so fall back to the
    # public list when no personal token is set.
    if os.getenv("METRICS_TOKEN"):
        url = f"{API}/user/repos"
        params = {"affiliation": "owner"}
    else:
        url = f"{API}/users/{USER}/repos"
        params = {"type": "owner"}
    result = []
    for batch in paginated(url, params):
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
        # The second name/email pair is an old local identity used
        # before the git config was fixed. Those commits are mine,
        # so they count.
        self.known_emails = {"yukine@muse.local"}
        self.known_names = {"unknuw", "unknuwprojects", "yukine (muse)"}

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
    """Primary pass: commits authored by the account, all branches.

    The commits are filtered by GitHub login on this side. The API's
    own author parameter is not used: it returns 0 for repos whose
    commits use the ID form of the noreply email, which silently
    dropped whole repos (THPS4-Android lost 140 commits that way).
    """
    for repo in repos:
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        for branch in branch_names(repo) or [repo.get("default_branch")]:
            if not branch:
                continue
            for batch in paginated(
                f"{repo_url}/commits",
                {"sha": branch},
                max_pages=20,
            ):
                for commit in batch:
                    if not is_mine_by_login(commit.get("author")):
                        continue
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
    account (unlinked email) but whose git author name/email matches a
    known identity of mine. Capped to the 300 newest commits per
    repository; the primary pass covers the normal case.
    """
    for repo in repos:
        repo_url = f"{API}/repos/{USER}/{repo['name']}"
        for batch in paginated(f"{repo_url}/commits", max_pages=3):
            for commit in batch:
                if commit.get("sha") in activity.commit_shas:
                    continue
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


def collect_upstream_issues_and_prs(repos, activity):
    """
    Issues/PRs created by the account in other people's repositories.

    Contributions upstream almost always start from a fork, so scan
    the parent repository of each fork with the same `creator`
    filter used for the account's own repositories. The issues
    listing includes pull requests, so one endpoint covers both.
    Repositories whose parent cannot be read (renamed/deleted) are
    skipped; items are deduplicated by id against all other sources.
    """
    parents = {}
    for repo in repos:
        if not repo.get("fork"):
            continue
        try:
            details = get(f"{API}/repos/{USER}/{repo['name']}").json()
        except requests.HTTPError as exc:
            print(f"  cannot read fork {repo['name']} ({exc}); skipping.")
            continue
        parent = (details.get("parent") or {}).get("full_name")
        if parent:
            parents[parent.casefold()] = parent

    print(f"  {len(parents)} unique parent repositories to scan.")
    for parent in sorted(parents.values()):
        try:
            seen_before = len(activity.issue_ids)
            for batch in paginated(
                f"{API}/repos/{parent}/issues",
                {"state": "all", "creator": USER},
            ):
                for item in batch:
                    activity.add_issue_item(
                        item.get("id"),
                        parse_date(item.get("created_at")),
                        "pull_request" in item,
                    )
            found = len(activity.issue_ids) - seen_before
            if found:
                print(f"  {parent}: {found} new issue(s)/PR(s).")
        except requests.HTTPError as exc:
            print(f"  cannot scan {parent} ({exc}); skipping.")


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
        if repo.get("fork"):
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
    # The colours GitHub itself uses for each language.
    lang_colors = {
        "C++": "#f34b7d",
        "C": "#a8b9cc",
        "Java": "#b07219",
        "Kotlin": "#a97bff",
        "Python": "#3572a5",
        "JavaScript": "#f1e05a",
        "TypeScript": "#3178c6",
        "Shell": "#89e051",
        "CMake": "#da3434",
        "Rust": "#dea584",
        "Go": "#00add8",
    }
    fallback_colors = ["#7ee787", "#79c0ff", "#d2a8ff", "#ffa657", "#f778ba", "#a5d6ff"]

    rows = []
    for i in range(6):
        if i < len(top):
            name, amount = top[i]
            pct = amount / total_bytes * 100
            label = html.escape(name[:18])
            value = f"{pct:.2f}%"
            width = max(3, 430 * pct / 100)
            color = lang_colors.get(name, fallback_colors[i])
        else:
            label = "—"
            value = "—"
            width = 0
            color = fallback_colors[i]
        rows.append(f"""
  <text x="84" y="{ys[i]}" class="white">{label}</text>
  <rect x="230" y="{bars[i]}" width="430" height="13" rx="6.5" fill="#26292d"/>
  <rect x="230" y="{bars[i]}" width="{width:.1f}" height="13" rx="6.5" fill="{color}"/>
  <text x="680" y="{ys[i]-4}" class="white" text-anchor="end">{value}</text>
""")

    repo_count = len(repos)
    original_count = len(originals)
    fork_count = repo_count - original_count
    private_count = sum(1 for x in repos if x.get("private"))
    stars = sum(int(x.get("stargazers_count", 0)) for x in repos)
    forks = sum(int(x.get("forks_count", 0)) for x in repos)

    total_all, total_last_365 = activity.totals()
    current, longest = streaks(activity.days, created_date)
    created_label = created_date.strftime("%d %b %Y").upper()
    today = NOW.strftime("%d %b %Y").upper()

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="850" viewBox="0 0 1200 850" role="img" aria-labelledby="title desc">

<title id="title">UnknuwProjects GitHub Overview</title>
<desc id="desc">Live GitHub dashboard with repositories, contributions, languages and contribution streaks.</desc>

<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#050607"/>
    <stop offset="1" stop-color="#0c0d0f"/>
  </linearGradient>

  <linearGradient id="accent">
    <stop stop-color="#7ee787"/>
    <stop offset="1" stop-color="#7ee787"/>
  </linearGradient>

  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
    <path d="M32 0H0V32" fill="none" stroke="#ffffff" stroke-opacity=".05"/>
  </pattern>

  <style>
    .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
    .sans{{font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
    .muted{{fill:#8b949e}}
    .white{{fill:#e6edf3}}
    .label{{font-size:14px;letter-spacing:1.5px;fill:#8b949e}}
    .value{{font-size:31px;font-weight:700;fill:#f0f6fc}}
    .panel{{fill:#101214;stroke:#26292d;stroke-width:1}}
  </style>
</defs>

<rect width="1200" height="850" rx="26" fill="url(#bg)"/>
<rect width="1200" height="850" rx="26" fill="url(#grid)"/>

<path d="M0 150H1200" stroke="#232629"/>
<path d="M0 0H1200V8H0Z" fill="url(#accent)"/>

<g transform="translate(54 48)">
  <circle cx="24" cy="24" r="23" fill="#101214" stroke="#7ee787" stroke-width="1.5"/>

  <path d="M13 25l8-9 7 7 8-8M14 34h20"
        fill="none" stroke="#7ee787" stroke-width="2.5" stroke-linecap="round"/>

  <text x="66" y="20" class="sans white" font-size="25" font-weight="750" letter-spacing="1.5">UNKNUWPROJECTS</text>
  <text x="67" y="47" class="mono muted" font-size="13">SYSTEMS / TOOLS / EXPERIMENTS</text>

  <rect x="900" y="4" width="190" height="34" rx="17" fill="#0b120c" stroke="#2c4a34"/>
  <circle cx="920" cy="21" r="4" fill="#7ee787"/>

  <text x="934" y="26" class="mono" fill="#7ee787" font-size="12">BUILDING IN PUBLIC</text>
  <text x="900" y="57" class="mono muted" font-size="11">LIVE PROFILE · {today}</text>
</g>

<g>
  <rect x="54" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="330" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="606" y="180" width="258" height="132" rx="16" class="panel"/>
  <rect x="882" y="180" width="264" height="132" rx="16" class="panel"/>

  <g class="mono">
    <text x="78" y="216" class="label">REPOSITORIES</text>
    <text x="78" y="267" class="value">{repo_count:,}</text>
    <text x="78" y="291" class="muted" font-size="11" textLength="230" lengthAdjust="spacingAndGlyphs">{original_count} original · {fork_count} forks · {private_count} private</text>

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

  <path d="M796 466H1116" stroke="#26292d"/>

  <text x="796" y="493" class="muted" font-size="12">LAST 365 DAYS</text>
  <text x="1110" y="493" fill="#7ee787" font-size="20" text-anchor="end">{total_last_365:,}</text>

  <path d="M796 507H1116" stroke="#26292d"/>

  <text x="796" y="534" class="muted" font-size="12">CURRENT STREAK</text>
  <text x="1110" y="534" fill="#7ee787" font-size="20" text-anchor="end">{current} days</text>

  <path d="M796 548H1116" stroke="#26292d"/>

  <text x="796" y="575" class="muted" font-size="12">LONGEST STREAK</text>
  <text x="1110" y="575" class="white" font-size="20" text-anchor="end">{longest} days</text>

  <path d="M796 589H1116" stroke="#26292d"/>

  <text x="796" y="616" class="muted" font-size="12">COMMITS</text>
  <text x="1110" y="616" class="white" font-size="20" text-anchor="end">{activity.commits:,}</text>

  <path d="M796 630H1116" stroke="#26292d"/>

  <text x="796" y="657" class="muted" font-size="12">ISSUES OPENED</text>
  <text x="1110" y="657" class="white" font-size="20" text-anchor="end">{activity.issues:,}</text>

  <path d="M796 671H1116" stroke="#26292d"/>

  <text x="796" y="698" class="muted" font-size="12">PULL REQUESTS OPENED</text>
  <text x="1110" y="698" class="white" font-size="20" text-anchor="end">{activity.pull_requests:,}</text>

  <path d="M796 712H1116" stroke="#26292d"/>

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
    originals = [r for r in repos if not r.get("fork")]
    print(
        f"Repositories: {len(repos)} total, {len(public)} public "
        f"({len(originals)} original incl. private, "
        f"{len(repos) - len(originals)} forks)"
    )

    languages = languages_of(originals)

    activity = Activity()
    if info.get("name"):
        activity.known_names.add(info["name"].casefold())

    print("Counting commits (login filter, all branches)...")
    collect_commits(repos, activity)
    print(
        f"  {activity.commits} commits, "
        f"{len(activity.known_emails)} known author email(s)."
    )

    print("Counting commits with unlinked emails (default branches)...")
    before = activity.commits
    collect_unlinked_commits(repos, activity)
    print(f"  +{activity.commits - before} unlinked-email commits.")

    print("Counting issues and PRs in own repositories...")
    collect_own_issues_and_prs(repos, activity)
    print("Counting issues and PRs in upstream (parent) repositories...")
    collect_upstream_issues_and_prs(repos, activity)

    print("Counting contributors (people) in original repositories...")
    contributor_total = unique_contributors(originals)

    svg = render(
        repos,
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
        f"Updated {USER}: {len(repos)} repos ({len(public)} public); "
        f"{total_all} all-time contributions "
        f"({activity.commits} commits, {activity.issues} issues, "
        f"{activity.pull_requests} PRs); "
        f"{total_last_365} in the last 365 days; "
        f"{contributor_total} contributors; "
        f"current streak {current} days; longest streak {longest} days."
    )


if __name__ == "__main__":
    main()
