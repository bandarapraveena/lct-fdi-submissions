#!/usr/bin/env python3
"""Move See-Something-Say-Something GitHub issues into pending_submissions.csv.

Two modes, both idempotent (an issue is logged at most once):

    python3 scripts/sync_submissions.py --issue 42     # one issue (issue-opened trigger)
    python3 scripts/sync_submissions.py --all-open      # every open issue (scheduled reconcile)

For each `submission`-labelled issue:
  * `[New project] ...`  -> append a `pending` row, add the `logged` label + a comment.
  * `[Update ...]` / `[Location ...]` -> it's a correction to an existing record, not a new
    project: add the `correction` label + a comment, and DO NOT touch the CSV.
  * placeholder / missing source URL -> add the `needs-source` label + a comment, skip the CSV.

The row is written lean and honest: only fields the issue form actually captured. HQ, city,
year and the true capex are left for a human to confirm at accept time — automation does not guess.

Fetching uses the `gh` CLI (present on GitHub-hosted runners; GITHUB_TOKEN in the env). For local
testing without gh, set SUBMISSIONS_ISSUES_JSON to a file holding the /issues API array; label and
comment calls become no-ops and the CSV is still written.
"""

import argparse
import csv
import datetime
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("SUBMISSIONS_REPO", "bandarapraveena/lct-fdi-submissions")
CSV_PATH = os.environ.get("SUBMISSIONS_CSV") or os.path.join(
    os.path.dirname(__file__), os.pardir, "pending_submissions.csv")
CSV_PATH = os.path.abspath(CSV_PATH)
COLUMNS = ["date_logged", "status", "technology", "company", "parent_hq",
           "destination_country", "destination_city", "year_announced",
           "capital_usd_m", "source", "notes"]

PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "localhost")
LOCAL_JSON = os.environ.get("SUBMISSIONS_ISSUES_JSON")  # set for offline testing


# ---- gh helpers (no-ops offline) -------------------------------------------------
def gh_json(path):
    if LOCAL_JSON:
        return json.load(open(LOCAL_JSON))
    out = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def gh_write(args):
    """Run a `gh` write command; skipped when testing offline."""
    if LOCAL_JSON:
        print("  [offline] would run: gh " + " ".join(args))
        return
    subprocess.run(["gh"] + args, check=True)


def add_label(num, label):
    gh_write(["issue", "edit", str(num), "--repo", REPO, "--add-label", label])


def comment(num, body):
    gh_write(["issue", "comment", str(num), "--repo", REPO, "--body", body])


# ---- issue parsing ---------------------------------------------------------------
def field(body, label):
    m = re.search(r"###\s*" + re.escape(label) + r"\s*\n+(.*?)(?=\n###\s|\Z)", body or "", re.S)
    if not m:
        return ""
    v = m.group(1).strip()
    return "" if v == "_No response_" else v


def url_in(text):
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(").,") if m else ""


def host_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    h = m.group(1).lower() if m else ""
    return h[4:] if h.startswith("www.") else h


def capital_number(raw):
    """Return a bare number only if the field is purely numeric; else '' (phrase kept in notes)."""
    raw = (raw or "").strip()
    return raw.replace(",", "") if re.match(r"^[\d,]+(\.\d+)?$", raw) else ""


def classify(issue):
    title = (issue.get("title") or "").strip()
    body = issue.get("body") or ""
    src = field(body, "Source link") or url_in(title) or url_in(body)
    low = title.lower()
    if low.startswith("[update") or low.startswith("[location") or "existing" in low:
        return "correction", src, None
    host = host_of(src)
    if not src or host in PLACEHOLDER_HOSTS or host.endswith(".example.com"):
        return "needs-source", src, None
    row = {
        "date_logged": datetime.date.today().isoformat(),
        "status": "pending",
        "technology": field(body, "Technology"),
        "company": field(body, "Company / parent"),
        "parent_hq": "",
        "destination_country": field(body, "Destination country"),
        "destination_city": "",
        "year_announced": "",
        "capital_usd_m": capital_number(field(body, "Capital investment (US$ millions, if known)")),
        "source": src,
    }
    caps = field(body, "Capital investment (US$ millions, if known)")
    note = f"Auto-logged from See-Something-Say-Something GitHub issue #{issue['number']}. HQ/city/year to confirm at triage."
    if field(body, "Anything else?"):
        note += " Submitter notes: " + re.sub(r"\s+", " ", field(body, "Anything else?")).strip()
    if caps and not row["capital_usd_m"]:
        note += f" Capital stated as '{caps}'."
    row["notes"] = note
    return "new", src, row


# ---- CSV state -------------------------------------------------------------------
def already_logged_numbers():
    nums = set()
    if os.path.exists(CSV_PATH):
        for r in csv.DictReader(open(CSV_PATH)):
            for m in re.finditer(r"issue #(\d+)", r.get("notes", "") or ""):
                nums.add(int(m.group(1)))
    return nums


def append_rows(rows):
    exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow(row)


# ---- main ------------------------------------------------------------------------
def process(issues):
    done = already_logged_numbers()
    new_rows, logged, corrections, needs_source, skipped = [], [], [], [], []
    for issue in issues:
        num = issue["number"]
        labels = {l["name"] for l in issue.get("labels", [])}
        if num in done or "logged" in labels or "correction" in labels or "needs-source" in labels:
            skipped.append(num)
            continue
        kind, src, row = classify(issue)
        if kind == "new":
            new_rows.append(row)
            logged.append(num)
        elif kind == "correction":
            corrections.append(num)
        else:
            needs_source.append(num)
    if new_rows:
        append_rows(new_rows)
    for num in logged:
        add_label(num, "logged")
        comment(num, "Logged to `pending_submissions.csv` as a pending submission — thank you! "
                     "A maintainer will review it for the main dataset.")
    for num in corrections:
        add_label(num, "correction")
        comment(num, "Thanks! This looks like a correction to an **existing** project rather than a "
                     "new one, so it wasn't added to the pending log. A maintainer will update the record directly.")
    for num in needs_source:
        add_label(num, "needs-source")
        comment(num, "Thanks! We couldn't find a usable source link, so this wasn't logged yet. "
                     "Please edit the issue to add a real source URL and we'll pick it up.")
    print(f"logged (new): {logged}")
    print(f"corrections:  {corrections}")
    print(f"needs-source: {needs_source}")
    print(f"skipped (already handled): {skipped}")
    return len(new_rows)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--issue", type=int, help="process a single issue number")
    g.add_argument("--all-open", action="store_true", help="process all open issues")
    args = ap.parse_args()
    if args.issue:
        issues = [gh_json(f"/repos/{REPO}/issues/{args.issue}")]
    else:
        issues = [i for i in gh_json(f"/repos/{REPO}/issues?state=open&per_page=100")
                  if "pull_request" not in i]
    n = process(issues)
    print(f"appended {n} row(s) to {os.path.basename(CSV_PATH)}")


if __name__ == "__main__":
    main()
