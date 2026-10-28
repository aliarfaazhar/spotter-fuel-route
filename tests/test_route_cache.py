import threading
import time

import httpx
import respx
from django.core.cache import cache

from fuel.services import planner
from fuel.services.resolver import Point
from tests.test_routing import OSRM_URL, osrm_body

START = Point(41.0, -87.6, "Chicago, IL", "place")
END = Point(41.0, -96.0, "Omaha, NE", "place")


def slow(response, seconds=0.3):
    def respond(request):
        time.sleep(seconds)
        return response() if callable(response) else response

    return respond


def in_parallel(n, fn):
    results, errors = [None] * n, []

    def run(i):
        try:
            results[i] = fn()
        except Exception as exc:  # collected so the test can report it
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results, errors


def test_concurrent_requests_for_one_new_route_make_one_routing_call():
    mock = respx.get(url__startswith=OSRM_URL).mock(side_effect=slow(httpx.Response(200, json=osrm_body())))
    results, errors = in_parallel(8, lambda: planner.get_route(START, END))

    assert not errors and mock.call_count == 1
    assert sorted(calls for _, calls, _ in results) == [0] * 7 + [1]  # the others were answered from the cache
    assert len({r.distance_miles for r, _, _ in results}) == 1


def test_a_failed_fetch_hands_over_to_a_waiting_request():
    responses = iter([httpx.Response(503), httpx.Response(200, json=osrm_body())])
    mock = respx.get(url__startswith=OSRM_URL).mock(side_effect=slow(lambda: next(responses), 0.2))
    results, errors = in_parallel(2, lambda: planner.get_route(START, END))

    assert mock.call_count == 2  # the second request fetched as soon as the first gave up, not after a timeout
    assert len(errors) == 1 and [r for r in results if r][0][1] == 1


def test_the_lock_is_released_after_a_fetch():
    respx.get(url__startswith=OSRM_URL).mock(return_value=httpx.Response(200, json=osrm_body()))
    planner.get_route(START, END)
    key = planner._route_cache_key(START, END, "osrm:driving")
    assert cache.get(key) is not None and cache.get(key + ":lock") is None
