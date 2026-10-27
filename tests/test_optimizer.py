import math
import random

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint, milp

from fuel.services.optimizer import Candidate, InfeasibleRoute, cost_of, naive_plan, plan_fuel_stops

MPG = 10.0
TANK = 500.0


def c(mile, price, key=None, off=0.0):
    return Candidate(key=key if key is not None else f"s@{mile}", mile=mile, price=price, off=off)


def plan(stations, total, start, stop_cost=0.0, tank=TANK):
    return plan_fuel_stops(stations, total, tank, start, MPG, stop_cost)


def bought(purchases):
    return [(p.candidate.mile, round(p.miles, 6)) for p in purchases]


def test_trip_within_starting_fuel_needs_no_stops():
    assert plan([c(100, 3.0)], 400, TANK) == []


def test_last_leg_buys_only_what_is_needed():
    # Full tank, one station at 450, destination at 900: buy 400 mi, not a full tank.
    purchases = plan([c(450, 3.0)], 900, TANK)
    assert bought(purchases) == [(450, 400.0)]
    assert cost_of(purchases, MPG) == pytest.approx(120.0)


def test_goes_to_nearest_cheaper_station_not_cheapest_in_reach():
    # Empty tank at a $3.00 origin; $2.90 at mile 100; $2.00 at mile 400; destination 800.
    purchases = plan([c(0, 3.0), c(100, 2.9), c(400, 2.0)], 800, 0)
    assert bought(purchases) == [(0, 100.0), (100, 300.0), (400, 400.0)]


def test_fills_up_when_nothing_cheaper_is_in_reach():
    purchases = plan([c(0, 2.0), c(300, 3.0), c(600, 3.5)], 900, 0)
    # Fill at $2.00; at $3.00 nothing cheaper is in reach, so fill up; top up just enough at $3.50.
    assert bought(purchases) == [(0, 500.0), (300, 300.0), (600, 100.0)]


def test_start_with_partial_tank_cannot_buy_at_origin():
    assert bought(plan([c(50, 3.0)], 400, 60)) == [(50, 340.0)]


def test_gap_of_exactly_tank_range_is_feasible():
    assert bought(plan([c(500, 3.0)], 1000, TANK)) == [(500, 500.0)]


def test_gap_larger_than_tank_is_infeasible():
    with pytest.raises(InfeasibleRoute) as exc:
        plan([c(300, 3.0), c(900, 3.0)], 1200, TANK)
    assert exc.value.stranded_at_mile == 300
    assert exc.value.gap_miles == pytest.approx(600)


def test_cheapest_station_wins_when_two_share_a_location():
    purchases = plan([c(450, 3.0, "a"), c(450, 2.5, "b")], 900, TANK)
    assert [p.candidate.key for p in purchases] == ["b"]


def test_stations_past_destination_are_ignored():
    assert plan([c(450, 1.0), c(1000, 0.5)], 450, TANK) == []


def test_detour_counts_against_range():
    # 20 mi of fuel; the only station is 26 mi off the route at mile 0: unreachable.
    with pytest.raises(InfeasibleRoute):
        plan([c(0, 3.0, off=26)], 300, 20)
    # Mile 497 + 5 mi off route = 502 mi on a 500 mi tank: out of reach even though mile 497 isn't.
    purchases = plan([c(497, 3.0, off=5), c(490, 3.5)], 800, TANK)
    assert purchases[0].candidate.mile == 490  # must stop at 490 first; then the cheaper 497 is in reach


def test_detour_fuel_is_paid_for():
    purchases = plan([c(450, 3.0, off=10)], 900, TANK)
    # reach the station: 450 + 10 = 460 used, 40 left; need 10 back + 450 to go = 460 -> buy 420.
    assert bought(purchases) == [(450, 420.0)]
    assert purchases[0].fuel_on_arrival_miles == pytest.approx(40.0)


def test_stop_cost_merges_penny_stops():
    stations = [c(0, 3.00), c(10, 2.99), c(300, 3.10)]
    assert len(plan(stations, 600, 0, stop_cost=20.0)) == 2
    assert len(plan(stations, 600, 0, stop_cost=0.0)) == 3


