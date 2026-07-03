# Phoenix Gateway price/stock scraper (Scrapy, local runner)

A standalone Scrapy port of the `scrape-phoenix-gateway` edge function's catalogue
sweep — run it from **your own connection**, because Phoenix's gateway denies
cloud/datacenter-origin requests (the block that stops the edge function and
Zyte alike, unless you pay for Zyte's residential proxy).

Phoenix is a **GraphQL API** (`gateway.phoenixgateway.co.uk/graphql`):
`login` → `getAllProductsBySearchTerm` (names + PIPs, no prices) → per PIP
`getProductByPipId` (real price + stock). It writes priced rows into
`public.live_prices_cache` (supplier `phoenix`) — the same table the live-price
path reads.

## Setup

```bash
cd tools/phoenix-scrapy
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env      # then edit
```

Fill `.env`:
- `PHOENIX_USER` / `PHOENIX_PASS` — the branch's Phoenix Gateway login.
- `PHOENIX_DELIVERY_POINT_ID` — the branch's Phoenix **account number**
  (deliveryPointId, e.g. `21916`). **Required** — search + pricing key off it.
- `PHOENIX_BRANCH_ID` — the app branch UUID (recommended; `live_prices_cache` is
  keyed on `pip_code, supplier_name, branch_id`).
- `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` — optional; set both to upsert into
  `live_prices_cache`. Use the **service-role** key. Leave blank for JSONL only.

## Run

```bash
scrapy crawl phoenix -a max_terms=10     # quick test (10 prefix terms)
scrapy crawl phoenix                     # full catalogue sweep (~350 prefixes)
```

- Terms default to a 3-letter prefix sweep of the whole catalogue. Override with
  `-a terms_file=terms.txt` (one term/PIP per line) to target specific lines.
- `PHOENIX_CONCURRENCY` (default 8) — hydration is one request per PIP, so this is
  the throughput lever; lower it (and raise `PHOENIX_DELAY`) if Phoenix 429s.

Output: `output/phoenix_prices_*.jsonl`, and `live_prices_cache` upserts when the
Supabase settings are set.

## Zyte (optional)

Same as the eCass scraper: `shub deploy <project>` (or GitHub deploy from a repo
with this project at the root), set the same keys in Zyte **Raw settings**, and —
because Zyte Cloud is a datacenter IP Phoenix blocks — enable
`ZYTE_SMARTPROXY_ENABLED=True` + `ZYTE_SMARTPROXY_APIKEY` (paid). A local run on
your own line needs none of that.

## Notes / safety

- `.env` is gitignored — never commit credentials or the service key.
- One login session; don't run two crawls on the same Phoenix account at once.
- Real authenticated harvest under a named pharmacy account — keep concurrency
  sane and stop on sustained 429s.
- Mirrors the flow in `supabase/functions/scrape-phoenix-gateway/index.ts`.
