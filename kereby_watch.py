#!/usr/bin/env python3
"""
Kereby apartment watcher.

Scans https://kereby.dk/bolig/ and sends a push notification (ntfy) and/or
an email for every NEW listing that:
  - is still available (not "Reserveret" / "Udlejet")
  - costs less than MAX_RENT kr./month
  - is in Nørrebro, Vesterbro or Frederiksberg (matched by postal code / text)

Usage:
  python kereby_watch.py            # normal run
  python kereby_watch.py --dry-run  # print what would be sent, don't notify/save
  python kereby_watch.py --test     # send a test notification and exit
  python kereby_watch.py --list     # print everything parsed from the page
"""
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ----------------------------- CONFIG ---------------------------------------
URL = "https://kereby.dk/bolig/"
MAX_RENT = 14_000  # kr./md. - strictly below this

# Postal-code ranges (inclusive) per area you care about
AREAS = {
    "Nørrebro": [(2200, 2200)],
    "Vesterbro": [(1600, 1799)],       # København V
    "Frederiksberg": [(1800, 1999), (2000, 2000)],
}
# Fallback: also match these words in the listing text (e.g. "... på Nørrebro")
AREA_KEYWORDS = ["nørrebro", "vesterbro", "frederiksberg"]

UNAVAILABLE_WORDS = ("reserveret", "udlejet")
STATE_FILE = Path(__file__).with_name("seen.json")

# Notification settings come from environment variables (GitHub Secrets)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
# -----------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-apartment-watcher/1.0)",
    "Accept-Language": "da,en;q=0.8",
}
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d{3,6})\s*kr\.?\s*/\s*md", re.I)
POSTAL_RE = re.compile(r"\b(\d{4})\s+[A-ZÆØÅ]")
COUNT_RE = re.compile(r"(\d+)\s+boliger?\s+fundet", re.I)
SLUG_RE = re.compile(r"(?:https?://kereby\.dk)?/bolig/([^/\s\"'?#<>]+)/?")
ADDRESS_RE = re.compile(
    r"([A-ZÆØÅ][^,]{2,40}?\d+[A-Za-z]?(?:, [^,]{1,12})*, \d{4} "
    r"(?:København(?: [A-ZØ])?|Frederiksberg(?: C)?|[A-ZÆØÅ][a-zæøå]+))"
)
NOISE_WORDS = ("Reserveret", "Udlejet", "Delevenlig", "Elevator", "Altan",
               "Penthouse", "Tagterrasse")


def fetch_html() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_rendered_html() -> str:
    """Render the page in headless Chromium (for JS-loaded listings)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="da-DK")
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        for _ in range(6):  # scroll to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
        html = page.content()
        browser.close()
    return html


def expected_count(html: str):
    m = COUNT_RE.search(BeautifulSoup(html, "html.parser").get_text(" "))
    return int(m.group(1)) if m else None


def find_listing_url(card):
    """Look for a /bolig/<slug>/ URL in ANY attribute of the card or its
    descendants (href, data-href, data-url, onclick, ...)."""
    for tag in [card] + card.find_all(True):
        for val in tag.attrs.values():
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if isinstance(v, str):
                    m = SLUG_RE.search(v)
                    if m:
                        return urljoin(URL, f"/bolig/{m.group(1)}/")
    # also check if the card sits inside a link
    parent_a = card.find_parent("a", href=True)
    if parent_a:
        m = SLUG_RE.search(parent_a["href"])
        if m:
            return urljoin(URL, f"/bolig/{m.group(1)}/")
    return None


def parse_listings(html: str):
    """Find listing cards by locating prices ('15.696 kr./md.') and climbing
    to the largest ancestor that still contains exactly one price."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    cards, seen_ids = [], set()
    for s in soup.find_all(string=re.compile(r"kr\.?\s*/\s*md", re.I)):
        cur = s.parent
        if cur is None:
            continue
        while cur.parent is not None and cur.parent.name not in ("body", "html", "[document]"):
            if len(PRICE_RE.findall(cur.parent.get_text(" ", strip=True))) == 1:
                cur = cur.parent
            else:
                break
        if id(cur) in seen_ids:
            continue
        seen_ids.add(id(cur))
        cards.append(cur)

    listings = {}
    for card in cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        price_m = PRICE_RE.search(text)
        if not price_m:
            continue
        price = int(price_m.group(1).replace(".", ""))
        addr_m = ADDRESS_RE.search(text)
        address = addr_m.group(1) if addr_m else None
        if address:
            changed = True
            while changed:
                changed = False
                for w in NOISE_WORDS:
                    if address.startswith(w + " "):
                        address = address[len(w) + 1:]
                        changed = True
        postal_m = POSTAL_RE.search(text)
        postal = int(postal_m.group(1)) if postal_m else None
        url = find_listing_url(card)
        key = url or (f"addr:{address}" if address else f"text:{text[:80]}")
        listings[key] = {
            "key": key,
            "url": url or URL,          # fall back to the overview page
            "address": address,
            "text": text,
            "price": price,
            "postal": postal,
            "unavailable": any(w in text.lower() for w in UNAVAILABLE_WORDS),
        }
    return list(listings.values())


