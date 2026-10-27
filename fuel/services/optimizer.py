"""Choosing where to refuel along a fixed route.

Pure functions, no Django. Fuel is tracked in *miles of range* (gallons = miles / mpg).

Model: a station sits `off` miles off the route at route mile `mile`. Stopping there costs a detour of `off`
miles out and `off` back, so driving from stop i to stop k uses  mile_k - mile_i + off_i + off_k  miles of fuel.
Driving past a station costs nothing. The objective is fuel cost + `stop_cost` dollars per stop: without a stop
cost the cheapest plan chases cents (10 stops coast to coast), and a truck stop costs driver time.

Algorithm: an exact dynamic program over the structure proven in Khuller, Malekian & Mestre, "To fill or not
to fill: the gas station problem" (2007). In some optimal plan, at every stop you either fill the tank or buy
just enough to reach the next stop. So the fuel on arrival at a stop is one of:
  - 0;
  - what's left of a fill-up at an earlier stop;
  - what's left of the starting fuel.
That gives O(n * w) states (w = stations within one tank), and each station's transitions are evaluated with
numpy. `tests/test_optimizer.py` checks it against a mixed-integer program on random instances.
"""

import math
from collections.abc import Hashable, Iterable
from dataclasses import dataclass

import numpy as np

EPS = 1e-6


@dataclass(frozen=True)
class Candidate:
    """A fuel station seen from the route."""

    key: Hashable
    mile: float  # where on the route you leave it for the station
    price: float  # USD per gallon
    off: float = 0.0  # miles from the route to the station (one way)


@dataclass(frozen=True)
class Purchase:
    candidate: Candidate
    miles: float  # miles of range bought
    fuel_on_arrival_miles: float


class InfeasibleRoute(Exception):
    """Some stretch of the route can't be covered on one tank."""

    def __init__(self, stranded_at_mile: float, next_point_mile: float, tank_miles: float):
        self.stranded_at_mile = stranded_at_mile
        self.next_point_mile = next_point_mile
        self.gap_miles = next_point_mile - stranded_at_mile
        super().__init__(
            f"no fuel stop reachable after mile {stranded_at_mile:.1f}: the next one is at mile "
            f"{next_point_mile:.1f} ({self.gap_miles:.1f} mi away, a tank holds {tank_miles:.0f} mi)"
        )


def _prepare(candidates: Iterable[Candidate], total_miles: float, tank_miles: float) -> list[Candidate]:
    """Drop unusable stations; at one (mile, off) keep only the cheapest. Sorted by mile."""
    best: dict[tuple[float, float], Candidate] = {}
    for c in candidates:
        if not (0 <= c.mile < total_miles - EPS and 0 <= c.off <= tank_miles / 2 and math.isfinite(c.price)):
            continue
        current = best.get((c.mile, c.off))
        if current is None or c.price < current.price:
            best[(c.mile, c.off)] = c
    return sorted(best.values(), key=lambda c: (c.mile, c.off))


def _validate(total_miles: float, tank_miles: float, start_fuel_miles: float) -> None:
    if total_miles < 0:
        raise ValueError("total_miles must be >= 0")
    if tank_miles <= 0:
        raise ValueError("tank_miles must be > 0")
    if not 0 <= start_fuel_miles <= tank_miles + EPS:
        raise ValueError("start_fuel_miles must be between 0 and tank_miles")


def _gap(pts: list[Candidate], total: float, tank: float, start: float) -> InfeasibleRoute:
    """Locate the first stretch nobody can cross, for the error message."""
    reach = start
    for p in pts:
        if p.mile + p.off > reach + EPS:
            break
        reach = max(reach, p.mile - p.off + tank)
    stranded = max(min(reach, total), 0.0)
    last = max((p.mile for p in pts if p.mile <= stranded + EPS), default=0.0)
    nxt = next((p.mile for p in pts if p.mile > stranded + EPS), total)
    return InfeasibleRoute(last, nxt, tank)


