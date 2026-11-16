
import html
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import requests

USER = os.getenv("METRICS_USER", "UnknuwProjects")
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("METRICS_TOKEN")

if not TOKEN:
    raise SystemExit("Missing GITHUB_TOKEN or METRICS_TOKEN secret.")

API = "https://api.github.com"
NOW = datetime.now(timezone.utc)
CUTOFF = NOW.timestamp() - (365 * 24 * 60 * 60)

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


def get(url, params=None):
    response = S.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def paginated(url, params=None):
    page = 1

    while True:
        options = dict(params or {})
        options.update({
            "per_page": 100,
            "page": page
        })

        batch = get(url, options).json()

        if not isinstance(batch, list) or not batch:
            break

        yield batch

        if len(batch) < 100:
            break

        page += 1


def graphql(query, variables):
    response = S.post(
        API + "/graphql",
        json={
            "query": query,
            "variables": variables
        },
        timeout=45
    )

    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"GraphQL error: {data['errors']}")

    return data["data"]


def repositories():
    result = []

    for batch in paginated(
        f"{API}/users/{USER}/repos",
        {"type": "owner"}
    ):
        result.extend(batch)

    return result


def contribution_year(year):
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalIssueContributions
          totalPullRequestContributions
          totalCommitContributions
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """

    end = (
        NOW.isoformat().replace("+00:00", "Z")
        if year == NOW.year
        else f"{year}-12-31T23:59:59Z"
    )

    data = graphql(query, {
        "login": USER,
        "from": f"{year}-01-01T00:00:00Z",
        "to": end
    })

    if not data.get("user"):
        raise RuntimeError(f"GitHub user not found: {USER}")

    return data["user"]["contributionsCollection"]


def contribution_history():
    days = {}

    totals = {
        "commits": 0,
        "issues": 0,
        "pull_requests": 0
    }

    for year in range(2008, NOW.year + 1):
        item = contribution_year(year)

        for week in item["contributionCalendar"]["weeks"]:
            for day_item in week["contributionDays"]:
                days[day_item["date"]] = int(
                    day_item["contributionCount"]
                )

        totals["commits"] += int(
            item["totalCommitContributions"]
        )

        totals["issues"] += int(
            item["totalIssueContributions"]
        )

        totals["pull_requests"] += int(
            item["totalPullRequestContributions"]
        )

    return days, totals


def streaks(days):
    if not days:
        return 0, 0

    ordered = sorted(
        (date.fromisoformat(k), v)
        for k, v in days.items()
    )

    longest = run = 0
    previous = None

    for day, count in ordered:
        if count:
            run = (
                run + 1
                if previous and (day - previous).days == 1
                else 1
            )

            longest = max(longest, run)
            previous = day

        else:
            run = 0
            previous = None

    latest = ordered[-1][0]

    cursor = (
        latest
        if days[latest.isoformat()]
        else date.fromordinal(latest.toordinal() - 1)
    )

    current = 0

    while days.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor = date.fromordinal(cursor.toordinal() - 1)

    return current, longest


def unique_contributors(repos):
    """
    Count unique GitHub accounts across public, non-fork repositories.

    Activities:
    - Commits
    - Issue authors
    - Pull request authors
    - Pull request reviewers

    Returns:
    - All-time unique contributors
    - Unique contributors active in the last 365 days

    Anonymous contributors are excluded.
    """

    all_time = set()
    last_year = set()

    def add_contributor(login, timestamp=None):
        if not login:
            return

        login = login.casefold()

        all_time.add(login)

        if not timestamp:
            return

        try:
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp()

            if float(timestamp) >= CUTOFF:
                last_year.add(login)

        except (ValueError, TypeError, OverflowError):
            pass

    for repo in repos:
        if repo.get("private") or repo.get("fork"):
            continue

        repo_url = f"{API}/repos/{USER}/{repo['name']}"

        print(f"Scanning contributors: {repo['name']}")

        # 1. COMMITS FROM ALL BRANCHES

        branch_names = set()

        for batch in paginated(
            f"{repo_url}/branches"
        ):
            for branch in batch:
                if branch.get("name"):
                    branch_names.add(branch["name"])

        for branch_name in branch_names:
            for batch in paginated(
                f"{repo_url}/commits",
                {"sha": branch_name}
            ):
                for commit in batch:
                    author = commit.get("author") or {}

                    details = commit.get("commit") or {}
                    author_details = details.get("author") or {}

                    add_contributor(
                        author.get("login"),
                        author_details.get("date")
                    )

        # 2. ISSUES AND PULL REQUEST AUTHORS

        for batch in paginated(
            f"{repo_url}/issues",
            {"state": "all"}
        ):
            for item in batch:
                author = item.get("user") or {}

                add_contributor(
                    author.get("login"),
                    item.get("created_at")
                )

                # 3. PULL REQUEST REVIEWS

                if "pull_request" in item:
                    reviews_url = (
                        f"{repo_url}/pulls/"
                        f"{item['number']}/reviews"
                    )

                    for review_batch in paginated(
                        reviews_url
                    ):
                        for review in review_batch:
                            reviewer = review.get("user") or {}

                            add_contributor(
                                reviewer.get("login"),
                                review.get("submitted_at")
                            )

    return len(all_time), len(last_year)


def starred_count():
    response = get(
        f"{API}/users/{USER}/starred",
        {"per_page": 1, "page": 1}
    )

    match = re.search(
        r'[&?]page=(\d+)[^>]*>;\s*rel="last"',
        response.headers.get("Link", "")
    )

    if match:
        return int(match.group(1))

    return len(
        get(
            f"{API}/users/{USER}/starred",
            {"per_page": 100, "page": 1}
        ).json()
    )


def render(
    repos,
    langs,
    followers,
    starred,
    contributors,
    last_year_contributors,
    days,
    totals
):
    total_bytes = sum(langs.values()) or 1

    top = sorted(
        langs.items(),
        key=lambda x: x[1],
        reverse=True
    )[:6]

    ys = [456, 495, 534, 573, 612, 651]
    bars = [440, 479, 518, 557, 596, 635]

    colors = [
        "#75f0c2",
        "#64c9ff",
        "#b4a1ff",
        "#ffbd69",
        "#f4d36b",
        "#ff8d9b"
    ]

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

    stars = sum(
        int(x.get("stargazers_count", 0))
        for x in repos
    )

    forks = sum(
        int(x.get("forks_count", 0))
        for x in repos
    )

    size_gb = sum(
        int(x.get("size", 0))
        for x in repos
    ) / (1024 * 1024)

    current, longest = streaks(days)

    today = NOW.strftime("%d %b %Y").upper()

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="850" viewBox="0 0 1200 850" role="img" aria-labelledby="title desc">

<title id="title">UnknuwProjects GitHub Overview</title>
<desc id="desc">Live GitHub dashboard with repositories, contributors, languages and contribution streaks.</desc>

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
    <text x="78" y="291" class="muted" font-size="12">{size_gb:.1f} GB total repository size</text>

    <text x="354" y="216" class="label">STARS RECEIVED</text>
    <text x="354" y="267" class="value">{stars:,}</text>
    <text x="354" y="291" class="muted" font-size="12">Across public repositories</text>

    <text x="630" y="216" class="label">FOLLOWERS</text>
    <text x="630" y="267" class="value">{followers:,}</text>
    <text x="630" y="291" class="muted" font-size="12">People following the profile</text>

    <text x="906" y="216" class="label">FORKS RECEIVED</text>
    <text x="906" y="267" class="value">{forks:,}</text>
    <text x="906" y="291" class="muted" font-size="12">Across public repositories</text>
  </g>
</g>

<rect x="54" y="340" width="690" height="450" rx="18" class="panel"/>

<text x="84" y="382" class="sans white" font-size="19" font-weight="700">LANGUAGE FOOTPRINT</text>
<text x="84" y="407" class="mono muted" font-size="12">Share of code across public repositories</text>

<g class="mono" font-size="14">
{''.join(rows)}
</g>

<rect x="766" y="340" width="380" height="450" rx="18" class="panel"/>

<text x="796" y="382" class="sans white" font-size="19" font-weight="700">CONTRIBUTION PULSE</text>
<text x="796" y="407" class="mono muted" font-size="12">All-time activity · GitHub contribution calendar</text>

<g class="mono">

  <text x="796" y="452" class="muted" font-size="12">ALL-TIME CONTRIBUTORS</text>
  <text x="1110" y="452" class="white" font-size="20" text-anchor="end">{contributors:,}</text>

  <path d="M796 466H1116" stroke="#30435a"/>

  <text x="796" y="493" class="muted" font-size="12">LAST YEAR CONTRIBUTORS</text>
  <text x="1110" y="493" fill="#9ef2d4" font-size="20" text-anchor="end">{last_year_contributors:,}</text>

  <path d="M796 507H1116" stroke="#30435a"/>

  <text x="796" y="534" class="muted" font-size="12">CURRENT STREAK</text>
  <text x="1110" y="534" fill="#9ef2d4" font-size="20" text-anchor="end">{current} days</text>

  <path d="M796 548H1116" stroke="#30435a"/>

  <text x="796" y="575" class="muted" font-size="12">LONGEST STREAK</text>
  <text x="1110" y="575" class="white" font-size="20" text-anchor="end">{longest} days</text>

  <path d="M796 589H1116" stroke="#30435a"/>

  <text x="796" y="616" class="muted" font-size="12">COMMITS</text>
  <text x="1110" y="616" class="white" font-size="20" text-anchor="end">{totals["commits"]:,}</text>

  <path d="M796 630H1116" stroke="#30435a"/>

  <text x="796" y="657" class="muted" font-size="12">ISSUE CONTRIBUTIONS</text>
  <text x="1110" y="657" class="white" font-size="20" text-anchor="end">{totals["issues"]:,}</text>

  <path d="M796 671H1116" stroke="#30435a"/>

  <text x="796" y="698" class="muted" font-size="12">PULL REQUEST CONTRIBUTIONS</text>
  <text x="1110" y="698" class="white" font-size="20" text-anchor="end">{totals["pull_requests"]:,}</text>

  <path d="M796 712H1116" stroke="#30435a"/>

  <text x="796" y="739" class="muted" font-size="12">STARRED REPOSITORIES</text>
  <text x="1110" y="739" class="white" font-size="20" text-anchor="end">{starred:,}</text>

</g>

<text x="54" y="824" class="mono muted" font-size="11">LIVE DATA VIA GITHUB API / GENERATED BY GITHUB ACTIONS</text>
<text x="1146" y="824" class="mono muted" font-size="11" text-anchor="end">UNKNUW · {today}</text>

</svg>"""


def main():
    repos = repositories()

    public = [
        repo for repo in repos
        if not repo.get("private") and not repo.get("fork")
    ]

    followers = int(
        get(f"{API}/users/{USER}").json().get("followers", 0)
    )

    languages = {}

    for repo in public:
        for name, amount in get(repo["languages_url"]).json().items():
            languages[name] = (
                languages.get(name, 0) + int(amount)
            )

    days, totals = contribution_history()

    print("Counting all-time and last-year contributors...")

    contributor_total, last_year_contributor_total = (
        unique_contributors(public)
    )

    starred = starred_count()

    svg = render(
        public,
        languages,
        followers,
        starred,
        contributor_total,
        last_year_contributor_total,
        days,
        totals
    )

    Path("metrics.svg").write_text(
        svg,
        encoding="utf-8"
    )

    current, longest = streaks(days)

    print(
        f"Updated {USER}: {len(public)} public repos; "
        f"{contributor_total} all-time contributors; "
        f"{last_year_contributor_total} last-year contributors; "
        f"current streak {current} days; "
        f"longest streak {longest} days."
    )


if __name__ == "__main__":
    main()
