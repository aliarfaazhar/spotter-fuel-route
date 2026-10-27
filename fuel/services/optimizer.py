"""Cost-optimal refuelling along a fixed route.

Pure functions over mile markers and prices: no Django, no I/O. Fuel is tracked in *miles of range*
(gallons = miles / mpg), so the vehicle's efficiency never leaks into the algorithm.

Algorithm (the classic "gas station problem" greedy, optimal for linear prices on a fixed path):

* The destination is a sentinel station priced 0, so arriving there is always "cheaper".
* At a station, look for the nearest station within a full tank that is strictly cheaper.
  - If there is one, buy only enough to reach it.
  - Otherwise fill the tank and move to the cheapest station in reach (farthest one on a tie).
* The start is a point where nothing can be bought, so its reach is the fuel already in the tank.

`tests/test_optimizer.py` checks this against an exact linear program on random instances.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable
from dataclasses import dataclass

EPS = 1e-6


@dataclass(frozen=True)
class Candidate:
    """A fuel station projected onto the route."""

    key: Hashable
    mile: float
    price: float  # USD per gallon


@dataclass(frozen=True)
class Purchase:
    candidate: Candidate
    miles: float  # miles of range bought
    fuel_on_arrival_miles: float


class InfeasibleRoute(Exception):
    """No station is reachable before the tank runs dry."""

    def __init__(self, stranded_at_mile: float, next_point_mile: float, tank_miles: float):
        self.stranded_at_mile = stranded_at_mile
        self.next_point_mile = next_point_mile
        self.gap_miles = next_point_mile - stranded_at_mile
        super().__init__(
            f"no fuel stop reachable after mile {stranded_at_mile:.1f}: next stop is at mile "
            f"{next_point_mile:.1f} ({self.gap_miles:.1f} mi away, tank holds {tank_miles:.0f} mi)"
        )


_SENTINEL = object()


def _prepare(candidates: Iterable[Candidate], total_miles: float) -> list[Candidate]:
    """Sort by mile, keep only the cheapest station per mile marker, append the destination sentinel."""
    best_at_mile: dict[float, Candidate] = {}
    for c in candidates:
        if c.mile < 0 or c.mile >= total_miles - EPS or not math.isfinite(c.price):
            continue
        current = best_at_mile.get(c.mile)
        if current is None or c.price < current.price:
            best_at_mile[c.mile] = c
    points = sorted(best_at_mile.values(), key=lambda c: c.mile)
    points.append(Candidate(key=_SENTINEL, mile=total_miles, price=0.0))
    return points


def _validate(total_miles: float, tank_miles: float, start_fuel_miles: float) -> None:
    if total_miles < 0:
        raise ValueError("total_miles must be >= 0")
    if tank_miles <= 0:
        raise ValueError("tank_miles must be > 0")
    if not 0 <= start_fuel_miles <= tank_miles + EPS:
        raise ValueError("start_fuel_miles must be between 0 and tank_miles")


def plan_fuel_stops(
    candidates: Iterable[Candidate],
    total_miles: float,
    tank_miles: float,
    start_fuel_miles: float,
) -> list[Purchase]:
    """Return the cheapest set of purchases that gets the vehicle from mile 0 to `total_miles`."""
    _validate(total_miles, tank_miles, start_fuel_miles)
    points = _prepare(candidates, total_miles)

    purchases: list[Purchase] = []
    here_mile, here_price, here = 0.0, math.inf, None  # the start: nothing can be bought here
    fuel = start_fuel_miles
    arrival_fuel = fuel
    next_idx = 0

    while True:
        reach = tank_miles if here is not None else fuel
        cheaper_idx = None
        cheapest_idx = None
        j = next_idx
        while j < len(points) and points[j].mile - here_mile <= reach + EPS:
            if points[j].price < here_price:
                cheaper_idx = j
                break
            if cheapest_idx is None or points[j].price <= points[cheapest_idx].price:
                cheapest_idx = j
            j += 1

        if cheaper_idx is not None:
            target = cheaper_idx
            leg = points[target].mile - here_mile
            buy = max(0.0, leg - fuel)
        elif cheapest_idx is not None:
            target = cheapest_idx
            leg = points[target].mile - here_mile
            buy = max(0.0, tank_miles - fuel)
        else:
            next_mile = points[next_idx].mile if next_idx < len(points) else total_miles
            raise InfeasibleRoute(here_mile, next_mile, tank_miles)

        if here is not None and buy > EPS:
            purchases.append(Purchase(candidate=here, miles=buy, fuel_on_arrival_miles=arrival_fuel))
        fuel = max(0.0, fuel + buy - leg)

        destination = points[target]
        if destination.key is _SENTINEL:
            return purchases
        here_mile, here_price, here = destination.mile, destination.price, destination
        arrival_fuel = fuel
        next_idx = target + 1


def naive_plan(
    candidates: Iterable[Candidate],
    total_miles: float,
    tank_miles: float,
    start_fuel_miles: float,
) -> list[Purchase]:
    """Price-blind baseline: drive to the farthest station in reach and fill up; top up just enough at the end."""
    _validate(total_miles, tank_miles, start_fuel_miles)
    points = _prepare(candidates, total_miles)

    purchases: list[Purchase] = []
    here_mile, here = 0.0, None
    fuel = start_fuel_miles
    arrival_fuel = fuel
    next_idx = 0

    while True:
        reach = tank_miles if here is not None else fuel
        if total_miles - here_mile <= reach + EPS:
            buy = max(0.0, (total_miles - here_mile) - fuel)
            if here is not None and buy > EPS:
                purchases.append(Purchase(candidate=here, miles=buy, fuel_on_arrival_miles=arrival_fuel))
            return purchases

        farthest = None
        j = next_idx
        while j < len(points) - 1 and points[j].mile - here_mile <= reach + EPS:
            farthest = j
            j += 1
        if farthest is None:
            next_mile = points[next_idx].mile if next_idx < len(points) else total_miles
            raise InfeasibleRoute(here_mile, next_mile, tank_miles)

        buy = max(0.0, tank_miles - fuel) if here is not None else 0.0
        if buy > EPS:
            purchases.append(Purchase(candidate=here, miles=buy, fuel_on_arrival_miles=arrival_fuel))
        leg = points[farthest].mile - here_mile
        fuel = max(0.0, fuel + buy - leg)
        here_mile, here = points[farthest].mile, points[farthest]
        arrival_fuel = fuel
        next_idx = farthest + 1


def cost_of(purchases: Iterable[Purchase], mpg: float) -> float:
    return sum(p.miles / mpg * p.candidate.price for p in purchases)


def plan_fuel_stops_with_stop_cost(
    candidates: Iterable[Candidate],
    total_miles: float,
    tank_miles: float,
    start_fuel_miles: float,
    mpg: float,
    stop_cost: float,
) -> list[Purchase]:
    """Minimise fuel cost + `stop_cost` (USD) per refuelling stop. Exact; equals `plan_fuel_stops` at stop_cost=0.

    Without a stop cost the cheapest plan chases every cent (17 stops coast to coast, some buying a gallon).
    A truck stop costs driver time, so we price it in. This is a DP over the structure proven in
    Khuller, Malekian & Mestre, "To fill or not to fill" (2007): some optimal plan, at every stop, either
    fills the tank or buys just enough to reach the next stop. So the fuel on arrival at a station is
    0, what's left of a fill-up at an earlier station, or what's left of the starting fuel.
    That gives O(n * w) states (w = stations within one tank), each relaxed with numpy.
    """
    import numpy as np

    _validate(total_miles, tank_miles, start_fuel_miles)
    pts = _prepare(candidates, total_miles)[:-1]
    n, tank, start, total = len(pts), tank_miles, start_fuel_miles, total_miles
    miles = np.array([p.mile for p in pts], dtype=float)
    per_mile = np.array([p.price for p in pts], dtype=float) / mpg  # USD per mile of range

    best_total, best_end = (0.0, ("start",)) if total <= start + EPS else (math.inf, None)
    best0 = np.full(n, math.inf)  # cheapest way to arrive at k with an empty tank
    parent0: list = [None] * n
    fill_cost = np.full(n, math.inf)  # cheapest way to leave k with a full tank
    fill_parent: list = [None] * n

    for k in range(n):
        lo = int(np.searchsorted(miles, miles[k] - tank - EPS, "left"))
        back = np.arange(lo, k)
        back = back[np.isfinite(fill_cost[back])]
        g = np.concatenate([[0.0], tank - (miles[k] - miles[back])])
        c = np.concatenate([[best0[k]], fill_cost[back]])
        ids = [("Z", k)] + [("F", int(i), k) for i in back]
        if miles[k] <= start + EPS:
            g, c = np.append(g, start - miles[k]), np.append(c, 0.0)
            ids.append(("S", k))
        live = np.isfinite(c)
        if not live.any():
            continue
        g, c = g[live], c[live]
        ids = [s for s, ok in zip(ids, live, strict=True) if ok]
        p = per_mile[k]

        # Option 1: fill the tank here.
        buy = np.maximum(tank - g, 0.0)
        cost = c + buy * p + stop_cost * (buy > EPS)
        j = int(np.argmin(cost))
        fill_cost[k], fill_parent[k] = cost[j], (ids[j], float(buy[j]))

        # Option 2: buy just enough to reach a later station (arrive empty) or the destination.
        order = np.argsort(g)
        g_sorted = g[order]
        score = (c - g * p)[order]
        pref_arg = np.zeros(len(score), dtype=int)
        for t in range(1, len(score)):
            pref_arg[t] = t if score[t] < score[pref_arg[t - 1]] else pref_arg[t - 1]

        hi = int(np.searchsorted(miles, miles[k] + tank + EPS, "right"))
        targets = np.arange(k + 1, hi)
        if targets.size:
            d = miles[targets] - miles[k]
            cut = np.searchsorted(g_sorted, d - EPS, "left")  # states with g < d
            ok = cut > 0
            sel = pref_arg[np.maximum(cut - 1, 0)]
            val = score[sel] + d * p + stop_cost
            better = ok & (val < best0[targets] - 1e-12)
            for t, s, v, dd in zip(targets[better], sel[better], val[better], d[better], strict=True):
                src = order[s]
                best0[t], parent0[t] = v, (k, ids[src], float(dd - g[src]))

        d = total - miles[k]
        if d <= tank + EPS:
            enough = g >= d - EPS
            if enough.any():
                j = int(np.argmin(np.where(enough, c, math.inf)))
                if c[j] < best_total:
                    best_total, best_end = c[j], ("stop", k, ids[j], 0.0)
            cut = int(np.searchsorted(g_sorted, d - EPS, "left"))
            if cut > 0:
                src = order[pref_arg[cut - 1]]
                val = c[src] + (d - g[src]) * p + stop_cost
                if val < best_total:
                    best_total, best_end = val, ("stop", k, ids[src], float(d - g[src]))

    if best_end is None:
        plan_fuel_stops(pts, total_miles, tank_miles, start_fuel_miles)  # raises InfeasibleRoute with the gap
        raise InfeasibleRoute(0.0, total_miles, tank_miles)  # pragma: no cover

    def arrival(state) -> float:
        kind = state[0]
        if kind == "Z":
            return 0.0
        if kind == "F":
            return tank - (miles[state[2]] - miles[state[1]])
        return start - miles[state[1]]

    purchases: list[Purchase] = []
    if best_end[0] == "stop":
        _, k, state, buy = best_end
        if buy > EPS:
            purchases.append(Purchase(pts[k], buy, arrival(state)))
        while state[0] != "S":
            if state[0] == "Z":
                k, state, buy = parent0[state[1]]
            else:
                k = state[1]
                state, buy = fill_parent[k]
            if buy > EPS:
                purchases.append(Purchase(pts[k], buy, arrival(state)))
    purchases.reverse()
    return purchases
