#!/usr/bin/env python3
"""Move See-Something-Say-Something GitHub issues into pending_submissions.csv.

The CSV mirrors the main dataset's columns (FDI_All_ToSource.xlsx 'All' sheet:
Sector … Operational) so an accepted row lifts into the dataset 1:1, plus three
trailing housekeeping columns (submission_status, date_logged, submission_notes)
that are dropped on merge. The file is written UTF-8 with a BOM so Excel on macOS
renders accented city names correctly.

Two modes, both idempotent (an issue is logged at most once):

    python3 scripts/sync_submissions.py --issue 42     # one issue (issue-opened trigger)
    python3 scripts/sync_submissions.py --all-open      # every open issue (scheduled reconcile)

For each `submission`-labelled issue:
  * `[New project] ...`  -> append a lean `pending` row (main-dataset columns filled
    only where the issue form gives them; Project Status / Year Completed / JV / HQ
    left blank for a human sourcing pass), then label `logged` + comment.
  * `[Update ...]` / `[Location ...]` -> a correction to an existing record: label
    `correction` + comment, do NOT touch the CSV.
  * placeholder / missing source URL -> label `needs-source` + comment, skip the CSV.

Fetching uses the `gh` CLI (present on GitHub-hosted runners; GITHUB_TOKEN in env).
For local testing without gh, set SUBMISSIONS_ISSUES_JSON to a file holding the
/issues API array; label/comment calls become no-ops and the CSV is still written.
"""

import argparse
import csv
import datetime
import json
import os
import re
import subprocess

REPO = os.environ.get("SUBMISSIONS_REPO", "bandarapraveena/lct-fdi-submissions")
CSV_PATH = os.environ.get("SUBMISSIONS_CSV") or os.path.join(
    os.path.dirname(__file__), os.pardir, "pending_submissions.csv")
CSV_PATH = os.path.abspath(CSV_PATH)

MAIN = ["Sector", "Project ID", "Parent company", "Company Country (HQ)", "Project Status",
        "Destination Country", "Destination city", "Year Announced", "Year Completed",
        "Technology", "Industry activity", "Capital Investment (US$m)", "Expansion",
        "Joint Venture", "Joint Venture Local", "Joint Venture Company",
        "Source (Parent company name and HQ)", "Source (Project Status)",
        "Source (Project location, country and city)", "Source (Amount)",
        "Source (Ownership share, where available)", "Source (Date Announced [MM/DD/YYYY])",
        "Source (Date Completed [MM/DD/YYYY])", "Source (Industry Activity)",
        "Source (Joint Venture)", "Operational"]
HOUSE = ["submission_status", "date_logged", "submission_notes"]
COLUMNS = MAIN + HOUSE

PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "localhost")
LOCAL_JSON = os.environ.get("SUBMISSIONS_ISSUES_JSON")  # set for offline testing
RESOLVED_PATH = os.environ.get("SUBMISSIONS_RESOLVED") or os.path.join(
    os.path.dirname(CSV_PATH), "resolved_issues.csv")


# ---- gh helpers (no-ops offline) -------------------------------------------------
def gh_json(path):
    if LOCAL_JSON:
        return json.load(open(LOCAL_JSON))
    out = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def gh_write(args):
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

    tech = field(body, "Technology")
    caps = field(body, "Capital investment (US$ millions, if known)")
    cap = capital_number(caps)
    note = (f"Auto-logged from See-Something-Say-Something GitHub issue #{issue['number']}. "
            f"HQ / Project Status / Year / JV / Year Completed to confirm at triage.")
    if field(body, "Anything else?"):
        note += " Submitter notes: " + re.sub(r"\s+", " ", field(body, "Anything else?")).strip()
    if caps and not cap:
        note += f" Capital stated as '{caps}'."

    row = {c: "" for c in COLUMNS}
    row.update({
        "Sector": tech,
        "Technology": tech,
        "Destination Country": field(body, "Destination country"),
        "Industry activity": "Manufacturing",
        "Capital Investment (US$m)": cap,
        "Expansion": 0,
        "Source (Parent company name and HQ)": src,
        "Source (Project Status)": src,
        "Source (Project location, country and city)": src,
        "Source (Amount)": src if cap else "",
        "Source (Date Announced [MM/DD/YYYY])": src,
        "submission_status": "pending",
        "date_logged": datetime.date.today().isoformat(),
        "submission_notes": note,
    })
    # the issue form's "Company / parent" -> Parent company
    row["Parent company"] = field(body, "Company / parent")
    return "new", src, row


# ---- CSV state -------------------------------------------------------------------
def already_logged_numbers():
    nums = set()
    if os.path.exists(CSV_PATH):
        for r in csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")):
            for m in re.finditer(r"issue #(\d+)", r.get("submission_notes", "") or ""):
                nums.add(int(m.group(1)))
    return nums


def resolved_numbers():
    """Issues a human has manually dispositioned (e.g. dropped as duplicate) and that
    must never be auto-logged again, even though no CSV row references them."""
    out = {}
    if os.path.exists(RESOLVED_PATH):
        for r in csv.DictReader(open(RESOLVED_PATH, encoding="utf-8-sig")):
            try:
                out[int(r["issue"])] = (r.get("disposition", "").strip() or "resolved",
                                        r.get("reason", ""))
            except (ValueError, KeyError):
                continue
    return out


def append_rows(rows):
    exists = os.path.exists(CSV_PATH)
    # BOM only when creating the file; append as plain utf-8 so no BOM lands mid-file.
    enc = "utf-8" if exists else "utf-8-sig"
    with open(CSV_PATH, "a", newline="", encoding=enc) as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow(row)


# ---- main ------------------------------------------------------------------------
def process(issues):
    done = already_logged_numbers()
    resolved = resolved_numbers()
    new_rows, logged, corrections, needs_source, resolved_hits, skipped = [], [], [], [], [], []
    for issue in issues:
        num = issue["number"]
        labels = {l["name"] for l in issue.get("labels", [])}
        # Manual dispositions win first, so a dropped duplicate is always labelled/commented
        # for visibility and never re-logged — even if a stray "#N" mention elsewhere would
        # otherwise mark it "already handled".
        if num in resolved:
            disposition, reason = resolved[num]
            resolved_hits.append(num)
            if disposition not in labels:
                add_label(num, disposition)
                comment(num, f"Recorded as **{disposition}** in `resolved_issues.csv` and not logged"
                             f" to the pending list. {reason}")
            continue
        if num in done or {"logged", "correction", "needs-source", "duplicate"} & labels:
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
                     "A maintainer will source the remaining fields and review it for the main dataset.")
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
    print(f"resolved (ledger, skipped): {resolved_hits}")
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
