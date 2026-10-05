"""
ATS Board Finder — checks which Indian companies have a *live, public* job
board on Greenhouse, Lever, or Ashby, and finds the correct board token/slug.

Why this exists: Greenhouse/Lever/Ashby all expose free public JSON APIs,
but there's no way to search or list companies — you need the exact slug in
advance, and slugs don't always match the obvious company name (e.g. Razorpay
is "razorpaysoftwareprivatelimited", not "razorpay"). This script brute-forces
a handful of common slug variations per company against all three ATS platforms,
so you end up with a verified token list instead of guesses.

Usage:
    pip install requests
    python -m scripts.ats_board_finder
    -> writes data/boards/discovered_boards_batch2.csv with every hit

Confirmed already (found manually, included as sanity checks):
    Razorpay  -> greenhouse -> razorpaysoftwareprivatelimited
    PhonePe   -> greenhouse -> phonepe
    Groww     -> greenhouse -> groww
    Meesho    -> lever      -> meesho
"""

import csv
import re
import time
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. CANDIDATE COMPANY LIST — BATCH 2
# A second, non-overlapping set of 85 Indian companies (ecommerce, mobility,
# fintech, SaaS, proptech, healthtech, edtech). Batch 1 (Razorpay, PhonePe,
# Groww, Meesho, CRED, Zepto, Swiggy, etc.) already ran separately — see
# verified_boards_batch1.csv. Edit freely.
# ---------------------------------------------------------------------------
COMPANIES = [
    "Flipkart", "Myntra", "Ajio", "Snapdeal", "Blinkit", "Ather Energy",
    "Ola Electric", "Bounce", "Rivigo", "BlackBuck", "Xpressbees",
    "Ecom Express", "Cars24", "Spinny", "Droom", "CoinDCX", "CoinSwitch",
    "WazirX", "Zerodha", "Upstox", "Angel One", "5paisa", "Smallcase",
    "INDmoney", "ETMoney", "KreditBee", "Kissht", "LazyPay", "CASHe",
    "IndiaLends", "FamPay", "Niyo", "Open Financial Technologies",
    "HighRadius", "Kissflow", "Zluri", "Hevo Data", "MoEngage", "WebEngage",
    "Leena AI", "Haptik", "Gupshup", "Verloop", "Exotel", "Knowlarity",
    "Times Internet", "InMobi", "Glance", "Pratilipi", "Urban Ladder",
    "Pepperfry", "Livspace", "HomeLane", "NoBroker", "Housing",
    "MagicBricks", "99acres", "Shaadi", "Practo", "PharmEasy", "Netmeds",
    "Portea", "Healthkart", "Curefoods", "Fresh To Home", "Country Delight",
    "Milkbasket", "Setu", "Yubi", "Perfios", "SigTuple", "Niramai",
    "Qure.ai", "Simplilearn", "Great Learning", "Physics Wallah", "Toppr",
    "Testbook", "Unstop", "Internshala", "Scaler", "Newton School",
    "Masai School", "GeeksforGeeks", "Coding Ninjas",
]

ATS_CHECKS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def slug_variants(name: str):
    """Generate a handful of plausible board-token variants for a company name."""
    base = name.lower().strip()
    no_punct = re.sub(r"[^a-z0-9 ]", "", base)
    variants = {
        no_punct.replace(" ", ""),          # "tata1mg"
        no_punct.replace(" ", "-"),         # "tata-1mg"
        no_punct.split(" ")[0],             # first word only, e.g. "tata"
    }
    return [v for v in variants if v]


def check(ats: str, slug: str, timeout=8):
    url = ATS_CHECKS[ats].format(slug=slug)
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            data = resp.json()
            # Greenhouse/Ashby return {"jobs": [...]}, Lever returns a bare list
            jobs = data.get("jobs", data) if isinstance(data, dict) else data
            count = len(jobs) if isinstance(jobs, list) else None
            return True, count, url
    except (requests.RequestException, ValueError):
        pass
    return False, None, url


def main():
    results = []
    for company in COMPANIES:
        found_for_company = False
        for slug in slug_variants(company):
            for ats in ATS_CHECKS:
                ok, count, url = check(ats, slug)
                if ok:
                    print(f"[HIT] {company:20s} -> {ats:10s} -> slug={slug!r} ({count} jobs)")
                    results.append({
                        "company": company, "ats": ats, "slug": slug,
                        "job_count": count, "api_url": url,
                    })
                    found_for_company = True
                time.sleep(0.3)  # be polite, avoid rate limits
        if not found_for_company:
            print(f"[miss] {company:20s} -> no board found under tried slugs")

    out_file = Path(__file__).resolve().parents[1] / "data" / "boards" / "discovered_boards_batch2.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["company", "ats", "slug", "job_count", "api_url"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. {len(results)} verified boards written to {out_file}")
    print("Companies with no hit likely: use a different ATS (Workday/SmartRecruiters/"
          "in-house), disabled their public board, or need a non-obvious slug — "
          "check their careers page footer/source for the real one.")
    print("\nTip: if you also have verified_boards_batch1.csv from the first run, "
          "just open both in Excel/Sheets and stack the rows — same columns, "
          "no merge logic needed.")


if __name__ == "__main__":
    main()
