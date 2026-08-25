# Paire Meta feed

This repository replaces the paid **Flexify** Shopify app. Every hour it reads
the product catalog from Shopify (read-only — it cannot change anything in the
store) and publishes an XML file that Meta (Facebook/Instagram) downloads to
decide **which image, gallery and video** each catalog item shows in ads.

**The feed URL** (this is what Commerce Manager points at):

```
https://ny1k.github.io/paire-meta-feed/feed.xml
```

Everything else about a catalog item — title, price, availability, links —
comes from the Shopify "Facebook & Instagram" sales channel, exactly as
before. This feed only wins the image/video attributes, matched to catalog
items by the numeric Shopify variant ID.

---

## Forcing a rebuild right now

The feed rebuilds itself every hour. To rebuild immediately (e.g. after the
merch team changes an image):

1. Open the repository on GitHub → **Actions** tab.
2. Click **Build & publish feed** in the left sidebar.
3. Click the **Run workflow** dropdown (right side) → green **Run workflow**
   button.
4. Wait ~3–5 minutes for the green tick. The feed URL now serves the new XML.
   (Meta itself re-fetches the feed on its own hourly schedule.)

## Changing which image or video a variant uses

Three ways, all done in Shopify admin — never in this repository:

- **The `meta` tag (strongest — the go-forward tool):** in the image's alt
  text, add the word `meta` as its own comma-separated token next to the
  colour name, e.g. `Snow,meta` or `Espresso[portrait],meta` — that image
  becomes the main image for that colour's variants. `meta,[global]` makes
  it the main image for every colourway. First tagged image in media order
  wins.
- **Legacy manual override:** the variant metafield `flexify.image_link`
  (~1,300 variants hold curated values from the Flexify era; they keep
  working even after Flexify is uninstalled, because the data lives in the
  store, not in the app). A `meta` tag outranks it. `flexify.video` works
  the same way for videos, comma-separated URLs.
- **Alt-text colour tagging:** otherwise the feed picks by the merch team's
  existing convention — comma-separated colour names, `[portrait]` marking
  a lifestyle/on-model shot, `[global]` meaning "applies to every colour".
  For each variant the feed prefers a portrait image tagged for its colour,
  then any image tagged for its colour, then the variant's own image.

The gallery (`additional_image_link`, the Shops carousel): products **with**
a Color/Colour option get a colour-curated gallery (images tagged for that
colour or `[global]`); products **without** a colour option carry the full
product gallery. Capped at 20, main image never duplicated.

Changes take effect at the next hourly rebuild (or force one, above).

## If you see a red ❌ in the Actions tab

A failed run means **the feed simply was not updated** — Meta keeps using the
last good version, so there is no urgent breakage. Click the failed run to see
the log. The common causes:

- **"SAFETY GUARD: only N items generated"** — Shopify returned incomplete
  data (usually a temporary API problem). The run refused to publish a
  near-empty feed on purpose. Re-run it; if it keeps failing, something
  changed in the store.
- **"Could not get an access token"** — the app's credentials were rotated,
  or the app was uninstalled from the store. In the Shopify **Dev Dashboard**
  open the app "Paire Meta feed generator" → **Settings**, copy the current
  **Client ID** and **Client secret**, and paste them into GitHub: repo
  **Settings → Secrets and variables → Actions** → update
  `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`. (If the app was
  uninstalled, reinstall it from the app's Home page first.)
- Anything else: re-run once before investigating; most one-off failures are
  network blips.

## The Google Merchant Center feed

The same build also publishes a **supplemental feed for Google Merchant
Center** (account 288154111):

```
https://ny1k.github.io/paire-meta-feed/feed_google.xml
```

It applies the same image rules (meta tag → legacy metafield → first
portrait/model shot for the colour) but emits **only** the main image —
no galleries, no custom labels (Google Ads campaigns may use custom labels
for segmentation, so we never touch them), and no link/price/title. Each
variant appears twice, once per Google offer-id scheme
(`shopify_AU_{productId}_{variantId}` for the Shopify app source and
`{productId}_{sku}` for the "Found by Google" crawl source) — whichever
source the feed is attached to ignores the ids it doesn't hold, exactly like
Meta ignores unknown ids.

## Expected warnings in Meta

This feed deliberately covers **more** items than Flexify did (it includes
POS-only products that exist in the Meta catalog, e.g. the three Track Pants
products). A few emitted rows may not match any catalog item — Meta ignores
unknown IDs in supplementary feeds and may log warnings about them. That is
normal and expected.

## Things NOT to touch

- **Do not** commit a `feed.xml` to this repository — it is generated fresh
  each run and published straight to GitHub Pages.
- **Do not** add fields like price, title, availability or `gtin` to the
  feed. Supplementary feeds cannot override price/availability, and the
  store's barcode data is not valid GTINs (Flexify emitting them caused
  ~1,228 warnings). The principle: *emit only the fields we intend to win.*
- **Do not** give the Shopify app any scope beyond `read_products` (scopes
  live on the app version in the Dev Dashboard). The generator refuses to
  run if its access can write to the store.
- The schedule note: GitHub pauses hourly schedules if the repository sees no
  activity for 60 days. Pressing **Run workflow** (or any commit) wakes it up
  again — worth doing after long quiet periods.

## Config knobs (top of `generate_feed.py`)

- `EXCLUDE_TITLE_PATTERN` — products whose title matches are left out of the
  feed entirely (gift cards, outlet, clearance, …).
- `STRICT_VIDEO_COLOUR` — `False` (current): a variant with no colour-matched
  video falls back to the product's videos, like Flexify did. `True`: such
  variants get no video.
- `MIN_ITEMS_GUARD` — refuse to publish a feed smaller than this (1,000).