def plan_fuel_stops(
    candidates: Iterable[Candidate],
    total_miles: float,
    tank_miles: float,
    start_fuel_miles: float,
    mpg: float,
    stop_cost: float = 0.0,
) -> list[Purchase]:
    """Minimise fuel cost + `stop_cost` per stop for a trip of `total_miles` starting with `start_fuel_miles`."""
    _validate(total_miles, tank_miles, start_fuel_miles)
    pts = _prepare(candidates, total_miles, tank_miles)
    n, tank, start, total = len(pts), tank_miles, start_fuel_miles, total_miles
    miles = np.array([p.mile for p in pts], dtype=float)
    off = np.array([p.off for p in pts], dtype=float)
    per_mile = np.array([p.price for p in pts], dtype=float) / mpg  # USD per mile of range

    best_total, best_end = (0.0, ("start",)) if total <= start + EPS else (math.inf, None)
    best0 = np.full(n, math.inf)  # cheapest way to arrive at k with an empty tank
    parent0: list = [None] * n
    fill_cost = np.full(n, math.inf)  # cheapest way to leave k with a full tank
    fill_parent: list = [None] * n

    for k in range(n):
        # Arrival states at k.
        lo = int(np.searchsorted(miles, miles[k] - tank - EPS, "left"))
        back = np.arange(lo, k)
        legs = miles[k] - miles[back] + off[back] + off[k]
        keep = np.isfinite(fill_cost[back]) & (legs <= tank + EPS)
        back, legs = back[keep], legs[keep]
        g = np.concatenate([[0.0], tank - legs])
        c = np.concatenate([[best0[k]], fill_cost[back]])
        ids = [("Z", k)] + [("F", int(i), k) for i in back]
        from_start = miles[k] + off[k]
        if from_start <= start + EPS:
            g, c = np.append(g, start - from_start), np.append(c, 0.0)
            ids.append(("S", k))
        live = np.isfinite(c)
        if not live.any():
            continue
        g, c = np.maximum(g[live], 0.0), c[live]
        ids = [s for s, ok in zip(ids, live, strict=True) if ok]
        p = per_mile[k]

        # Option 1: fill the tank here.
        buy = np.maximum(tank - g, 0.0)
        cost = c + buy * p + stop_cost * (buy > EPS)
        j = int(np.argmin(cost))
        fill_cost[k], fill_parent[k] = cost[j], (ids[j], float(buy[j]))

        # Option 2: buy just enough for the next leg, to another station (arriving empty) or the destination.
        order = np.argsort(g)
        g_sorted = g[order]
        score = (c - g * p)[order]
        pref_arg = np.zeros(len(score), dtype=int)
        for t in range(1, len(score)):
            pref_arg[t] = t if score[t] < score[pref_arg[t - 1]] else pref_arg[t - 1]

        hi = int(np.searchsorted(miles, miles[k] + tank + EPS, "right"))
        targets = np.arange(k + 1, hi)
        if targets.size:
            d = miles[targets] - miles[k] + off[k] + off[targets]
            cut = np.searchsorted(g_sorted, d - EPS, "left")  # states with g < d
            ok = (cut > 0) & (d <= tank + EPS)
            sel = pref_arg[np.maximum(cut - 1, 0)]
            val = score[sel] + d * p + stop_cost
            better = ok & (val < best0[targets] - 1e-12)
            for t, s, v, dd in zip(targets[better], sel[better], val[better], d[better], strict=True):
                src = order[s]
                best0[t], parent0[t] = v, (k, ids[src], float(dd - g[src]))

        d = total - miles[k] + off[k]
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
        raise _gap(pts, total, tank, start)

    def arrival(state) -> float:
        kind = state[0]
        if kind == "Z":
            return 0.0
        if kind == "F":
            i, k = state[1], state[2]
            return max(tank - (miles[k] - miles[i] + off[i] + off[k]), 0.0)
        k = state[1]
        return max(start - miles[k] - off[k], 0.0)

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


def naive_plan(
    candidates: Iterable[Candidate],
    total_miles: float,
    tank_miles: float,
    start_fuel_miles: float,
) -> list[Purchase]:
    """Price-blind baseline: go to the farthest station a tank allows, fill up; top up just enough at the end."""
    _validate(total_miles, tank_miles, start_fuel_miles)
    pts = _prepare(candidates, total_miles, tank_miles)
    purchases: list[Purchase] = []
    here: Candidate | None = None
    fuel = arrival = start_fuel_miles

    def leg(b_mile: float, b_off: float) -> float:
        return b_mile - (here.mile if here else 0.0) + (here.off if here else 0.0) + b_off

    while True:
        reach = tank_miles if here is not None else fuel
        to_end = leg(total_miles, 0.0)
        if to_end <= reach + EPS:
            buy = max(0.0, to_end - fuel)
            if here is not None and buy > EPS:
                purchases.append(Purchase(here, buy, arrival))
            return purchases
        after = here.mile if here else -1.0
        options = [p for p in pts if p.mile > after and leg(p.mile, p.off) <= reach + EPS]
        if not options:
            raise _gap(pts, total_miles, tank_miles, start_fuel_miles)
        nxt = max(options, key=lambda p: (p.mile, -p.off))
        buy = max(0.0, tank_miles - fuel) if here is not None else 0.0
        if buy > EPS:
            purchases.append(Purchase(here, buy, arrival))
        fuel = max(0.0, fuel + buy - leg(nxt.mile, nxt.off))
        here, arrival = nxt, fuel


def cost_of(purchases: Iterable[Purchase], mpg: float) -> float:
    return sum(p.miles / mpg * p.candidate.price for p in purchases)
