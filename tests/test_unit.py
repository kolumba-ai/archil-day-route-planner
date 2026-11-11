"""Unit tests A, B, D. Fast, no disk, no network. Method names start with the test ID."""
import copy
import unittest

from planner import planner, request, routes, search

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


def all_branches(cands, c):
    return [{k: cands[k][i] for k, i in spec.items()} for spec in routes.branch_specs(cands, c["categories"])]


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

    def test_D6_minimal_travel(self):
        """Plan 1 takes (nearly) the least travel of every feasible itinerary; no alternative zigzags."""
        c = ctx("me", "ana", "biscuit", dog=True)
        cands = {cat: search.load_seed(cat) for cat in ("cafe", "museum", "food", "beach")}
        pool, every = [], []
        for fixed in all_branches(cands, c):
            pool += [("b", it) for it in routes.route_branch(fixed, cands, c)[0]]
            every += routes.route_branch(fixed, cands, c, keep=10 ** 6)[0]
        fastest = min(it["travel_min"] for it in every)
        plans = [it for _, it in routes.pick_diverse(pool, 3)]
        self.assertLessEqual(plans[0]["travel_min"], fastest + 10, "plan 1 %d min vs fastest %d" % (plans[0]["travel_min"], fastest))
        for p in plans:
            self.assertLessEqual(p["travel_min"], plans[0]["travel_min"] + routes.MAX_EXTRA_TRAVEL_MIN)

    def test_D4_scores_spread(self):
        """Known to fail today: scores bunch up at 9-10/10."""
        c = ctx("me", "ana", "biscuit", dog=True)
        cands = {cat: search.load_seed(cat) for cat in ("cafe", "museum", "food", "beach")}
        plans = []
        for fixed in all_branches(cands, c):
            plans += routes.route_branch(fixed, cands, c, keep=50)[0]
        scores = sorted(p["group_score"] for p in plans)
        self.assertGreaterEqual(scores[-1] - scores[0], 0.1, "spread %.2f" % (scores[-1] - scores[0]))
        top3 = sorted(plans, key=lambda p: -p["group_score"])[:3]
        people = [v for p in top3 for v in p["scores"].values() if isinstance(v, float)]
        self.assertFalse(all(v >= 0.9 for v in people), "every person scores 9-10/10 in the top 3 plans")


PORTLAND = ("Plan Saturday with Ana in Portland Oregon, starting at St Stephen's Catholic Church. Start at 10am "
            "and walk for no more than 2km, visiting at least one park, one coffee shop, and avoiding hills.")


class RequestTests(unittest.TestCase):
    """R: reading the request: city, start, time, walking limit, stops, hills."""

    def test_R1_portland_request(self):
        r = request.parse(PORTLAND)
        self.assertEqual(r["city"], "Portland Oregon")
        self.assertEqual(r["start_place"], "St Stephen's Catholic Church")
        self.assertEqual(r["start_min"], 10 * 60)
        self.assertEqual(r["max_walk_km"], 2.0)
        self.assertEqual(sorted(r["categories"]), ["cafe", "park"])
        self.assertTrue(r["avoid_hills"])
        self.assertEqual(r["with"], ["ana"])

    def test_R2_start_place_is_not_a_stop(self):
        r = request.parse("Plan Sunday in Chicago starting at Millennium Park at 9:30am: coffee, a museum and lunch, "
                          "walk at most 2 miles")
        self.assertEqual((r["city"], r["start_place"], r["day"]), ("Chicago", "Millennium Park", "Sunday"))
        self.assertEqual(r["start_min"], 9 * 60 + 30)
        self.assertAlmostEqual(r["max_walk_km"], 3.22, places=2)
        self.assertEqual(r["categories"], ["cafe", "museum", "food"])  # "Millennium Park" is the start, not a park stop

    def test_R3_other_cities(self):
        cases = {
            "Plan Saturday in New York City starting from Washington Square Park, coffee and a park, under 3 km":
                ("New York City", "Washington Square Park", ["cafe", "park"]),
            "Plan Saturday in Seattle starting at Pike Place Market at 11am, coffee and a museum":
                ("Seattle", "Pike Place Market", ["cafe", "museum"]),
            "Plan Saturday in Austin Texas, starting at the Texas State Capitol: lunch and a park":
                ("Austin Texas", "the Texas State Capitol", ["food", "park"]),
        }
        for text, (city, start, cats) in cases.items():
            r = request.parse(text)
            self.assertEqual((r["city"], r["start_place"], r["categories"]), (city, start, cats), text)

    def test_R4_san_francisco_demo_default(self):
        r = request.parse("Plan Saturday with Ana and Biscuit: coffee, a museum, burritos, beach at sunset")
        self.assertIsNone(r["city"])
        self.assertEqual(r["categories"], ["cafe", "museum", "food", "beach"])
        self.assertTrue(r["dog"])

    def test_R5_hill_preferences(self):
        for text in ("avoiding hills", "no hills please", "keep it flat"):
            r = request.parse("Plan Saturday in Portland, coffee and a park, " + text)
            self.assertTrue(r["avoid_hills"] and not r["prefer_hills"], text)
        for text in ("I want hills", "something hilly", "a good climb", "with some hills"):
            r = request.parse("Plan Saturday in Portland, coffee and a park, " + text)
            self.assertTrue(r["prefer_hills"] and not r["avoid_hills"], text)
        r = request.parse("Plan Saturday in Portland, coffee and a park")
        self.assertFalse(r["avoid_hills"] or r["prefer_hills"])


