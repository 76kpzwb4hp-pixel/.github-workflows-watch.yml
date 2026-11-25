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
LISTING_HREF = re.compile(r"^https://kereby\.dk/bolig/[^/?#]+/?$")
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d{3,6})\s*kr\.?\s*/\s*md", re.I)
ADDRESS_RE = re.compile(r"([^\n]*?\d[^\n]*?,\s*(\d{4})\s+[A-ZÆØÅa-zæøå .]+)")
POSTAL_RE = re.compile(r"\b(\d{4})\s+[A-ZÆØÅ]")


def fetch_html() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def card_for(link):
    """Return the smallest ancestor of the link that contains a price and
    only one distinct listing URL (i.e. the listing 'card')."""
    node = link
    for _ in range(6):
        text = node.get_text(" ", strip=True)
        if PRICE_RE.search(text):
            hrefs = {
                urljoin(URL, a["href"]).rstrip("/")
                for a in node.find_all("a", href=True)
                if LISTING_HREF.match(urljoin(URL, a["href"]))
            }
            if len(hrefs) <= 1:
                return node
            break
        if node.parent is None:
            break
        node = node.parent
    return link


def parse_listings(html: str):
    soup = BeautifulSoup(html, "html.parser")
    listings = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(URL, a["href"])
        if not LISTING_HREF.match(href):
            continue
        href = href.rstrip("/") + "/"
        card = card_for(a)
        text = " ".join(card.get_text(" ", strip=True).split())
        price_m = PRICE_RE.search(text)
        price = int(price_m.group(1).replace(".", "")) if price_m else None
        postal_m = POSTAL_RE.search(text)
        postal = int(postal_m.group(1)) if postal_m else None
        # Keep the richest version if the same URL appears several times
        old = listings.get(href)
        if old and (old["price"] is not None or price is None):
            continue
        listings[href] = {
            "url": href,
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
    body = f"{short_desc(listing)}\n\n{listing['url']}"
    ok1 = send_ntfy(title, short_desc(listing), listing["url"])
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

    listings = parse_listings(fetch_html())
    print(f"Parsed {len(listings)} listings from {URL}")

    if "--list" in args:
        for l in listings:
            print(
                f"- price={l['price']} postal={l['postal']} "
                f"unavailable={l['unavailable']} area={area_of(l)} {l['url']}"
            )
        return

    if not listings:
        # Page layout changed or content is JS-rendered -> fail loudly so you notice
        print("ERROR: found 0 listings - the page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    dry = "--dry-run" in args
    seen = load_seen()
    first_run = seen is None
    seen = seen or set()

    new_matches = []
    for l in listings:
        if l["url"] in seen:
            continue
        area = matches(l)
        if area and not first_run:
            new_matches.append((l, area))
        # Mark everything unavailable-or-not as seen so only truly NEW pages alert
        seen.add(l["url"])

    for l, area in new_matches:
        if dry:
            print(f"[dry-run] would notify: {area} {l['price']} {l['url']}")
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
