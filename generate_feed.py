#!/usr/bin/env python3
"""Meta (Facebook/Instagram) supplementary product feed generator for Paire.

Replaces the Flexify Shopify app. Reads products from the Shopify Admin
GraphQL API (read-only) and writes feed.xml — an RSS 2.0 file with the
Google `g:` namespace that Meta fetches hourly.

This feed is SUPPLEMENTARY: it only overrides the image, gallery, video and
custom_label_0 attributes of catalog items created by the Shopify sales
channel. Items are matched on g:id = the numeric Shopify variant ID.
Everything else (title, price, availability, ...) is deliberately NOT
emitted — the Shopify channel owns those fields.

Auth: a Dev Dashboard app (client credentials grant). Each run exchanges
SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET for a fresh 24-hour Admin API
access token — nothing long-lived is stored.

Usage:
    SHOPIFY_CLIENT_ID=... SHOPIFY_CLIENT_SECRET=... python generate_feed.py
    ... --check    # additionally run the acceptance tests
"""

import argparse
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

# ============================================================================
# CONFIG — the knobs a future session may need to edit
# ============================================================================

SHOP = "getpaire.myshopify.com"
API_VERSION = "2026-07"

# Products whose title matches this pattern (case-insensitive) are excluded
# from the feed entirely (no rows emitted; the Shopify channel's data stands).
EXCLUDE_TITLE_PATTERN = (
    r"gift ?card|mystery|gift wrap|warehouse sale|outlet|\(special\)"
    r"|grab box|clearance|packing material|blind box|\(offline\)"
)

# When a variant has no colour-matched video: False = fall back to all of the
# product's videos (matches Flexify's behaviour); True = emit no video.
STRICT_VIDEO_COLOUR = False

# Safety guard: refuse to write a feed with fewer items than this
# (protects against a Shopify API hiccup publishing a near-empty feed).
MIN_ITEMS_GUARD = 1000

MAX_ADDITIONAL_IMAGES = 20   # Meta's cap for additional_image_link
MAX_VIDEOS = 20              # sanity cap for videos per item
MAX_VIDEO_HEIGHT = 1080      # emit the largest mp4 derivative <= this height

CHANNEL_TITLE = "Paire"
CHANNEL_LINK = "https://www.paire.com"

OUTPUT_FILE = "feed.xml"

# --- Google Merchant Center supplemental feed --------------------------------
# Same image rules as the Meta feed, but emits ONLY g:image_link ("emit only
# the fields we intend to win" — and never custom_label_*, which Google Ads
# campaigns may use for segmentation). Each variant appears under BOTH offer-id
# schemes found in GMC account 288154111 (feed label AU); whichever source the
# feed is attached to simply ignores ids it doesn't hold:
#   shopify_AU_{productId}_{variantId}   -> "Shopify App API" source
#   {productId}_{lowercased sku}         -> "Found by Google" crawl source
GOOGLE_OUTPUT_FILE = "feed_google.xml"
GOOGLE_ID_PREFIX = "shopify_AU"

# Variant metafields that act as manual overrides (namespace.key):
META_IMAGE = ("flexify", "image_link")   # single URL, used verbatim
META_VIDEO = ("flexify", "video")        # comma-separated URLs, used verbatim

# ============================================================================
# Shopify Admin GraphQL client (read-only) with cost-throttle backoff
# ============================================================================

GRAPHQL_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
OAUTH_TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"


def get_access_token(client_id, client_secret):
    """Client credentials grant: exchange the app's ID+secret for a fresh
    Admin API access token (valid 24h; we fetch a new one every run)."""
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"Could not get an access token (HTTP {resp.status_code}). "
                 "Usual causes: the app is not installed on the store, or the "
                 "client ID/secret is wrong or was rotated. Response: "
                 f"{resp.text[:300]}")
    payload = resp.json()
    granted = payload.get("scope", "")
    writes = [s for s in granted.split(",") if s.strip().startswith("write_")]
    if writes:
        sys.exit(f"REFUSING TO RUN: token grants write scopes {writes}. "
                 "Remove them from the app version in the Dev Dashboard.")
    return payload["access_token"]


class ShopifyClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        })

    def query(self, query, variables=None):
        """Run one GraphQL query with retry + cost-throttle backoff."""
        for attempt in range(8):
            try:
                resp = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables or {}},
                    timeout=90,
                )
            except requests.RequestException as exc:
                print(f"  network error ({exc.__class__.__name__}), retrying...",
                      file=sys.stderr)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                print(f"  HTTP {resp.status_code}, retrying...", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            payload = resp.json()

            errors = payload.get("errors")
            if errors:
                if any(e.get("extensions", {}).get("code") == "THROTTLED"
                       for e in errors):
                    print("  throttled by Shopify, backing off...", file=sys.stderr)
                    time.sleep(2 + 2 ** attempt)
                    continue
                raise RuntimeError(f"GraphQL errors: {errors}")

            # Proactive backoff: if the cost bucket is running low, pause
            # long enough for it to refill (docs: extensions.cost.throttleStatus).
            cost = payload.get("extensions", {}).get("cost", {})
            status = cost.get("throttleStatus", {})
            available = status.get("currentlyAvailable")
            restore = status.get("restoreRate") or 50
            if available is not None and available < 300:
                time.sleep(min(10.0, (300 - available) / restore))

            return payload["data"]
        raise RuntimeError("Shopify API kept failing after 8 attempts")


def verify_read_only(client):
    """Abort if the token holds any write scope. This system must never be
    able to modify the store."""
    data = client.query(
        "{ currentAppInstallation { accessScopes { handle } } }")
    scopes = [s["handle"]
              for s in data["currentAppInstallation"]["accessScopes"]]
    writes = [s for s in scopes if s.startswith("write_")]
    if writes:
        sys.exit(f"REFUSING TO RUN: token has write scopes {writes}. "
                 "Remove them from the app version in the Dev Dashboard.")
    print(f"Token scopes verified read-only: {scopes}")


# ============================================================================
# Fetching
# ============================================================================

PRODUCT_LIST_QUERY = """
query ProductList($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes { id title }
  }
}
"""

# One product per request keeps the requested query cost safely under
# Shopify's single-query maximum even with 100 media + 100 variants.
PRODUCT_DETAIL_QUERY = """
query ProductDetail($id: ID!) {
  product(id: $id) {
    id
    title
    options { name }
    media(first: 100) {
      nodes {
        __typename
        ... on MediaImage {
          alt
          image { url }
        }
        ... on Video {
          alt
          sources { format height url }
          originalSource { url }
        }
      }
    }
    variants(first: 100) {
      pageInfo { hasNextPage }
      nodes {
        id
        title
        sku
        availableForSale
        selectedOptions { name value }
        image { url }
        imageOverride: metafield(namespace: "%s", key: "%s") { value }
        videoOverride: metafield(namespace: "%s", key: "%s") { value }
      }
    }
  }
}
""" % (META_IMAGE[0], META_IMAGE[1], META_VIDEO[0], META_VIDEO[1])


def numeric_gid(gid):
    """gid://shopify/ProductVariant/47050634002655 -> '47050634002655'"""
    return gid.rsplit("/", 1)[-1]


def fetch_products(client):
    """Return detailed product dicts for all active, non-excluded products."""
    exclude_re = re.compile(EXCLUDE_TITLE_PATTERN, re.IGNORECASE)

    listing, cursor = [], None
    while True:
        data = client.query(PRODUCT_LIST_QUERY, {"cursor": cursor})
        page = data["products"]
        listing.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    included = [p for p in listing if not exclude_re.search(p["title"])]
    excluded = [p for p in listing if exclude_re.search(p["title"])]
    print(f"{len(listing)} active products; {len(included)} included, "
          f"{len(excluded)} excluded by title pattern")

    products = []
    for i, stub in enumerate(included, 1):
        data = client.query(PRODUCT_DETAIL_QUERY, {"id": stub["id"]})
        product = data["product"]
        if product is None:
            print(f"  warning: product {stub['id']} vanished mid-run, skipping",
                  file=sys.stderr)
            continue
        if product["variants"]["pageInfo"]["hasNextPage"]:
            print(f"  warning: {product['title']} has >100 variants; "
                  "only the first 100 are included", file=sys.stderr)
        products.append(product)
        if i % 25 == 0 or i == len(included):
            print(f"  fetched {i}/{len(included)} products")
    return products


# ============================================================================
# Alt-text grammar
#
# Alt text is comma-separated tokens. [portrait] marks a lifestyle/on-model
# shot, [global] applies to every colourway; stripping all [bracketed]
# segments from a token leaves a colour name. A token that is just the word
# "meta" designates this image as the MAIN image for the colours named in
# the same alt (or all colours with [global]) — the go-forward manual pick,
# outranking even the legacy flexify.image_link metafield. Examples:
#   "[portrait]Snow"          lifestyle shot of Snow
#   "Espresso[portrait],Blush" lifestyle for Espresso; plain tag for Blush
#   "[portrait][global]"      lifestyle, applies to all colours
#   "Navy"                    plain e-comm shot of Navy
#   "Snow,meta"               main image for Snow variants
#   "meta,[global]"           main image for every colourway
# ============================================================================

BRACKET_RE = re.compile(r"\[([^\]]*)\]")


class Token:
    __slots__ = ("colour", "portrait", "is_global", "is_meta")

    def __init__(self, raw):
        markers = [m.lower() for m in BRACKET_RE.findall(raw)]
        self.portrait = "portrait" in markers
        self.is_global = "global" in markers
        colour = BRACKET_RE.sub("", raw).strip().lower()
        # "meta" (or [meta]) is a marker, never a colour name.
        self.is_meta = colour == "meta" or "meta" in markers
        self.colour = "" if colour == "meta" else colour


def parse_alt(alt):
    if not alt:
        return []
    return [Token(part) for part in alt.split(",") if part.strip()]


def variant_colour(variant, product):
    """The value of the Color/Colour option; else text before the first '/'
    in the variant title."""
    for opt in variant["selectedOptions"]:
        if opt["name"].strip().lower() in ("color", "colour"):
            return opt["value"].strip()
    return variant["title"].split("/")[0].strip()


def has_colour_option(product):
    return any(o["name"].strip().lower() in ("color", "colour")
               for o in product["options"])


class TaggedMedia:
    """One media entry (image or video) with its parsed alt tokens."""

    def __init__(self, node):
        self.type = node["__typename"]
        self.alt = node.get("alt") or ""
        self.tokens = parse_alt(self.alt)
        if self.type == "MediaImage":
            self.url = (node.get("image") or {}).get("url")
        else:
            self.url = None
        self.node = node

    def matches_colour(self, colour):
        """Tagged for this colour: a token names it, or carries [global]."""
        return any(t.is_global or (t.colour and t.colour == colour)
                   for t in self.tokens)

    def has_portrait_for(self, colour):
        return any(t.portrait and (t.is_global or t.colour == colour)
                   for t in self.tokens)

    def has_meta(self):
        return any(t.is_meta for t in self.tokens)

    def named_colours(self):
        return {t.colour for t in self.tokens if t.colour}

    def is_untagged(self, known_colours):
        """No known colour name and no [global] marker — the alt grammar
        doesn't apply to this media (empty alt, filenames, etc.)."""
        if any(t.is_global for t in self.tokens):
            return False
        return not (self.named_colours() & known_colours)


def pick_video_url(node):
    """Largest mp4 derivative <= MAX_VIDEO_HEIGHT from sources — never the
    original upload and never the .m3u8 stream."""
    mp4s = [s for s in node.get("sources") or []
            if (s.get("format") or "").lower() == "mp4"
            and s.get("url") and s.get("height")]
    if not mp4s:
        return None
    capped = [s for s in mp4s if s["height"] <= MAX_VIDEO_HEIGHT]
    best = max(capped, key=lambda s: s["height"]) if capped \
        else min(mp4s, key=lambda s: s["height"])
    return best["url"]


# ============================================================================
# Per-variant field logic
# ============================================================================

def metafield_value(variant, alias):
    mf = variant.get(alias)
    return (mf or {}).get("value") or None


def build_item(variant, product, media, known_colours):
    colour = variant_colour(variant, product).lower()
    images = [m for m in media if m.type == "MediaImage" and m.url]
    videos = [m for m in media if m.type == "Video"]

    # ---- g:image_link ------------------------------------------------------
    image_link, label = None, None

    # "meta"-tagged image for this colour: the go-forward manual pick,
    # outranking the legacy metafield (user decision 2026-08-18).
    meta_tagged = [m for m in images
                   if m.has_meta() and m.matches_colour(colour)]
    if meta_tagged:
        image_link, label = meta_tagged[0].url, "override"

    if not image_link:
        override = metafield_value(variant, "imageOverride")
        if override:
            image_link, label = override.strip(), "override"

    if not image_link:
        solo = [m for m in images
                if m.has_portrait_for(colour) and m.named_colours() <= {colour}]
        multi = [m for m in images if m.has_portrait_for(colour)]
        tagged = [m for m in images if m.matches_colour(colour)]
        if solo:
            image_link, label = solo[0].url, "lifestyle-rule"
        elif multi:
            image_link, label = multi[0].url, "lifestyle-rule"
        elif tagged:
            image_link, label = tagged[0].url, "ecomm-fallback"

    if not image_link:
        own = (variant.get("image") or {}).get("url")
        if own:
            image_link, label = own, "ecomm-fallback"
        elif images:
            image_link, label = images[0].url, "ecomm-fallback"

    if not image_link:
        return None  # nothing usable at all (product with zero media)

    # ---- g:additional_image_link ------------------------------------------
    # Colour-option products get a colour-curated gallery; products with no
    # colour concept carry the full gallery (user decision 2026-08-18).
    if has_colour_option(product):
        gallery = [m.url for m in images if m.matches_colour(colour)]
        if len(gallery) < 2:
            gallery += [m.url for m in images if m.is_untagged(known_colours)]
    else:
        gallery = [m.url for m in images]
    seen = {image_link}
    additional = []
    for url in gallery:
        if url not in seen:
            additional.append(url)
            seen.add(url)
    additional = additional[:MAX_ADDITIONAL_IMAGES]

    # ---- video -------------------------------------------------------------
    video_urls = []
    video_override = metafield_value(variant, "videoOverride")
    if video_override:
        video_urls = [u.strip() for u in video_override.split(",") if u.strip()]
    else:
        matched = [m for m in videos if m.matches_colour(colour)]
        chosen = matched if (matched or STRICT_VIDEO_COLOUR) else videos
        for m in chosen:
            url = pick_video_url(m.node)
            if url:
                video_urls.append(url)
    video_urls = list(dict.fromkeys(video_urls))[:MAX_VIDEOS]

    return {
        "id": numeric_gid(variant["id"]),
        "product_id": numeric_gid(product["id"]),
        "availability": ("in stock" if variant.get("availableForSale")
                         else "out of stock"),
        "sku": (variant.get("sku") or "").strip(),
        "product_title": product["title"],
        "variant_title": variant["title"],
        "colour": colour,
        "image_link": image_link,
        "additional_image_link": additional,
        "videos": video_urls,
        "custom_label_0": label,
    }


def build_items(products):
    items = []
    for product in products:
        media = [TaggedMedia(n) for n in product["media"]["nodes"]]
        known_colours = set()
        for v in product["variants"]["nodes"]:
            known_colours.add(variant_colour(v, product).lower())
        for variant in product["variants"]["nodes"]:
            item = build_item(variant, product, media, known_colours)
            if item:
                items.append(item)
            else:
                print(f"  skipped (no image anywhere): {product['title']!r} / "
                      f"{variant['title']!r}", file=sys.stderr)
    return items


# ============================================================================
# XML output — mirrors the structure of the proven-working Flexify feed:
# <item> per variant, plain <g:id>, CDATA values, repeated
# <g:additional_image_link>, nested <video><url>.
# ============================================================================

def cdata(value):
    # "]]>" cannot appear inside a CDATA section; split it if it ever occurs.
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def write_feed(items, path):
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">',
        "  <channel>",
        f"    <title>{CHANNEL_TITLE}</title>",
        f"    <link>{CHANNEL_LINK}</link>",
    ]
    for item in items:
        lines.append("<item>")
        lines.append(f" <g:id>{item['id']}</g:id>")
        lines.append(f" <g:image_link>{cdata(item['image_link'])}</g:image_link>")
        for url in item["additional_image_link"]:
            lines.append(
                f" <g:additional_image_link>{cdata(url)}"
                "</g:additional_image_link>")
        for url in item["videos"]:
            lines.append(" <video>")
            lines.append(f"  <url>{cdata(url)}</url>")
            lines.append(" </video>")
        lines.append(
            f" <g:custom_label_0>{cdata(item['custom_label_0'])}"
            "</g:custom_label_0>")
        lines.append(
            f" <g:availability>{cdata(item['availability'])}</g:availability>")
        lines.append("</item>")
    lines += ["  </channel>", "</rss>", ""]
    content = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {path}: {len(items)} items, {len(content) / 1e6:.1f} MB")


