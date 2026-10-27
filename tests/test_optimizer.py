import math
import random

import numpy as np
import pytest
from scipy.optimize import linprog

from fuel.services.optimizer import Candidate, InfeasibleRoute, cost_of, naive_plan, plan_fuel_stops

MPG = 10.0
TANK = 500.0


def c(mile, price, key=None):
    return Candidate(key=key or f"s@{mile}", mile=mile, price=price)


def bought(purchases):
    return [(p.candidate.mile, round(p.miles, 6)) for p in purchases]


def test_trip_within_starting_fuel_needs_no_stops():
    assert plan_fuel_stops([c(100, 3.0)], total_miles=400, tank_miles=TANK, start_fuel_miles=TANK) == []


def test_last_leg_buys_only_what_is_needed():
    # Full tank, one station at 450, destination at 900: buy 400 mi, not a full tank.
    plan = plan_fuel_stops([c(450, 3.0)], total_miles=900, tank_miles=TANK, start_fuel_miles=TANK)
    assert bought(plan) == [(450, 400.0)]
    assert cost_of(plan, MPG) == pytest.approx(120.0)


def test_goes_to_nearest_cheaper_station_not_cheapest_in_reach():
    # Empty tank at a $3.00 origin; $2.90 at mile 100; $2.00 at mile 400; destination 800.
    stations = [c(0, 3.0), c(100, 2.9), c(400, 2.0)]
    plan = plan_fuel_stops(stations, total_miles=800, tank_miles=TANK, start_fuel_miles=0)
    assert bought(plan) == [(0, 100.0), (100, 300.0), (400, 400.0)]


def test_fills_up_when_nothing_cheaper_is_in_reach():
    stations = [c(0, 2.0), c(300, 3.0), c(600, 3.5)]
    plan = plan_fuel_stops(stations, total_miles=900, tank_miles=TANK, start_fuel_miles=0)
    # Fill at $2.00; at $3.00 nothing cheaper is in reach, so fill up; top up just enough at $3.50.
    assert bought(plan) == [(0, 500.0), (300, 300.0), (600, 100.0)]


def test_start_with_partial_tank_cannot_buy_at_origin():
    plan = plan_fuel_stops([c(50, 3.0)], total_miles=400, tank_miles=TANK, start_fuel_miles=60)
    assert bought(plan) == [(50, 340.0)]


def test_gap_of_exactly_tank_range_is_feasible():
    plan = plan_fuel_stops([c(500, 3.0)], total_miles=1000, tank_miles=TANK, start_fuel_miles=TANK)
    assert bought(plan) == [(500, 500.0)]


def test_gap_larger_than_tank_is_infeasible():
    with pytest.raises(InfeasibleRoute) as exc:
        plan_fuel_stops([c(300, 3.0), c(900, 3.0)], total_miles=1200, tank_miles=TANK, start_fuel_miles=TANK)
    assert exc.value.stranded_at_mile == 300
    assert exc.value.gap_miles == pytest.approx(600)


def test_cheapest_station_wins_when_two_share_a_mile_marker():
    plan = plan_fuel_stops([c(450, 3.0, "a"), c(450, 2.5, "b")], 900, TANK, TANK)
    assert [p.candidate.key for p in plan] == ["b"]


def test_stations_past_destination_are_ignored():
    assert plan_fuel_stops([c(450, 1.0), c(1000, 0.5)], 450, TANK, TANK) == []


def test_naive_never_beats_optimal():
    stations = [c(0, 3.2), c(120, 2.8), c(390, 3.9), c(480, 3.1), c(700, 2.7)]
    opt = cost_of(plan_fuel_stops(stations, 1100, TANK, 0), MPG)
    naive = cost_of(naive_plan(stations, 1100, TANK, 0), MPG)
    assert opt <= naive + 1e-9


# --- Exact optimum via linear programming ------------------------------------------------------


def lp_optimal_cost(stations, total, tank, start_fuel):
    pts = sorted({s.mile: s for s in sorted(stations, key=lambda s: -s.price)}.values(), key=lambda s: s.mile)
    pts = [p for p in pts if p.mile < total - 1e-6]
    n = len(pts)
    if n == 0:
        return 0.0 if start_fuel >= total - 1e-6 else math.inf
    a_ub, b_ub = [], []
    for k, p in enumerate(pts):
        before = [1.0 if i < k else 0.0 for i in range(n)]
        upto = [1.0 if i <= k else 0.0 for i in range(n)]
        a_ub.append([-x for x in before])  # arrival fuel >= 0
        b_ub.append(start_fuel - p.mile)
        a_ub.append(upto)  # fuel after buying <= tank
        b_ub.append(tank - start_fuel + p.mile)
    a_ub.append([-1.0] * n)  # reach the destination
    b_ub.append(start_fuel - total)
    res = linprog(
        [p.price for p in pts], A_ub=np.array(a_ub), b_ub=np.array(b_ub), bounds=[(0, None)] * n, method="highs"
    )
    return res.fun if res.status == 0 else math.inf


