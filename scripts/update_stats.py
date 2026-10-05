#!/usr/bin/env python3
"""Generate profile SVGs using GitHub's API. Python standard library only."""
import argparse
import collections
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login createdAt followers { totalCount }
    repositories(first: 50, after: $cursor, privacy: PUBLIC,
                 ownerAffiliations: [OWNER]) {
      totalCount pageInfo { hasNextPage endCursor }
      nodes {
        isFork stargazerCount
        languages(first: 100) { edges { size node { name color } } }
      }
    }
  }
}
"""
YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions totalIssueContributions
      totalPullRequestContributions
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def graphql(query, variables, token):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        "https://api.github.com/graphql", data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "k4gan-profile-stats"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if result.get("errors"):
                messages = "; ".join(e.get("message", "GraphQL error") for e in result["errors"])
                raise RuntimeError(messages)
            user = result.get("data", {}).get("user")
            if user is None:
                raise RuntimeError("GitHub user not found or unavailable")
            return user
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"GitHub API returned HTTP {error.code}") from None
        except urllib.error.URLError:
            if attempt == 2:
                raise RuntimeError("Unable to connect to GitHub API") from None
        time.sleep(2 ** attempt)


def streaks(days, today):
    """Calendar dates are from GitHub; UTC today has a grace day until it ends."""
    if not days:
        return 0, 0
    longest = run = 0
    day = min(dt.date.fromisoformat(d) for d in days)
    while day <= today:
        run = run + 1 if days.get(day.isoformat(), 0) else 0
        longest = max(longest, run)
        day += dt.timedelta(days=1)
    day = today if days.get(today.isoformat(), 0) else today - dt.timedelta(days=1)
    current = 0
    while days.get(day.isoformat(), 0):
        current += 1
        day -= dt.timedelta(days=1)
    return current, longest


def collect(login, token, now):
    cursor = None
    languages = collections.Counter()
    colors = {}
    stars = 0
    profile = None
    while True:
        page = graphql(PROFILE_QUERY, {"login": login, "cursor": cursor}, token)
        if profile is None:
            profile = page
        repos = page["repositories"]
        for repo in repos["nodes"]:
            stars += repo["stargazerCount"]
            if repo["isFork"]:
                continue
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                languages[name] += edge["size"]
                colors[name] = edge["node"]["color"] or "#58a6ff"
        if not repos["pageInfo"]["hasNextPage"]:
            break
        next_cursor = repos["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Invalid repository pagination cursor")
        cursor = next_cursor

    first_year = int(profile["createdAt"][:4])
    days = {}
    commits = issues = prs = 0
    for year in range(first_year, now.year + 1):
        # Inclusive, non-overlapping calendar-year windows, including leap years.
        start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
        end = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        data = graphql(YEAR_QUERY, {"login": login,
                       "from": start.isoformat(), "to": end.isoformat()}, token)
        contributions = data["contributionsCollection"]
        commits += contributions["totalCommitContributions"]
        issues += contributions["totalIssueContributions"]
        prs += contributions["totalPullRequestContributions"]
        for week in contributions["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                # Some API calendars include neighboring padding days.
                if start.date().isoformat() <= day["date"] <= end.date().isoformat():
                    days[day["date"]] = day["contributionCount"]
    today = now.date()
    since = (today - dt.timedelta(days=364)).isoformat()
    contributions_365 = sum(n for d, n in days.items() if since <= d <= today.isoformat())
    current, longest = streaks(days, today)
    return {"login": profile["login"], "repos": profile["repositories"]["totalCount"],
            "stars": stars, "followers": profile["followers"]["totalCount"],
            "commits": commits, "issues": issues, "prs": prs,
            "contributions": contributions_365, "current": current, "longest": longest,
            "days": days, "languages": languages, "colors": colors,
            "updated": now.strftime("%Y-%m-%d %H:%M UTC"), "pending": False}


def text(x, y, value, size=14, color="#8b949e", weight="400"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}">{html.escape(str(value))}</text>')


def rect(x, y, width, height, color, radius=10):
    return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{color}"/>'


def svg(body, height, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" '
            f'viewBox="0 0 1000 {height}" role="img" aria-labelledby="title">'
            f'<title id="title">{html.escape(title)}</title>'
            '<g font-family="Segoe UI,Helvetica,Arial,sans-serif">'
            + rect(0, 0, 1000, height, "#0d1117", 16) + body + '</g></svg>\n')


def number(value):
    return "—" if value is None else f"{value:,}"


def dashboard(data):
    body = text(30, 40, f'@{data["login"]} / GitHub overview', 23, "#e6edf3", "600")
    body += text(30, 64, "Waiting for first workflow run" if data["pending"]
                 else f'Updated {data["updated"]} · refresh scheduled every 30 minutes', 13)
    metrics = [("Public repositories", "repos"), ("Commit contributions · all years", "commits"),
               ("Stars · public repositories", "stars"), ("Followers", "followers"),
               ("Contributions · last 365 days", "contributions"), ("Pull requests · all years", "prs")]
    for i, (label, key) in enumerate(metrics):
        x, y = 30 + (i % 3) * 320, 87 + (i // 3) * 110
        body += rect(x, y, 300, 94, "#161b22")
        body += text(x + 18, y + 29, label, 13)
        body += text(x + 18, y + 73, number(data[key]), 35, "#2fdfb0", "650")
    footer = (f'Issues: {number(data["issues"])}'
              f'   ·   Current streak: {number(data["current"])} days'
              f'   ·   Longest streak: {number(data["longest"])} days')
    body += text(30, 325, footer, 14, "#c9d1d9")
    body += text(30, 348, "GitHub contribution rules apply. Calendar days use GitHub dates; streak cutoff is UTC.", 12)
    return svg(body, 370, "GitHub profile statistics for " + data["login"])


def activity(data, today):
    body = text(30, 39, "Contribution activity", 23, "#e6edf3", "600")
    body += text(30, 63, "Waiting for first workflow run" if data["pending"]
                 else f'{number(data["contributions"])} contributions in the last 365 days', 13)
    start = today - dt.timedelta(days=364)
    sunday = start - dt.timedelta(days=(start.weekday() + 1) % 7)
    for offset in range(365):
        day = start + dt.timedelta(days=offset)
        count = data["days"].get(day.isoformat(), 0)
        level = 0 if not count else 1 if count < 3 else 2 if count < 6 else 3 if count < 10 else 4
        color = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"][level]
        column = (day - sunday).days // 7
        row = (day.weekday() + 1) % 7
        if day.day == 1:
            body += text(65 + column * 16, 87, day.strftime("%b"), 11)
        body += (f'<g><title>{day.isoformat()}: '
                 f'{"awaiting data" if data["pending"] else str(count) + " contributions"}</title>'
                 + rect(65 + column * 16, 98 + row * 16, 13, 13, color, 3) + '</g>')
    for row, label in [(1, "Mon"), (3, "Wed"), (5, "Fri")]:
        body += text(30, 108 + row * 16, label, 11)
    body += text(30, 233, "Commits, pull requests, issues and other contributions counted by GitHub.", 12)
    return svg(body, 255, "Last 365 days of GitHub contributions")


def language_card(data):
    body = text(30, 40, "Languages across public repositories", 23, "#e6edf3", "600")
    body += text(30, 64, "Measured by source bytes · owned repositories · forks excluded", 13)
    entries = sorted(data["languages"].items(), key=lambda pair: (-pair[1], pair[0]))
    total = sum(n for _, n in entries)
    if not total:
        message = "Waiting for first workflow run" if data["pending"] else "No language data in public non-fork repositories."
        body += text(30, 120, message, 16, "#c9d1d9")
    else:
        top = entries[:9]
        remainder = sum(n for _, n in entries[9:])
        if remainder:
            top.append(("Other languages", remainder))
        position = 30.0
        for i, (name, size) in enumerate(top):
            candidate = data["colors"].get(name, "#8b949e")
            color = candidate if re.fullmatch(r"#[0-9a-fA-F]{6}", candidate) else "#58a6ff"
            width = size / total * 940
            body += rect(round(position, 2), 91, round(width, 2), 16, color, 0)
            position += width
            x, y = 30 + (i % 2) * 475, 140 + (i // 2) * 28
            body += rect(x, y - 10, 10, 10, color, 5)
            body += text(x + 21, y, f'{name}  {size / total:.1%}', 14, "#c9d1d9")
    return svg(body, 285, "Languages in public non-fork repositories")


def write_cards(data, now, output):
    # Fetch and render everything before modifying previous valid assets.
    cards = {"github-stats.svg": dashboard(data),
             "github-activity.svg": activity(data, now.date()),
             "github-languages.svg": language_card(data)}
    output.mkdir(parents=True, exist_ok=True)
    for name, contents in cards.items():
        target = output / name
        temporary = target.with_suffix(".svg.tmp")
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Generate honest initial placeholders without API calls")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    login = os.getenv("PROFILE_USERNAME", "k4gan")
    if args.init:
        data = dict.fromkeys(["repos", "stars", "followers", "commits", "issues", "prs",
                              "contributions", "current", "longest"])
        data.update(login=login, pending=True, days={}, languages={}, colors={}, updated="")
    else:
        token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required; run the GitHub Actions workflow")
        data = collect(login, token, now)
    write_cards(data, now, ROOT / "assets")
    print("Generated three GitHub profile SVG cards.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print request headers or tokens. Keep previous cards on API failure.
        print(f"Profile update failed: {error}", file=sys.stderr)
        sys.exit(1)