def google_ids(item):
    """The offer ids this variant may hold in Google Merchant Center."""
    ids = [f"{GOOGLE_ID_PREFIX}_{item['product_id']}_{item['id']}"]
    if item["sku"]:
        ids.append(f"{item['product_id']}_{item['sku'].lower()}")
    return ids


def write_google_feed(items, path):
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">',
        "  <channel>",
        f"    <title>{CHANNEL_TITLE}</title>",
        f"    <link>{CHANNEL_LINK}</link>",
    ]
    rows = 0
    for item in items:
        for gid in google_ids(item):
            lines.append("<item>")
            lines.append(f" <g:id>{gid}</g:id>")
            lines.append(
                f" <g:image_link>{cdata(item['image_link'])}</g:image_link>")
            lines.append("</item>")
            rows += 1
    lines += ["  </channel>", "</rss>", ""]
    content = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {path}: {rows} rows ({len(items)} variants x id schemes), "
          f"{len(content) / 1e6:.1f} MB")


# ============================================================================
# --check mode: acceptance tests against known-good values verified in Meta
# ============================================================================

EXPECTED_IMAGES = {
    "47050634002655": "Socks25475.jpg?v=1749636335",
    "48563298009311": "CoolBlend55.jpg?v=1773303194",
    "50505110618335": "Cabin_Stay3.jpg?v=1783619564",
}
EXPECTED_VIDEO_VARIANT = "47284620296415"
EXPECTED_VIDEO_SUBSTRINGS = ("1fc30f58a64e4b52ad4843126334b4fd", "HD-1080p")
# Verified 2026-08-18: 1,806 total variants = 235 on title-excluded products
# + 4 on products with no images at all + 1,567 emitted. Threshold sits well
# above Flexify's 1,258 to catch any real coverage regression.
EXPECTED_MIN_ITEMS = 1500
EXPECTED_PRODUCTS = {"9290199761119", "9005881229535", "9290199498975"}