@pytest.mark.parametrize("seed", range(500))
def test_greedy_matches_linear_program(seed):
    rng = random.Random(seed)
    total = rng.uniform(50, 2500)
    n = rng.randint(0, 25)
    stations = [
        c(round(rng.uniform(0, total), rng.choice([0, 1, 3])), round(rng.uniform(2.5, 4.5), rng.choice([1, 2, 3])), i)
        for i in range(n)
    ]
    if rng.random() < 0.3:
        stations.append(c(0.0, round(rng.uniform(2.5, 4.5), 2), "origin"))
    start = rng.choice([0.0, TANK, round(rng.uniform(0, TANK), 1)])

    expected = lp_optimal_cost(stations, total, TANK, start)
    try:
        greedy = cost_of(plan_fuel_stops(stations, total, TANK, start), 1.0)
    except InfeasibleRoute:
        greedy = math.inf
    if math.isinf(expected):
        assert math.isinf(greedy)
    else:
        assert greedy == pytest.approx(expected, rel=1e-7, abs=1e-6)


# --- With a per-stop cost ----------------------------------------------------------------------

from scipy.optimize import Bounds, LinearConstraint, milp  # noqa: E402

from fuel.services.optimizer import plan_fuel_stops_with_stop_cost  # noqa: E402


def milp_optimal_cost(stations, total, tank, start_fuel, mpg, stop_cost):
    pts = sorted({s.mile: s for s in sorted(stations, key=lambda s: -s.price)}.values(), key=lambda s: s.mile)
    pts = [p for p in pts if p.mile < total - 1e-6]
    n = len(pts)
    if n == 0:
        return 0.0 if start_fuel >= total - 1e-6 else math.inf
    # variables: b_0..b_{n-1} (miles bought), y_0..y_{n-1} (stop made)
    rows, lo, hi = [], [], []
    for k, p in enumerate(pts):
        before = [1.0 if i < k else 0.0 for i in range(n)] + [0.0] * n
        upto = [1.0 if i <= k else 0.0 for i in range(n)] + [0.0] * n
        rows.append(before), lo.append(p.mile - start_fuel), hi.append(np.inf)  # arrival fuel >= 0
        rows.append(upto), lo.append(-np.inf), hi.append(tank - start_fuel + p.mile)  # tank capacity
        link = [0.0] * (2 * n)
        link[k], link[n + k] = 1.0, -tank  # b_k <= tank * y_k
        rows.append(link), lo.append(-np.inf), hi.append(0.0)
    rows.append([1.0] * n + [0.0] * n), lo.append(total - start_fuel), hi.append(np.inf)
    cost = [p.price / mpg for p in pts] + [stop_cost] * n
    res = milp(
        cost,
        constraints=LinearConstraint(np.array(rows), lo, hi),
        integrality=[0] * n + [1] * n,
        bounds=Bounds([0] * (2 * n), [np.inf] * n + [1] * n),
    )
    return res.fun if res.status == 0 else math.inf


def test_stop_cost_zero_matches_greedy():
    stations = [c(0, 3.0), c(100, 2.9), c(400, 2.0), c(650, 2.95), c(700, 2.1)]
    greedy = cost_of(plan_fuel_stops(stations, 1100, TANK, 0), MPG)
    dp = cost_of(plan_fuel_stops_with_stop_cost(stations, 1100, TANK, 0, MPG, 0.0), MPG)
    assert dp == pytest.approx(greedy)


def test_stop_cost_merges_penny_stops():
    # Chasing the 1-cent-cheaper station at mile 10 is not worth a $20 stop.
    stations = [c(0, 3.00), c(10, 2.99), c(300, 3.10)]
    plan = plan_fuel_stops_with_stop_cost(stations, 600, TANK, 0, MPG, stop_cost=20.0)
    assert len(plan) == 2
    assert len(plan_fuel_stops(stations, 600, TANK, 0)) == 3


def test_stop_cost_infeasible_raises_with_gap():
    with pytest.raises(InfeasibleRoute):
        plan_fuel_stops_with_stop_cost([c(300, 3.0), c(900, 3.0)], 1200, TANK, TANK, MPG, 10.0)


@pytest.mark.parametrize("seed", range(300))
def test_stop_cost_dp_matches_milp(seed):
    rng = random.Random(1000 + seed)
    total = rng.uniform(50, 1800)
    n = rng.randint(0, 10)
    stations = [c(round(rng.uniform(0, total), 1), round(rng.uniform(2.5, 4.5), 3), i) for i in range(n)]
    if rng.random() < 0.4:
        stations.append(c(0.0, round(rng.uniform(2.5, 4.5), 3), "origin"))
    start = rng.choice([0.0, TANK, round(rng.uniform(0, TANK), 1)])
    stop_cost = rng.choice([0.0, 5.0, 25.0, round(rng.uniform(0, 60), 2)])

    expected = milp_optimal_cost(stations, total, TANK, start, MPG, stop_cost)
    try:
        plan = plan_fuel_stops_with_stop_cost(stations, total, TANK, start, MPG, stop_cost)
        got = cost_of(plan, MPG) + stop_cost * len(plan)
    except InfeasibleRoute:
        got = math.inf
    if math.isinf(expected):
        assert math.isinf(got)
    else:
        assert got == pytest.approx(expected, rel=1e-6, abs=1e-5)