def area_of(listing):
    p = listing["postal"]
    if p is not None:
        for name, ranges in AREAS.items():
            if any(lo <= p <= hi for lo, hi in ranges):
                return name
    low = listing["text"].lower()
    for kw in AREA_KEYWORDS:
        if kw in low:
            return kw.capitalize()
    return None


def matches(listing):
    if listing["unavailable"]:
        return None
    if listing["price"] is None or listing["price"] >= MAX_RENT:
        return None
    return area_of(listing)


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return None  # None = first run


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def short_desc(listing):
    # Strip the "Reserveret"/"Udlejet" noise and trim for the push body
    return listing["text"][:220]


def send_ntfy(title, message, click_url=None, priority=4):
    if not NTFY_TOPIC:
        return False
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": ["house"],
    }
    if click_url:
        payload["click"] = click_url
        payload["actions"] = [
            {"action": "view", "label": "Open listing", "url": click_url}
        ]
    r = requests.post(NTFY_SERVER, json=payload, timeout=20)
    r.raise_for_status()
    return True


def send_email(subject, body):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        return False
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


def notify(listing, area):
    title = f"Ny Kereby-bolig i {area}: {listing['price']:,} kr./md".replace(",", ".")
    addr = listing.get("address") or short_desc(listing)
    body = f"{addr}\n\n{listing['url']}"
    ok1 = send_ntfy(title, addr, listing["url"])
    ok2 = send_email(title, body)
    if not (ok1 or ok2):
        print("WARNING: no notification channel configured!", file=sys.stderr)
    print(f"NOTIFIED: {title} -> {listing['url']}")


def main():
    args = set(sys.argv[1:])

    if "--test" in args:
        ok1 = send_ntfy("Kereby watcher test", "Notifications work!", URL)
        ok2 = send_email("Kereby watcher test", "Notifications work!")
        print(f"ntfy: {ok1}, email: {ok2}")
        return

    html = fetch_html()
    listings = parse_listings(html)
    expected = expected_count(html)
    print(f"Static HTML: parsed {len(listings)} listings (page says {expected})")

    if "--debug" in args:
        print(f"HTML length: {len(html)}, 'kr./md' occurrences: "
              f"{len(re.findall(r'kr\\.?/md', html))}, '/bolig/' occurrences: "
              f"{html.count('/bolig/')}")

    need_browser = ("--browser" in args or not listings
                    or (expected is not None and len(listings) < expected))
    if need_browser:
        print("Falling back to headless browser...")
        try:
            html2 = fetch_rendered_html()
            listings2 = parse_listings(html2)
            expected = expected_count(html2) or expected
            print(f"Browser: parsed {len(listings2)} listings (page says {expected})")
            if len(listings2) >= len(listings):
                listings = listings2
        except Exception as e:
            print(f"Browser fallback failed: {e}", file=sys.stderr)

    if "--list" in args:
        for l in listings:
            print(
                f"- {l['price']} kr | postal={l['postal']} | "
                f"unavailable={l['unavailable']} | area={area_of(l)} | "
                f"{l['address']} | {l['url']}"
            )
        return

    if not listings:
        print("ERROR: found 0 listings - the page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)
    if expected is not None and len(listings) < expected:
        print(f"WARNING: parsed {len(listings)} of {expected} listings.",
              file=sys.stderr)

    dry = "--dry-run" in args
    seen = load_seen()
    first_run = seen is None
    seen = seen or set()

    new_matches = []
    for l in listings:
        if l["key"] in seen:
            continue
        area = matches(l)
        if area and not first_run:
            new_matches.append((l, area))
        # Mark everything unavailable-or-not as seen so only truly NEW pages alert
        seen.add(l["key"])

    for l, area in new_matches:
        if dry:
            print(f"[dry-run] would notify: {area} {l['price']} {l['address']} {l['url']}")
        else:
            notify(l, area)

    if first_run and not dry:
        try:
            send_ntfy("Kereby watcher started",
                      f"Tracking {len(listings)} existing listings. "
                      f"You'll be alerted about new ones under {MAX_RENT:,} kr."
                      .replace(",", "."), URL, priority=3)
        except Exception as e:  # don't fail the seeding run on this
            print(f"start notification failed: {e}", file=sys.stderr)

    if not dry:
        save_seen(seen)
    print(f"Done. {len(new_matches)} new matching listing(s).")


if __name__ == "__main__":
    main()