FLEXIFY_URL = ("https://getpaire.myshopify.com/a/feed/"
               "facebook_supplementary_feed-superfeed.xml")


def url_tail(url):
    """Filename + query string: .../files/Socks25475.jpg?v=123 ->
    'Socks25475.jpg?v=123'."""
    return url.rsplit("/", 1)[-1]


def run_checks(items, flexify_path=None):
    by_id = {item["id"]: item for item in items}
    failures = []

    def check(ok, message):
        print(("  PASS  " if ok else "  FAIL  ") + message)
        if not ok:
            failures.append(message)

    print("\n=== Acceptance checks ===")
    for vid, expected in EXPECTED_IMAGES.items():
        item = by_id.get(vid)
        actual = url_tail(item["image_link"]) if item else "(variant missing)"
        check(item is not None and actual == expected,
              f"variant {vid}: image_link {actual!r} (expect {expected!r})")

    item = by_id.get(EXPECTED_VIDEO_VARIANT)
    ok = item is not None and any(
        all(s in url for s in EXPECTED_VIDEO_SUBSTRINGS)
        for url in item["videos"])
    check(ok, f"variant {EXPECTED_VIDEO_VARIANT} carries the expected "
              f"HD-1080p video ({len(item['videos']) if item else 0} videos)")

    check(len(items) > EXPECTED_MIN_ITEMS,
          f"total items {len(items)} > {EXPECTED_MIN_ITEMS}")

    present_products = {item["product_id"] for item in items}
    for pid in sorted(EXPECTED_PRODUCTS):
        check(pid in present_products, f"product {pid} (POS-only) present")

    dupes = [item["id"] for item in items
             if item["image_link"] in item["additional_image_link"]]
    check(not dupes,
          f"no image_link duplicated in additional_image_link "
          f"({len(dupes)} offenders{': ' + ', '.join(dupes[:5]) if dupes else ''})")

    try:
        ET.parse(OUTPUT_FILE)
        check(True, "feed.xml parses cleanly")
    except ET.ParseError as exc:
        check(False, f"feed.xml parse error: {exc}")

    try:
        tree = ET.parse(GOOGLE_OUTPUT_FILE)
        g_ns = "{http://base.google.com/ns/1.0}"
        rows = [e.text for e in tree.iter(g_ns + "id")]
        expected = sum(len(google_ids(i)) for i in items)
        check(len(rows) == expected,
              f"feed_google.xml rows {len(rows)} == expected {expected}")
        sample = set(google_ids(items[0]))
        check(sample <= set(rows),
              f"feed_google.xml carries both id schemes for first item")
    except ET.ParseError as exc:
        check(False, f"feed_google.xml parse error: {exc}")

    diff_against_flexify(by_id, flexify_path)

    print(f"\n{len(failures)} check(s) failed" if failures
          else "\nAll checks passed.")
    return not failures


