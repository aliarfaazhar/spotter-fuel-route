# Fuel Route API

A Django API that takes a start and finish anywhere in the USA and returns the route, where to buy fuel along it
and what the fuel costs. The vehicle has a 500-mile range and gets 10 mpg; prices come from the supplied OPIS
fuel price list.

```
GET /api/route?start=New York, NY&finish=Los Angeles, CA
→ 2,810 mi · 7 fuel stops · $860.19 of fuel · $61.31 cheaper than refuelling without looking at prices
→ 1 call to the routing API (0 on a repeat request) · ~10 ms when the route is cached
```

## Run it

**Option A: local (SQLite, no API key, no signup)**

```bash
make setup     # Python 3.12 venv, dependencies, `migrate` (loads all station and place data, ~10 s)
make run       # http://localhost:8000
```

**Option B: Docker (PostgreSQL + Redis)**

```bash
docker compose up --build     # http://localhost:8000
```

Then open **http://localhost:8000/map** for the map, or import `postman_collection.json` into Postman.
The NYC → LA request there renders the map in Postman's *Visualize* tab.

Routing uses the public OSRM server by default, so it works with no key. Set `ORS_API_KEY` in `.env` (free at
openrouteservice.org) to route with OpenRouteService's **truck profile** instead. If ORS is down, the API falls
back to OSRM once.

## API

`GET /api/route` (or `POST` with the same fields as JSON)

