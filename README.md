# Fuel Route API

A Django API that takes a start and a finish anywhere in the USA. It returns the route, where to buy fuel along
it, and what the fuel costs. The vehicle has a 500-mile range and gets 10 mpg, and prices come from the supplied
fuel price list.

```
GET /api/route?start=New York, NY&finish=Los Angeles, CA
→ 2,810 mi · 7 fuel stops · $873.68 of fuel · $85.24 cheaper than refuelling without looking at prices
→ 1 call to the routing API (0 when repeated) · ~50 ms when the route is cached
```

## The assignment, point by point

| Asked for | Where it is |
|---|---|
| Start and finish inside the USA | `start` and `finish` accept `City, ST`, a city name on its own, a ZIP code, `lat,lon` or an address. Places outside the US, and islands with no road to the mainland, get a 400 |
| A map of the route | `map.route` is the route as a GeoJSON line, included by default. `map.url` opens an interactive map, and every Postman request draws it in the *Visualize* tab |
| Cost-effective fuel stops, 500-mile range | `fuel_stops`, chosen by an exact optimiser. "Optimal" here means the lowest fuel cost plus $10 per stop, so the plan doesn't stop to save a cent. `stop_cost=0` gives the pure cheapest plan, and `cheapest_possible_fuel_cost` is always shown next to it |
| Total spent on fuel at 10 mpg | `total_fuel_cost` is the sum of the stop costs, to the cent. `total_gallons` is the gallons bought |
| Fast | About 50 ms when the route is cached. On a new route nearly all the time is the routing API (~1 s), and our own work takes ~30 ms (`meta.timings_ms`) |
| One routing call is ideal | One routing call per new route and zero for a repeat. The only exception is a route the price list can't fuel, which takes one more call for a detour (two at most). `meta.external_api_calls` reports the count on every response |
| Latest stable Django | Django 6.1.2 with Django REST Framework 3.18 |

## Run it (Python 3.12+)

**Option A: local, using SQLite. No API key or signup needed.**

```bash
make setup     # venv, dependencies, .env, then `migrate`, which also loads all station and place data (~8 s)
make run       # http://localhost:8000
```

**Option B: Docker, using PostgreSQL and Redis.**

```bash
docker compose up --build     # http://localhost:8000
```

Open **http://localhost:8000/map** for the map, or import `postman_collection.json` into Postman.

Routing uses the public OSRM server, so no key is needed. Put a free `ORS_API_KEY` in `.env` to use
OpenRouteService's truck profile instead. If ORS fails, the request falls back to OSRM once.

## API

`GET /api/route`, or `POST` with the same fields as JSON.

