import math

import pytest

from fuel.services import candidates
from fuel.services.optimizer import Candidate, plan_fuel_stops

MPG, TANK = 10.0, 500.0


def unlisted(mile, price):
    return Candidate(key=("unlisted", float(mile)), mile=float(mile), price=price)


def listed(i, mile, price):
    return Candidate(key=i, mile=float(mile), price=price)


def test_no_extra_stop_just_to_avoid_a_little_off_list_fuel():
    # Off-list fuel at the start (typical price $3.50 + the margin), a $4.58 station at mile 300 and a $3.00 one
    # 10 miles on. Topping up at the first only to save 10 miles of off-list fuel isn't worth a $10 stop.
    price = 3.50 + candidates.UNLISTED_PREMIUM_PER_GALLON
    cands = [unlisted(0, price), listed(1, 300, 4.58), listed(2, 310, 3.00)]
    plan = plan_fuel_stops(cands, 700, TANK, 0.0, MPG, stop_cost=10.0)
    assert [p.candidate.key for p in plan] == [("unlisted", 0.0), 2]


def test_a_listed_station_still_wins_when_it_saves_real_money():
    price = 3.50 + candidates.UNLISTED_PREMIUM_PER_GALLON
    cands = [unlisted(0, price), listed(1, 100, 3.00)]
    plan = plan_fuel_stops(cands, 450, TANK, 0.0, MPG, stop_cost=10.0)
    assert plan[0].miles == 100  # only enough off the list to reach the listed station
    assert plan[1].candidate.key == 1


@pytest.mark.parametrize("price", [4.4, 3.987, 5.123])
@pytest.mark.parametrize(("total", "start"), [(1873.4, 0.0), (2268.0, 250.0), (1500.3, 500.0)])
def test_off_list_fuel_is_not_split_into_many_stops_without_a_stop_cost(price, total, start):
    # Points every 10 miles at one price: with stop_cost=0 every split costs the same, and without the tie-break
    # the plan came out as 40-118 small stops (rounding decides between equal costs).
    cands = [unlisted(m, price) for m in range(0, int(total), 10)]
    plan = plan_fuel_stops(cands, total, TANK, start, MPG, candidates.stop_cost_for(cands, 0.0))
    assert len(plan) <= math.ceil((total - start) / TANK) + 1


def test_tie_break_only_applies_with_off_list_fuel():
    assert candidates.stop_cost_for([listed(1, 0, 3.0)], 0.0) == 0.0
    assert candidates.stop_cost_for([unlisted(0, 4.0)], 10.0) == 10.0