| Parameter | Default | |
|---|---|---|
| `start`, `finish` | required | `City, ST`, `City, State`, a city on its own (if it's unambiguous), a ZIP code, a street address, or `lat,lon` |
| `start_fuel` | `empty` | `empty`, `full`, or miles of range already in the tank |
| `stop_cost` | `10` | Dollars charged per refuelling stop (driver time). `0` gives the strict cheapest plan |
| `include` | | `route` adds the route as a GeoJSON LineString |

Response (trimmed):

```jsonc
{
  "distance_miles": 2810.4, "duration_hours": 50.31,
  "fuel_stops": [
    {"stop": 1, "name": "BOLLA MARKET", "address": "I-95, EXIT 13", "city": "Elizabeth", "state": "NJ",
     "mile_marker": 0.0, "price_per_gallon": 3.099, "gallons": 39.601, "cost": 122.72, "lat": 40.666, "lon": -74.194, ...},
    ...
  ],
  "total_fuel_cost": 860.19,              // sum of the stop costs, to the cent
  "cheapest_possible_fuel_cost": 852.48,  // the same trip with stop_cost=0
  "savings_vs_naive": 61.31,              // vs. driving as far as possible and filling up
  "fuel_purchased_gallons": 281.036, "trip_fuel_gallons": 281.036,
  "assumptions": {...}, "warnings": [],
  "map_url": "http://localhost:8000/map?start=...", "google_maps_url": "https://www.google.com/maps/dir/...",
  "routing_provider": "osrm:driving",
  "external_api_calls": {"routing": 1, "geocoding": 0},
  "route_cache_hit": false,
  "timings_ms": {"resolve": 1.1, "routing": 1210.4, "match": 4.5, "optimise": 11.3, "total": 1228.0}
}
```

Errors share one shape, `{"error": {"code", "message", "details"}}`:
- **400:** bad, ambiguous or non-US input.
- **422:** no drivable route, or the price list has no station to cover part of the route.
- **502/503:** the routing provider failed or rate-limited the request.

## How it works

```
start/finish ──► resolve offline (ZIP / city / lat,lon) ──► route cache ──miss──► 1 routing call
                                                                │
              stations near the route (KD-tree) ◄──────────────┘
                          │
              optimiser (exact) ──► stops, cost, savings ──► JSON / map
```

**Calls to the routing API: 1 per new route, 0 for a repeat.**
- Places are resolved from offline data, so the only external call is the route. Stations and city names are
  located from Census and GeoNames files committed to the repo.
- Only the route is cached (in Redis or a file cache), so changing `stop_cost` or `start_fuel` never triggers a
  new routing call.
- A free-text address that can't be resolved offline costs one geocoding call, shown in `external_api_calls`.

**Matching stations to the route.**
- All 6,626 stations are held in memory, loaded with one SQL query.
- The route is resampled every half mile and put in a KD-tree. One vectorised query then gives each station
  within 10 miles of the route its mile marker and off-route distance.
- If a stretch has no station within reach, the search widens to 25, then 50 miles before giving up.

**Choosing the stops.** `fuel/services/optimizer.py`
- With `stop_cost=0` it is the classic "gas station problem" greedy. At each station it buys just enough fuel to
  reach the nearest cheaper station within range. If there isn't one, it fills up and moves to the cheapest
  station in range.
- That plan is the cheapest possible, but it chases cents: 18 stops coast to coast, some buying one gallon. So the
  default adds a $10 cost per stop and solves it **exactly** with a dynamic program. It uses the structure proven
  in *Khuller, Malekian & Mestre, "To fill or not to fill" (2007)*: at each stop, an optimal plan either fills the
  tank or buys just enough to reach the next stop. NYC → LA drops to 7 stops for 0.9% more fuel.
- Both are tested against an exact solver on random routes:
  - the greedy against a linear program (`scipy.optimize.linprog`), on 500 random instances;
  - the DP against a mixed-integer program (`scipy.optimize.milp`), on 300 random instances.

## The data, and what had to be fixed

Profiling the CSV turned up several problems:

| Problem in the raw file | Handling |
|---|---|
| **No coordinates at all.** Addresses look like `I-44, EXIT 283 & US-69` | Each station is placed at its town's point, matched offline against the Census 2026 Gazetteer (places + county subdivisions), then GeoNames, then 3 Nominatim lookups and 3 by hand (`data/places_manual.csv`). **All 6,626 US stations located** |
| **620 Canadian rows** (ON, AB, BC…) | Dropped; the task is US only |
| **Same station ID at different prices** (487 stations) | Every quote is kept as a `StationPrice` row; the planner uses the lowest |
| City names padded with spaces, `St.`/`Saint`, `LaGrange`/`La Grange`, `S Coffeyville` | Normalised name keys (`fuel/services/names.py`) |
| **California has only 16 stations, all in Imperial County** | San Francisco → Seattle has a 576-mile stretch with no listed station. The API returns 422 with the gap, rather than an incomplete plan |

Rebuild everything from scratch with `manage.py build_gazetteer --download` and then
`manage.py import_fuel_prices data/fuel-prices.csv --geocode-missing`.
The outputs are committed, so `migrate` is the only setup step.

## Data model

```
Place (name, key, state, lat, lon, source)      unique(key, state)
  └── Station (opis_id PK, name, address, highway, exit_number)
        └── StationPrice (rack_id, price DECIMAL)  unique(station, rack_id, price), price > 0
ZipCode (code PK, lat, lon)
```

## Assumptions and limitations

- **Start fuel.**
  - By default the tank starts empty. The trip begins by buying at the cheapest station within 25 mi of the start,
    so short trips have a real cost.
  - If there's no station that close (for example most of California), the API assumes a full tank and says so
    in `warnings`.
- **Station positions.** A station is placed at its town's centre, so `off_route_miles` is approximate. Detour
  miles to reach a station aren't added to the fuel bill.
- **Prices** are treated as fixed for the whole trip, and there's no reserve: the tank may run down to empty
  exactly at a station.
- **Truck routing.** Without an ORS key the route is OSRM's car route, not a truck route.

## Tests

```bash
make test    # 850+ tests, no network: every HTTP call is mocked and an unmocked call fails the test
make live    # optional tests against the real routing API
```

They cover:
- the optimisers against the exact LP and MILP solvers;
- feed cleaning;
- place-name normalisation;
- input resolution, including rejecting "Toronto, Ontario" before a US-only geocoder turns it into Ontario, California;
- routing request shape and `[lon, lat]` handling;
- error mapping and the OSRM fallback;
- the route cache;
- the one-query station load (`assertNumQueries`);
- the API contract.

## Layout

```
config/                      settings (env-driven: DATABASE_URL, REDIS_URL, ORS_API_KEY)
fuel/models.py               Place, Station, StationPrice, ZipCode
fuel/services/
  resolver.py                text → point (offline first)
  routing.py                 the only module that calls a routing API
  stations.py                in-memory station index + route matching
  optimizer.py               greedy (cheapest) + exact stop-cost DP
  planner.py                 orchestration, caching, response
fuel/management/commands/    build_gazetteer, import_fuel_prices
fuel/migrations/0002_*       loads the committed data on `migrate`
data/                        source CSV + cleaned, geocoded outputs
```

Built with Django 6.1.2, Django REST Framework 3.18, numpy/scipy, PostgreSQL or SQLite, and Redis or a file cache.