| Parameter | Default | |
|---|---|---|
| `start`, `finish` | required | `Chicago, IL`, `Chicago`, `60601`, `41.88,-87.63`, or an address (resolved to its city or ZIP; free text the offline data can't place costs one geocoding call) |
| `start_fuel` | `empty` | `empty` means the truck fills up at the cheapest station near the start before leaving. `full`, or a number of miles, means fuel is already in the tank |
| `stop_cost` | `10` | Dollars counted per refuelling stop, standing in for driver time. `0` gives the strict cheapest plan |
| `include_route` | `true` | `false` leaves out the route geometry (~25 KB per route) |

Response, trimmed:

```jsonc
{
  "start": {"query": "New York, NY", "label": "New York, NY", "lat": 40.662712, "lon": -73.938677, "resolved_by": "place"},
  "finish": {...},
  "distance_miles": 2810.4, "duration_hours": 50.31,
  "fuel_stops": [
    {"stop": 1, "name": "Fuel 4", "address": "US-9", "city": "Jersey City", "state": "NJ",
     "mile_marker": 0.0, "off_route_miles": 7.4, "price_per_gallon": 3.239, "gallons": 40.633, "cost": 131.61,
     "fuel_on_arrival_gallons": 0.0, "lat": 40.711417, "lon": -74.06476, ...},
    ...
  ],
  "total_fuel_cost": 873.68,
  "total_gallons": 283.389,
  "unpriced_gallons": 0.0, "cost_is_complete": true,   // see "When the price list runs out" below
  "cheapest_possible_fuel_cost": 865.6,     // the same trip with stop_cost=0 (10 stops)
  "savings_vs_naive": 85.24,                // vs. driving as far as a tank allows and filling up, price-blind
  "route_options": [{"name": "Direct route", "selected": true, "status": "ok", "distance_miles": 2810.4, ...}],
  "map": {"url": "http://localhost:8000/map?start=...", "route": {"type": "LineString", "coordinates": [...]}},
  "assumptions": {...}, "warnings": [],
  "meta": {"routing_provider": "osrm:driving", "external_api_calls": {"routing": 1, "geocoding": 0},
           "route_cache_hit": false, "timings_ms": {...}}
}
```

`GET /api/places?q=chic` returns up to 8 place names for autocomplete (`["Chicago, IL", "Chico, CA", ...]`), biggest first. It's offline, from the same place data, and every suggestion is accepted by `/api/route` as-is. The map page uses it for both inputs.

Errors all have the shape `{"error": {"code", "message", "details"}}`:
- **400:** bad, ambiguous or non-US input.
- **422:** there's no drivable route between the two points.
- **429:** too many requests (60 per minute per client).
- **502/503:** the routing or geocoding provider failed.

## How it works

```
start/finish ──► resolve offline (city / ZIP / lat,lon) ──► route cache ──miss──► 1 routing call
                                                                │
                 stations near the route (KD-tree) ◄────────────┘
                           │
                 exact optimiser ──► stops, cost, savings ──► JSON + map
                           │
                 a stretch longer than a tank? ──► detour through listed stations (+1 call, cached)
                                                    └─ none ──► plan with unpriced fuel for the gap
```

**Routing calls.**
- Places are resolved offline, from Census and GeoNames files committed to the repo. That leaves the route as the only external call.
- Only the route is cached (Redis, or a file cache), so changing `stop_cost` or `start_fuel` never triggers a new routing call.
- **Worst case:** an address the offline data can't place costs one geocoding call each for start and finish (cached for a day). An ORS outage costs one OSRM fallback call. A route the price list can't fuel costs one or two detour calls.

**Matching stations to the route.**
- All 6,626 stations are loaded into memory with one SQL query.
- The route is resampled every half mile and put in a KD-tree. A single vectorised query gives every station within 10 miles of the route its mile marker and its distance from the road.
- A stretch with no station in reach widens the search to 25, then 50 miles, before the API gives up.

**When the price list runs out** (`fuel/services/detour.py`).
- Some routes have a stretch longer than a tank with no listed station. San Francisco → Seattle up I-5 has none for the first 576 miles, because the list's 8 California stations are all near the Mexican border.
- The API then looks for a detour through listed stations. Towns with a station become a graph, linked where the estimated road distance fits in a tank. Dijkstra finds the shortest chain, and one more routing call goes through its waypoints. The plan is then re-checked on the real road distances. A tighter estimate is tried first, then a cautious one, so a detour costs at most two extra calls.
- `route_options` lists the direct route first. When it can't be fuelled from the list it is marked `out_of_range`, with the gap (`from_mile`, `to_mile` and its geometry), and the detour that's used is `selected`. SF → Seattle goes via Sparks, NV and Madras, OR: +141 mi, every leg in range.
- With no workable detour, the API plans the direct route anyway. Fuel bought where nothing listed is in range appears in `fuel_stops` with `"listed": false` and no price, and is counted in `unpriced_gallons`. The listed purchases make up `total_fuel_cost`, and `cost_is_complete` is `false`. The same applies when there's no listed station near the start, as in most of California: the plan buys only enough unlisted fuel to reach the first listed station.

**Choosing the stops** (`fuel/services/optimizer.py`).
- A stop costs a detour from the road and back, and that detour is charged against both range and fuel cost.
- The objective is fuel cost plus `stop_cost` for each stop, solved exactly with a dynamic program.
  - It relies on the structure proven in *Khuller, Malekian & Mestre, "To fill or not to fill" (2007)*: at every stop, an optimal plan either fills the tank or buys just enough to reach the next stop. That keeps the fuel levels worth tracking to a small set.
  - NYC → LA takes about 25 ms.
- The tests check it against an exact mixed-integer program (`scipy.optimize.milp`) on 400 random routes, and replay each plan to confirm the tank never runs dry or overflows.
- Without a stop cost, the cheapest NYC → LA plan has 10 stops; with the $10 default it has 7, for 0.9% more fuel.

## The data, and what needed fixing

| Problem in the raw file | What the import does |
|---|---|
| **No coordinates.** Addresses look like `I-44, EXIT 283 & US-69` | Each station is placed at its town's point. Towns are matched offline against Census 2026 places, then GeoNames, then Census county subdivisions. The rest come from 3 Nominatim lookups and 3 entered by hand (`data/places_manual.csv`). All 6,626 US stations are placed |
| **620 Canadian rows** (ON, AB, BC…) | Dropped: the task is US only |
| **The same station at different prices** (487 stations) | Every price is kept as a `StationPrice` row, and the planner uses the lowest |
| City names padded with spaces; `St.` vs `Saint`; `LaGrange` vs `La Grange`; `S Coffeyville` | Names are compared through a normalised key (`fuel/services/names.py`) |
| **California has only 8 stations** (16 price rows), all in the far south-east | San Francisco → Seattle has a 576-mile stretch with no listed station. The API detours through Nevada and Oregon and shows the direct route as out of range (see above) |

To rebuild everything from the sources, run:

```bash
manage.py build_gazetteer --download
manage.py import_fuel_prices data/fuel-prices.csv --geocode-missing
```

The outputs are committed, so `migrate` is the only setup step. Restart the server after re-importing, because each process keeps the station index in memory.

## Data model

```
Place (name, key, state, lat, lon, population, source)   unique(key, state)
  └── Station (opis_id PK, name, address, highway, exit_number)
        └── StationPrice (rack_id, price DECIMAL)  unique(station, rack_id, price), price > 0
ZipCode (code PK, lat, lon)
```

## Assumptions and limitations

- **Station locations.** The price list has no coordinates, so each station is placed at the centre of its town. Its distance from the route (`off_route_miles`) is approximate.
- **Starting fuel.** With the default `start_fuel=empty`, the trip starts by filling up at the cheapest station within 10 miles of the start (25 miles if there isn't one), and the drive to that station isn't billed. Where there's no station that close (most of California), the first purchase is unpriced fuel at the start, just enough to reach a listed station, and `warnings` says how much.
- **Detours** are only chosen to stay on listed stations. A real truck would refuel in California; the direct route stays in `route_options` with its distance.
- **Prices** are treated as fixed for the trip, and the tank may run down to empty exactly at a station.
- **Route type.** Without an ORS key the route is OSRM's car route.
- **Border coordinates.** `lat,lon` input is checked against US ZIP centroids, so a point just across the border (Windsor, Tijuana) can pass. City and ZIP input can't.

## Tests

```bash
make test    # ~80 tests plus 400 randomised checks against the exact solver; no network access
make live    # optional: a test against the real routing API
```

Every HTTP call is mocked, and an unmocked call fails the test. Coverage:
- the optimiser against the MILP, including detours;
- cleaning the price list, and normalising place names;
- detours through listed stations, and unpriced fuel when there's no detour;
- resolving input, including rejecting "Toronto, Ontario" before a US-only geocoder turns it into Ontario, California;
- routing request shape and `[lon, lat]` order;
- malformed responses from the provider, the fallback, and a Redis or cache outage;
- the one-query station load;
- the API contract.

## Layout

```
config/                         settings (DATABASE_URL, REDIS_URL, ORS_API_KEY, DJANGO_*), wsgi
fuel/models.py                  Place, Station, StationPrice, ZipCode
fuel/services/
  resolver.py                   text → point (offline first)
  routing.py                    the only module that calls a routing API
  stations.py                   in-memory station index + route matching
  optimizer.py                  exact stop-cost optimiser (+ price-blind baseline)
  detour.py                     waypoints through listed stations when the direct route can't be fuelled
  planner.py                    one request end to end: caching, response
fuel/serializers.py, views.py   API input and endpoint; errors.py for the error shape
fuel/templates/fuel/map.html    the map page
fuel/management/commands/       build_gazetteer, import_fuel_prices
fuel/migrations/0002_*          loads the committed data on `migrate`
data/                           the price list + cleaned, geocoded outputs
```

## Data sources

- **Fuel prices:** the price list supplied with the assignment (`data/fuel-prices.csv`).
- **Places and ZIP codes:** U.S. Census Bureau, 2026 Gazetteer Files (public domain).
- **Places:** GeoNames (geonames.org), licensed CC BY 4.0.
- **Routes, three town locations and the `/map` tiles:** © OpenStreetMap contributors (ODbL), via OSRM and Nominatim.
- **Postman map tiles:** © Esri (Postman's sandbox can't send the Referer OSM's tile servers require).
- **Optional truck routing:** openrouteservice.org.

The code is MIT licensed (see `LICENSE`).
