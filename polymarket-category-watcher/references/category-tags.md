# Category tag slugs & APIs — polymarket-category-watcher

This skill discovers markets by **category** and streams their live prices. It
touches two public, unauthenticated APIs:

- **Gamma** (`https://gamma-api.polymarket.com/markets`) — market discovery,
  filtered by `tag_slug`, paginated by `offset`.
- **CLOB** (`https://clob.polymarket.com/midpoint`) — live midpoint per token.

> **Verification status:** the skill's sandbox blocks egress to the Polymarket
> APIs, so the tag slugs below were inferred from Polymarket's public tag system
> and the patterns already used by `polymarket-analyzer/scripts/lol_top_holders.py`.
> They are **best-effort**. Confirm a slug with `--debug` (or a browser/curl on a
> networked machine) before relying on it, and pass it explicitly via `--tag`.

## Category → candidate tag slugs

Discovery tries each candidate **in order** and uses the first that returns
markets. If none do, it falls back to a Gamma `q=` text search on the category
name. Defined in `CATEGORY_TAG_CANDIDATES` in `scripts/category_common.py`.

| Category key | Candidate `tag_slug`s (in order) |
|---|---|
| `basketball` | basketball, nba, ncaab, euroleague |
| `tennis` | tennis, atp, wta |
| `soccer` | soccer, football, epl, premier-league, champions-league, la-liga, uefa, mls |
| `baseball` | baseball, mlb |
| `american-football` | nfl, american-football, college-football |
| `hockey` | hockey, nhl |
| `cricket` | cricket, ipl |
| `golf` | golf, pga |
| `combat-sports` | mma, ufc, boxing |
| `league-of-legends` | league-of-legends, lol, esports |
| `counter-strike` | counter-strike, cs2, csgo, esports |
| `dota` | dota, dota-2, esports |
| `valorant` | valorant, esports |
| `esports` | esports |
| `crypto` | crypto, bitcoin, ethereum |
| `politics` | politics, elections, us-election |
| `economy` | economy, economics, fed |
| `sports` | sports |

## Aliases (incl. PT-BR)

`CATEGORY_ALIASES` maps common user input to a canonical key:

| Input | Canonical |
|---|---|
| basquete, nba | basketball |
| tenis, tênis | tennis |
| futebol, football, futbol | soccer |
| futebol-americano, nfl | american-football |
| beisebol, mlb | baseball |
| hoquei, hóquei, nhl | hockey |
| lol, league | league-of-legends |
| cs, cs2, csgo | counter-strike |
| ufc, mma, boxe | combat-sports |
| criptomoeda, bitcoin | crypto |
| politica, política, eleicoes | politics |
| economia | economy |

Any value that is neither a canonical key nor an alias is treated as a **literal
tag slug** (so `--category my-custom-tag` works like `--tag my-custom-tag`).

## How to verify a slug locally

```bash
# Does a tag return active markets?
curl -s "https://gamma-api.polymarket.com/markets?tag_slug=nba&active=true&closed=false&limit=3" \
  | jq '.[].question'

# Live midpoint for a token from that market's clobTokenIds
curl -s "https://clob.polymarket.com/midpoint?token_id=<TOKEN_ID>" | jq
```

If a slug differs, update `CATEGORY_TAG_CANDIDATES` in `category_common.py`
(and this table), or just pass the working slug with `--tag`.

## Sports game slugs (used by `list_games_today.py`)

Polymarket lists a sports game with a date-stamped slug under `/sports/<league>/`:

```
https://polymarket.com/sports/mlb/mlb-hou-kc-2026-06-13
                                   └─┬─┘ └┬┘ └┬┘ └────┬────┘
                                  league away home  game date
```

`extract_slug_date()` reads the trailing `YYYY-MM-DD` from the slug (or
`event_slug`), which is the **authoritative** game date. `game_date()` falls back
to `gameStartTime` then `startDate` (UTC) when a slug carries no date. Each game
is one event; its moneyline, totals, and run-line markets share the same
`event_slug` and are grouped into a single game row.

## Pagination

`discover_markets` requests pages of 100 (`limit=100`) and advances `offset` by
100 until a short/empty page, so it returns **all** markets for the tag, not just
the first page. `--max-markets` caps the total when you only need a sample.

## Rate limits

No documented limit. The client defaults to 100ms between calls (`--rate-limit`)
with exponential backoff on HTTP 429. `watch_category.py` makes one `/midpoint`
call per tracked market per cycle, plus one paginated discovery sweep every
`--rescan-every` cycles.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `Live markets: 0` for a sport | tag slug differs from the candidates | `--debug`, verify slug, pass `--tag` |
| Discovery falls back to `text:<name>` | no tag candidate matched | confirm the real slug and add it to the table |
| `priced: 0` in snapshots | CLOB `/midpoint` shape/host changed | `--debug`, check the `/midpoint` response |
| 403 / blocked | egress blocked or API down | run from a networked machine |