def test_naive_never_beats_optimal():
    stations = [c(0, 3.2), c(120, 2.8, off=3), c(390, 3.9), c(480, 3.1, off=1), c(700, 2.7)]
    opt = cost_of(plan(stations, 1100, 0), MPG)
    naive = cost_of(naive_plan(stations, 1100, TANK, 0), MPG)
    assert opt <= naive + 1e-9


# --- Exact optimum via mixed-integer programming ------------------------------------------------


def milp_optimal_cost(stations, total, tank, start, stop_cost):
    """min sum(p_k b_k)/mpg + stop_cost sum(y_k), with detour 2*off_k paid only at stops (y_k = 1)."""
    best = {}
    for s in stations:
        if 0 <= s.mile < total - 1e-6 and s.off <= tank / 2:
            k = (s.mile, s.off)
            if k not in best or s.price < best[k].price:
                best[k] = s
    pts = sorted(best.values(), key=lambda s: (s.mile, s.off))
    n = len(pts)
    if n == 0:
        return 0.0 if start >= total - 1e-6 else math.inf
    rows, lo, hi = [], [], []

    def fuel_at(k):  # coefficients of fuel left on reaching route mile m_k (before any detour at k)
        row = [0.0] * (2 * n)
        for i in range(k):
            row[i] = 1.0
            row[n + i] = -2.0 * pts[i].off
        return row

    for k, p in enumerate(pts):
        arrive = fuel_at(k)
        arrive[n + k] -= p.off  # going out to station k (only if stopping)
        rows.append(arrive), lo.append(p.mile - start), hi.append(np.inf)
        after_buy = list(arrive)
        after_buy[k] += 1.0
        rows.append(after_buy), lo.append(-np.inf), hi.append(tank - start + p.mile)
        link = [0.0] * (2 * n)
        link[k], link[n + k] = 1.0, -tank  # b_k <= tank * y_k
        rows.append(link), lo.append(-np.inf), hi.append(0.0)
        reach = fuel_at(k)  # never run dry driving past mile m_k
        rows.append(reach), lo.append(p.mile - start), hi.append(np.inf)
    end = fuel_at(n)
    rows.append(end), lo.append(total - start), hi.append(np.inf)
    cost = [p.price / MPG for p in pts] + [stop_cost] * n
    res = milp(
        cost,
        constraints=LinearConstraint(np.array(rows), lo, hi),
        integrality=[0] * n + [1] * n,
        bounds=Bounds([0] * (2 * n), [np.inf] * n + [1] * n),
    )
    return res.fun if res.status == 0 else math.inf


@pytest.mark.parametrize("seed", range(400))
def test_matches_milp(seed):
    rng = random.Random(seed)
    total = rng.uniform(50, 1800)
    n = rng.randint(0, 10)
    stations = [
        c(
            round(rng.uniform(0, total), rng.choice([0, 1])),
            round(rng.uniform(2.5, 4.5), rng.choice([1, 2, 3])),
            i,
            off=rng.choice([0.0, 0.0, round(rng.uniform(0, 30), 1)]),
        )
        for i in range(n)
    ]
    if rng.random() < 0.4:
        stations.append(c(0.0, round(rng.uniform(2.5, 4.5), 3), "origin"))
    start = rng.choice([0.0, TANK, round(rng.uniform(0, TANK), 1)])
    stop_cost = rng.choice([0.0, 0.0, 5.0, 25.0, round(rng.uniform(0, 60), 2)])

    expected = milp_optimal_cost(stations, total, TANK, start, stop_cost)
    try:
        purchases = plan(stations, total, start, stop_cost)
        got = cost_of(purchases, MPG) + stop_cost * len(purchases)
    except InfeasibleRoute:
        got = math.inf
    if math.isinf(expected):
        assert math.isinf(got)
    else:
        assert got == pytest.approx(expected, rel=1e-6, abs=1e-5)
        replay(purchases, total, start)


def replay(purchases, total, start):
    """Drive the plan: the tank never goes negative or over capacity."""
    fuel, at = start, 0.0
    for p in purchases:
        fuel -= p.candidate.mile - at + p.candidate.off
        assert fuel >= -1e-6
        assert fuel == pytest.approx(p.fuel_on_arrival_miles, abs=1e-6)
        fuel += p.miles
        assert fuel <= TANK + 1e-6
        fuel -= p.candidate.off
        at = p.candidate.mile
    assert fuel - (total - at) >= -1e-6
