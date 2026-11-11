"""Unit tests A, B, D. Fast, no disk, no network. Method names start with the test ID."""
import copy
import unittest

from planner import planner, routes, search

FERRY = {"name": "Ferry Building", "lat": 37.7955, "lon": -122.3937}
CRISSY = {"name": "Crissy Field", "lat": 37.8045, "lon": -122.4486}
AQUATIC = {"name": "Aquatic Park", "lat": 37.8070, "lon": -122.4225}
OCEAN = {"name": "Ocean Beach", "lat": 37.7594, "lon": -122.5107}
INNER_SUNSET = {"name": "Inner Sunset", "lat": 37.7640, "lon": -122.4660}
RITUAL = {"name": "Ritual", "lat": 37.7565, "lon": -122.4213}
PHILZ = {"name": "Philz", "lat": 37.7527, "lon": -122.4147}


def profiles(*ids):
    return [planner.load_profile(i) for i in ids]


def ctx(*ids, dog=False):
    req = planner.parse_request("Plan Saturday" + (" with Ana" if "ana" in ids else "") + (" and the dog" if dog else ""))
    return routes.combine(profiles(*ids), req)


def venue(cat, name):
    return copy.deepcopy(next(v for v in search.load_seed(cat) if v["name"].startswith(name)))


def day(museum="Presidio Tunnel Tops", beach="Crissy Field", food="Papalote", cafe="Ritual"):
    return [("cafe", venue("cafe", cafe)), ("museum", venue("museum", museum)),
            ("food", venue("food", food)), ("beach", venue("beach", beach))]


class CombineTests(unittest.TestCase):
    def test_A1_strictest_limit_wins(self):
        lim = ctx("me", "ana", "biscuit", dog=True)["limits"]
        self.assertEqual(lim["max_km"], 6)
        self.assertEqual(lim["pace_kmh"], 4.2)
        self.assertTrue(lim["avoid_hills"])

    def test_A2_diets_merge(self):
        self.assertEqual(ctx("me", "ana")["limits"]["diets"], ["vegetarian"])
        self.assertEqual(ctx("me")["limits"]["diets"], [])

    def test_A3_dog_flag(self):
        self.assertTrue(planner.parse_request("Saturday with Biscuit")["dog"])
        self.assertTrue(planner.parse_request("Saturday with the dog")["dog"])
        lim = ctx("me", "biscuit", dog=True)["limits"]
        self.assertTrue(lim["dog"])
        self.assertEqual(lim["dog_owner"], "me")

    def test_A4_no_friend(self):
        self.assertEqual(planner.parse_request("Plan Saturday: coffee and a beach")["with"], [])


class RouteTests(unittest.TestCase):
    def test_B1_distance_sanity(self):
        # Straight line is about 4.9 km; walking streets adds ~30%.
        d = routes.leg(FERRY, CRISSY, 4.8)["km"]
        self.assertTrue(5 <= d <= 7, d)

    def test_B2_hills(self):
        # Test the hill model itself: a long leg may become a ride, which has no walking climb.
        climb, crossed = routes.climb_m(routes.START, INNER_SUNSET)
        self.assertGreater(climb, 0)
        self.assertIn("Twin Peaks", crossed)
        self.assertEqual(routes.climb_m(FERRY, AQUATIC)[0], 0)

    def test_B3_walk_or_ride(self):
        self.assertEqual(routes.leg(RITUAL, PHILZ, 4.2)["mode"], "walk")
        long_leg = routes.leg(FERRY, OCEAN, 4.2)
        self.assertEqual(long_leg["mode"], "ride")

    def test_B4_opening_hours(self):
        stops = day()
        stops[0][1]["open"] = "12:00"  # cafe opens after we would arrive
        it = routes.simulate(stops, ctx("me"))
        self.assertTrue(any("closed" in v for v in it["violations"]), it["violations"])

    def test_B5_sunset(self):
        it = routes.simulate(day(), ctx("me"))
        sunset = routes.to_min(it_conditions()["sunset"])
        arrive = routes.to_min(it["stops"][-1]["arrive"])
        self.assertTrue(sunset - 60 <= arrive <= sunset, it["stops"][-1]["arrive"])

    def test_B6_timeline_order(self):
        it = routes.simulate(day(), ctx("me", "ana"))
        for a, b in zip(it["stops"], it["stops"][1:]):
            self.assertGreaterEqual(routes.to_min(b["arrive"]), routes.to_min(a["leave"]))


def it_conditions():
    return routes.load_conditions()


class ScoreTests(unittest.TestCase):
    def test_D1_nobody_left_unhappy(self):
        self.assertGreater(routes.group_score([0.8, 0.8]), routes.group_score([1.0, 0.6]))

    def test_D2_museum_with_dog(self):
        museum_day = day(museum="de Young", beach="Crissy Field")
        with_dog = routes.simulate(museum_day, ctx("me", "ana", "biscuit", dog=True))
        no_dog = routes.simulate(museum_day, ctx("me", "ana"))
        self.assertLess(with_dog["scores"]["Me"], no_dog["scores"]["Me"])

    def test_D3_weather(self):
        c = ctx("me", "ana")
        foggy = routes.simulate(day(beach="Ocean Beach"), c)
        clear = routes.simulate(day(beach="Crissy Field"), c)
        self.assertLess(foggy["scores"]["Ana"], clear["scores"]["Ana"])

    def test_D4_scores_spread(self):
        """Known to fail today: scores bunch up at 9-10/10."""
        c = ctx("me", "ana", "biscuit", dog=True)
        cands = {cat: search.load_seed(cat) for cat in ("cafe", "museum", "food", "beach")}
        plans = []
        for m in cands["museum"]:
            for b in cands["beach"]:
                best, _, _ = routes.route_branch(m, b, cands, c, keep=50)
                plans += best
        scores = sorted(p["group_score"] for p in plans)
        self.assertGreaterEqual(scores[-1] - scores[0], 0.1, "spread %.2f" % (scores[-1] - scores[0]))
        top3 = sorted(plans, key=lambda p: -p["group_score"])[:3]
        people = [v for p in top3 for v in p["scores"].values() if isinstance(v, float)]
        self.assertFalse(all(v >= 0.9 for v in people), "every person scores 9-10/10 in the top 3 plans")


if __name__ == "__main__":
    unittest.main()
