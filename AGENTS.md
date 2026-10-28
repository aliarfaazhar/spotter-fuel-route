# AGENTS.md

Guidance for coding agents (and people) working in this repo. `README.md` covers what the API does and how to use it;
this file covers how the code is put together and the rules that keep it correct.

## Commands

```bash
make setup   # venv (Python 3.12+), dependencies, .env, SQLite with all stations and places
make run     # http://localhost:8000 (PORT=8001 for another port); /map is the UI
make test    # pytest, no network: every HTTP call must be mocked
make coverage  # tests + coverage report; fails under 85% (pyproject.toml)
make lint    # ruff check + ruff format --check (line length 120)
make live    # the one test that calls the real routing API (RUN_LIVE=1)
```

CI (`.github/workflows/ci.yml`) runs on every push: lint (ruff, including Bandit's `S` security rules),
`makemigrations --check`, tests with a coverage minimum, `pip-audit`, and a Docker build + smoke test. Run
`make lint coverage` before committing. `.flake8` mirrors the ruff settings for editors that use flake8.

## Request flow

```
views.RoutePlanView ─► serializers (validate) ─► planner.plan_trip
   resolver.resolve        text / ZIP / lat,lon → Point, offline first (Nominatim only as a last resort)
   planner.get_route       route cache → routing.fetch_route (the only routing call)
   stations.match_route    KD-tree: stations within 10/25/50 mi of the route, with mile markers
   candidates.build        where fuel may be bought: stations, the fill-up before departure, unlisted fuel
   optimizer               exact DP (min fuel cost + stop_cost per stop), naive baseline for savings
   detour.find_waypoints   only when no tank covers a gap on listed stations (+1 routing call)
   report.*                billed stops, trip cost, route options, warnings → response dict
errors.exception_handler   every failure → {"error": {"code", "message", "details"}}
```

## Module map (`fuel/services/`)

| Module | Owns | Must not |
|---|---|---|
| `routing.py` | ORS / OSRM HTTP calls, polyline decoding, fallback | be bypassed: no other module calls a routing API |
| `resolver.py` | turning user text into a US `Point`, rejecting non-US / island input | call the routing API |
| `stations.py` | in-memory station index (one query), route matching | touch the DB per request |
| `optimizer.py` | pure fuel-stop optimisation on floats (miles) | import Django or know about stations |
| `candidates.py` | building optimiser candidates and telling their kinds apart | format output |
| `detour.py` | waypoints through listed stations when the direct route can't be fuelled | |
| `planner.py` | one request end to end: caches, attempts, assembling the response | hold formatting logic (that's `report.py`) |
| `report.py` | Decimal money, per-stop details, route options, warnings | change plan decisions |
| `caching.py` | cache reads/writes that survive a cache outage | |
| `geometry.py`, `names.py`, `datafiles.py`, `suggest.py` | maths, name normalising, data files, autocomplete | |

## Rules that keep it correct

- **Routing-call budget.** One call per new route, zero for a repeat. Only a fallback (ORS → OSRM) or a detour adds
  one. `meta.external_api_calls` must report every call; tests assert the counts.
- **Coordinate order.** Providers speak `[lon, lat]`; everything inside the app is `(lat, lon)`; the GeoJSON in
  responses is `[lon, lat]`. Swaps happen only in `routing.py` (in) and `report.geojson` (out).
- **Money.** Floats inside the optimiser, Decimal from `report.bill` on. Gallons round to 3 dp, each stop's cost to
  the cent (`ROUND_HALF_UP`), and every total is the sum of the rounded stop figures, so the response adds up.
- **Caches.** Route and plan caches are keyed by provider and `FUEL["ROUTE_CACHE_VERSION"]`. Bump the version when
  the cached `Route` or the response shape changes. A cache outage must never fail a request (use `caching.safe_*`).
- **Tests never hit the network.** `tests/conftest.py` wraps every test in `respx.mock`, so an unmocked request
  fails. Routing fixtures are built with `tests/test_routing.py` helpers (`ors_body`, `osrm_body`, `line`).
- **The optimiser is checked against an exact MILP** on 400 random instances (`tests/test_optimizer.py`). Any change
  to `optimizer.py` must keep that test passing.
- **Data.** `data/` holds the supplied price list and the cleaned, geocoded outputs; migration `0002` loads them.
  Rebuild with `build_gazetteer --download` and `import_fuel_prices ... --geocode-missing`; don't hand-edit the
  `.csv.gz` files. Add hand-placed towns to `data/places_manual.csv` with the source noted.
- **Settings** live in `config/settings.py` under `FUEL` (vehicle, buffers, caching) and `ROUTING` (providers,
  timeouts). Read them from there; don't hard-code the same numbers in modules.

## Secrets and commits

- `.env` holds `ORS_API_KEY` and is gitignored. Never commit it, print the key, or put it in logs or responses.
- Commit subjects are short and descriptive (see `git log`), with no trailers.