# A small offline city: one cafe north of the start, a flat park east, a park on a 90 m hill west.
TOWN_START = {"name": "Test Church", "lat": 45.5000, "lon": -122.6000}
TOWN = {
    "cafe": [{"name": "Corner Coffee", "lat": 45.5027, "lon": -122.6000, "open": "07:00", "close": "18:00",
              "dogs": "patio", "vegetarian_ok": True, "tags": ["cafe"]}],
    "park": [{"name": "Flat Park", "lat": 45.5000, "lon": -122.5923, "open": "05:00", "close": "22:00",
              "dogs": "leash", "tags": ["park"]},
             {"name": "Hill Park", "lat": 45.5000, "lon": -122.6077, "open": "05:00", "close": "22:00",
              "dogs": "leash", "tags": ["park"]}],
}
TOWN_ELEV = {(45.5000, -122.6000): 50, (45.5027, -122.6000): 52, (45.5000, -122.5923): 51, (45.5000, -122.6077): 140}
TOWN_WEATHER = {"sunset": "16:40", "hourly": {str(h): {"all": {"temp_c": 14, "wind_kmh": 10, "fog": False}} for h in range(7, 21)}}


def town_plans(text):
    req = request.parse(text)
    c = routes.combine(profiles("me"), req, start=TOWN_START, conditions=TOWN_WEATHER)
    c["elev"] = {routes.key({"lat": la, "lon": lo}): e for (la, lo), e in TOWN_ELEV.items()}
    pool = []
    for fixed in all_branches(TOWN, c):
        pool += [("b", it) for it in routes.route_branch(fixed, TOWN, c)[0]]
    return [it for _, it in routes.pick_diverse(pool, 3)]


class AnyCityTests(unittest.TestCase):
    """W: planning works wherever the request says, with its limits; hills both ways."""

    BASE = "Plan Saturday in Testville starting at Test Church at 9am, coffee and a park, walk at most 2 km"

    def test_W1_plans_where_and_when_asked(self):
        plans = town_plans(self.BASE)
        self.assertTrue(plans)
        sf_names = {v["name"] for cat in ("cafe", "museum", "food", "beach") for v in search.load_seed(cat)}
        for p in plans:
            self.assertEqual(p["start"], "Test Church")
            self.assertEqual(p["depart"], "09:00")
            self.assertEqual(sorted(s["category"] for s in p["stops"]), ["cafe", "park"])
            self.assertLessEqual(p["walk_km"], 2.0)
            self.assertFalse({s["name"] for s in p["stops"]} & sf_names, "San Francisco venue in another city")

    def test_W2_avoid_hills_picks_the_flat_route(self):
        plans = town_plans(self.BASE + ", avoiding hills")
        self.assertTrue(plans)
        self.assertEqual(next(s["name"] for s in plans[0]["stops"] if s["category"] == "park"), "Flat Park")
        for p in plans:  # the hill route climbs ~90 m, over the 60 m limit for "avoid hills"
            self.assertLessEqual(p["climb_m"], routes.HILLY_AVOID_REQUEST_M, p["stops"])

    def test_W3_wanting_hills_picks_the_hilly_route(self):
        plans = town_plans(self.BASE + ", I want some hills")
        self.assertTrue(plans)
        self.assertEqual(next(s["name"] for s in plans[0]["stops"] if s["category"] == "park"), "Hill Park")
        self.assertGreaterEqual(plans[0]["climb_m"], 80)

    def test_W4_walking_limit_is_a_hard_limit(self):
        self.assertEqual(town_plans(self.BASE.replace("2 km", "0.5 km")), [])


if __name__ == "__main__":
    unittest.main()