def diff_against_flexify(by_id, flexify_path=None):
    """Compare image_link for 20 random variants present in both feeds."""
    print("\n=== Random-20 diff vs live Flexify feed ===")
    try:
        if flexify_path:
            xml_text = open(flexify_path, encoding="utf-8").read()
        else:
            xml_text = requests.get(FLEXIFY_URL, timeout=120).text
    except Exception as exc:
        print(f"  could not load Flexify feed ({exc}); skipping diff")
        return

    flexify = {}
    for m in re.finditer(
            r"<g:id>(\d+)</g:id>.*?<g:image_link><!\[CDATA\[(.*?)\]\]>",
            xml_text, re.S):
        flexify[m.group(1)] = m.group(2)

    common = sorted(set(flexify) & set(by_id))
    sample = random.sample(common, min(20, len(common)))
    same = 0
    for vid in sample:
        ours, theirs = by_id[vid], flexify[vid]
        if ours["image_link"] == theirs:
            same += 1
            continue
        print(f"  DIFF {vid} ({ours['product_title']} / "
              f"{ours['variant_title']}) [{ours['custom_label_0']}]")
        print(f"       ours:    {url_tail(ours['image_link'])}")
        print(f"       flexify: {url_tail(theirs)}")
    print(f"  {same}/{len(sample)} sampled items have identical image_link")


# ============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="run acceptance tests after generating")
    parser.add_argument("--flexify-file", default=None,
                        help="local copy of the Flexify feed for the diff "
                             "(default: fetch the live URL)")
    args = parser.parse_args()

    client_id = os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET environment "
                 "variables must both be set")

    token = get_access_token(client_id, client_secret)
    client = ShopifyClient(token)
    verify_read_only(client)

    products = fetch_products(client)
    items = build_items(products)

    if len(items) < MIN_ITEMS_GUARD:
        sys.exit(f"SAFETY GUARD: only {len(items)} items generated "
                 f"(minimum {MIN_ITEMS_GUARD}). Feed NOT written — this "
                 "protects against publishing a near-empty feed after an "
                 "API hiccup.")

    write_feed(items, OUTPUT_FILE)
    write_google_feed(items, GOOGLE_OUTPUT_FILE)

    labels = {}
    for item in items:
        labels[item["custom_label_0"]] = labels.get(item["custom_label_0"], 0) + 1
    print("provenance:", ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))

    if args.check:
        ok = run_checks(items, args.flexify_file)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
