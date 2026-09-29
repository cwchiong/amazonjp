"""
Amazon Japan L4 watcher (SDE, Solutions Architect, and adjacent tech roles).

Pulls every Amazon posting in Japan from amazon.jobs/en/search.json (unofficial,
unauthenticated, the same endpoint the careers site uses), filters locally for
L4 level tech roles that don't gate on Japanese, and pings Discord for
anything not seen before.

Usage:
    python amazon_jp_watcher.py            # normal run, notifies on new matches
    python amazon_jp_watcher.py --dry-run  # print matches, don't notify or save state
    python amazon_jp_watcher.py --all      # print every current match, ignore seen state

Env:
    DISCORD_WEBHOOK_URL  optional; if unset, matches just print to stdout
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---- switches / thresholds (plain module level on purpose) ----
COUNTRY_CODE = "JPN"
PAGE_SIZE = 100
REQUEST_DELAY_S = 1.0          # be polite between pages
MAX_YEARS_EXPERIENCE = 2       # L4 proxy: Amazon L4 basic quals are usually 0 to 2 yrs, L5 is 3+
EXCLUDE_JP_PREFERRED = False   # True = also drop roles that only *prefer* Japanese
EXCLUDE_GRAD_DEGREE = True     # drop roles whose basic quals require a Master's or PhD
MIN_SKILL_SCORE = 0            # raise to only see roles that mention more of your stack
STATE_FILE = Path(__file__).with_name("seen_amazon_jp.json")
SEARCH_URL = "https://www.amazon.jobs/en/search.json"
JOB_BASE_URL = "https://www.amazon.jobs"

# Role families you'd plausibly clear at L4. Comment a line out to stop watching it.
ROLE_FAMILIES = {
    "SDE": r"software (dev(elopment)? )?engineer|\bsde\b|software developer|"
           r"front.?end engineer|back.?end engineer|full.?stack|"
           r"ソフトウェア開発エンジニア|ソフトウェアエンジニア",
    "Solutions Architect": r"solutions? architect|\bsa\b|ソリューションアーキテクト",
    "Systems/DevOps": r"systems? dev(elopment)? engineer|\bsysde\b|devops|"
                      r"site reliability|\bsre\b|infrastructure engineer",
    "Data Engineer": r"data engineer|データエンジニア",
    "BIE": r"business intelligence engineer|\bbie\b",
    "Cloud Support": r"cloud support (associate|engineer)|クラウドサポート",
    "ProServe": r"(associate )?(delivery|cloud|professional services) consultant",
}
ROLE_PATTERNS = {name: re.compile(rx, re.I) for name, rx in ROLE_FAMILIES.items()}

# Titles that are clearly above L4
TITLE_EXCLUDE = re.compile(
    r"\bsr\.?\b|senior|principal|staff|lead\b|manager|\bmgr\b|director|head of|"
    r"specialist solutions architect|\bsde\s*(ii|iii|2|3)\b|\b(ii|iii|iv)\b|"
    r"intern|シニア|マネージャー|プリンシパル",
    re.I,
)

# Titles that explicitly signal early career / L4
EARLY_CAREER_SIGNAL = re.compile(
    r"new grad|university|campus|early career|graduate|associate|\bsde\s*(i|1)\b|"
    r"\bl4\b|新卒|20\d\d\b",
    re.I,
)

# Japanese requirement patterns (checked against basic vs preferred quals)
JP_REQUIRED = re.compile(
    r"(business|native|fluen(t|cy)|professional|advanced|proficien(t|cy))[ -]?(level)?[^.\n]{0,40}japanese|"
    r"japanese[^.\n]{0,20}(business|native|fluen(t|cy)|professional|advanced|proficien(t|cy))|"
    r"japanese\s*:\s*(business|native|fluent)|"
    r"jlpt\s*n[12]|\bn[12]\s*level|日本語",
    re.I,
)
JP_ANY = re.compile(r"japanese|日本語|jlpt", re.I)

GRAD_DEGREE_REQUIRED = re.compile(r"\b(phd|ph\.d|doctoral|master'?s degree|\bms\b|\bm\.s\.)", re.I)

YEARS_PATTERN = re.compile(r"(\d+)\s*\+?\s*(?:years|yrs)", re.I)

# Your stack, used to rank matches (higher = more overlap)
SKILLS = {
    "Python": r"\bpython\b", "JS/TS": r"javascript|typescript|\bnode", "React": r"\breact\b",
    "AWS": r"\baws\b|amazon web services|\bec2\b|\bs3\b|lambda", "SQL": r"\bsql\b",
    "Pandas": r"pandas", "PyTorch": r"pytorch|machine learning|\bml\b",
    "DevOps": r"ci/cd|devops|terraform|cloudformation|docker|kubernetes",
}
SKILL_PATTERNS = {k: re.compile(v, re.I) for k, v in SKILLS.items()}


def fetch_all_jobs():
    """Fetch every Japan posting, paging until empty."""
    jobs, offset = [], 0
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (personal job alert script)"})
    while True:
        params = {
            "normalized_country_code[]": COUNTRY_CODE,
            "result_limit": PAGE_SIZE,
            "offset": offset,
            "sort": "recent",
        }
        resp = session.get(SEARCH_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("jobs", [])
        jobs.extend(page)
        total = data.get("hits", 0)
        offset += len(page)
        if not page or offset >= total:
            break
        time.sleep(REQUEST_DELAY_S)
    return jobs


def max_years_required(text):
    years = [int(y) for y in YEARS_PATTERN.findall(text or "")]
    return max(years) if years else 0


def role_family(title):
    for name, pat in ROLE_PATTERNS.items():
        if pat.search(title):
            return name
    return None


def skill_hits(job):
    text = " ".join(str(job.get(k, "") or "") for k in
                    ("title", "description", "basic_qualifications", "preferred_qualifications"))
    return [k for k, pat in SKILL_PATTERNS.items() if pat.search(text)]


def classify(job):
    """Return (keep, tags, score) for a job dict."""
    title = job.get("title", "")
    basic = job.get("basic_qualifications", "") or ""
    preferred = job.get("preferred_qualifications", "") or ""

    family = role_family(title)
    if not family:
        return False, ["other role"], 0
    if TITLE_EXCLUDE.search(title) and not EARLY_CAREER_SIGNAL.search(title):
        return False, ["above L4 title"], 0
    if JP_REQUIRED.search(basic) or JP_REQUIRED.search(title):
        return False, ["Japanese required"], 0
    if EXCLUDE_GRAD_DEGREE and GRAD_DEGREE_REQUIRED.search(basic) and "bachelor" not in basic.lower():
        return False, ["grad degree required"], 0

    yrs = max_years_required(basic)
    if yrs > MAX_YEARS_EXPERIENCE:
        return False, [f"{yrs}+ yrs"], 0

    tags = [family, f"{yrs}+ yrs" if yrs else "no yrs listed"]
    if JP_ANY.search(preferred):
        if EXCLUDE_JP_PREFERRED:
            return False, ["Japanese preferred"], 0
        tags.append("JP preferred")
    if EARLY_CAREER_SIGNAL.search(title):
        tags.append("early career title")

    skills = skill_hits(job)
    if len(skills) < MIN_SKILL_SCORE:
        return False, ["low skill overlap"], 0
    if skills:
        tags.append("stack: " + "/".join(skills))
    return True, tags, len(skills)


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=0))


def job_url(job):
    path = job.get("job_path") or f"/en/jobs/{job.get('id_icims', '')}"
    return JOB_BASE_URL + path


def format_line(job, tags):
    loc = job.get("normalized_location") or job.get("location") or "Japan"
    posted = job.get("posted_date", "?")
    return f"**{job.get('title')}** ({loc}, posted {posted}) [{', '.join(tags)}]\n{job_url(job)}"


def notify(lines):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("\n\n".join(lines))
        return
    # Discord caps messages at 2000 chars, so chunk
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 1900:
            requests.post(webhook, json={"content": chunk}, timeout=15)
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        requests.post(webhook, json={"content": chunk}, timeout=15)


def main():
    dry_run = "--dry-run" in sys.argv
    show_all = "--all" in sys.argv

    jobs = fetch_all_jobs()
    seen = set() if show_all else load_seen()

    matches, skipped = [], {}
    for job in jobs:
        keep, tags, score = classify(job)
        if keep:
            matches.append((job, tags, score))
        else:
            skipped[tags[0]] = skipped.get(tags[0], 0) + 1

    matches.sort(key=lambda m: m[2], reverse=True)   # best stack overlap first
    new = [(j, t) for j, t, _ in matches if str(j.get("id_icims")) not in seen]

    print(f"fetched {len(jobs)} JP postings | {len(matches)} match | {len(new)} new")
    print("skipped:", skipped)

    if new:
        lines = [format_line(j, t) for j, t in new]
        if dry_run:
            print("\n\n".join(lines))
        else:
            notify(["🇯🇵 New Amazon Japan L4ish postings:"] + lines)

    if not dry_run and not show_all:
        save_seen(seen | {str(j.get("id_icims")) for j, _, _ in matches})


if __name__ == "__main__":
    main()
