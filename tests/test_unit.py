"""Unit tests A, B, D. Fast, no disk, no network. Method names start with the test ID."""
import copy
import os
import unittest

from planner import fitness, planner, present, request, routes, search

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

    USER = ("Plan Saturday starting at St Stephens Catholic Church in Portland, visit a coffee shop, a park and a "
            "restaurant. Start time is 10am. Finish time is no later than 3pm. Must not walk more than 3km.")

    def test_R6_start_place_stops_at_the_city(self):
        r = request.parse(self.USER)
        self.assertEqual((r["start_place"], r["city"]), ("St Stephens Catholic Church", "Portland"))
        self.assertEqual(r["categories"], ["cafe", "park", "food"])
        r = request.parse("Plan Saturday in Portland starting at St. Stephen Catholic Church at 9am, coffee")
        self.assertEqual((r["start_place"], r["start_min"]), ("St. Stephen Catholic Church", 9 * 60))

    def test_R7_addresses_and_replies(self):
        r = request.parse(self.USER + " Starting at 4055 SE Salmon St Portland Or 97214.")
        self.assertEqual(r["start_place"], "4055 SE Salmon St Portland Or 97214")  # digits: an address, not a time
        r = request.parse("Plan Saturday in Portland, coffee. Starting at the old place. Starting at Laurelhurst Park.")
        self.assertEqual(r["start_place"], "Laurelhurst Park")  # the latest answer wins
        self.assertEqual(request.parse("Plan Saturday in Portland starting at 10am, coffee")["start_place"], None)

    def test_R8_finish_time_and_limits(self):
        r = request.parse(self.USER)
        self.assertEqual((r["start_min"], r["end_min"], r["max_walk_km"]), (10 * 60, 15 * 60, 3.0))
        r = request.parse("Plan Saturday in Portland starting at Laurelhurst Park, coffee, back by 2:30 pm, finish at 3pm")
        self.assertEqual(r["start_min"], 10 * 60)  # "finish at 3pm" is not a start time
        self.assertEqual(r["end_min"], 14 * 60 + 30)

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

    def test_W5_finish_time_is_a_hard_limit(self):
        plans = town_plans(self.BASE + ", finish no later than 11am")
        self.assertTrue(plans)
        self.assertTrue(all(routes.to_min(p["end"]) <= 11 * 60 for p in plans), [p["end"] for p in plans])
        self.assertEqual(town_plans(self.BASE + ", back by 9:30am"), [])  # a cafe and a park can't fit in 30 min


def flat_plan(km=2.0, minutes=25, climb=0):
    return {"rank": 1, "depart": "10:00", "scores": {}, "stops": [{"name": "A", "category": "cafe"}],
            "legs": [{"mode": "walk", "km": km, "min": minutes, "climb_m": climb}]}


class FitnessTests(unittest.TestCase):
    """K: steps, calories, BMI and backpack, and who gets to see them."""

    def test_K1_steps_follow_distance_and_height(self):
        tall, short = {"height_cm": 190}, {"height_cm": 155}
        self.assertAlmostEqual(fitness.estimate(flat_plan(2.0), {"height_cm": 178})["steps"], 2000 / (1.78 * 0.414), delta=10)
        self.assertGreater(fitness.estimate(flat_plan(), short)["steps"], fitness.estimate(flat_plan(), tall)["steps"])
        self.assertAlmostEqual(fitness.estimate(flat_plan(4.0), tall)["steps"], 2 * fitness.estimate(flat_plan(2.0), tall)["steps"], delta=10)

    def test_K2_calories_use_weight_and_backpack(self):
        body = {"weight_kg": 78, "height_cm": 178}
        # ACSM: (0.1 x 2000 m + 3.5 x 25 min) ml/kg x 78 kg / 1000 x 5 kcal/L = 112 kcal
        self.assertEqual(fitness.estimate(flat_plan(), body)["kcal"], 112)
        loaded = fitness.estimate(flat_plan(), dict(body, backpack_kg=10))
        self.assertEqual(loaded["load_kg"], 88)
        self.assertGreater(loaded["kcal"], 112)
        self.assertNotIn("kcal", fitness.estimate(flat_plan(), {"height_cm": 178}))  # no weight, no calories

    def test_K3_climbing_burns_more(self):
        body = {"weight_kg": 78, "height_cm": 178}
        flat, hilly = fitness.estimate(flat_plan(), body), fitness.estimate(flat_plan(climb=100), body)
        self.assertEqual(hilly["kcal"] - flat["kcal"], 70)  # 1.8 x 100 m x 78 kg / 1000 x 5

    def test_K4_bmi(self):
        self.assertEqual(fitness.bmi({"weight_kg": 78, "height_cm": 178}), {"value": 24.6, "label": "healthy weight"})
        self.assertEqual(fitness.bmi({"weight_kg": 95, "height_cm": 175})["label"], "obesity range")
        self.assertIsNone(fitness.bmi({"weight_kg": 70}))

    def test_K5_each_phone_shows_only_its_own_numbers(self):
        me, ana = planner.load_profile("me")["body"], planner.load_profile("ana")["body"]
        p = flat_plan()
        mine = present.template([p], "Me", me)[1]
        hers = present.template([p], "Ana", ana)[1]
        self.assertIn("%d kcal" % fitness.estimate(p, me)["kcal"], mine)
        self.assertIn("%d kcal" % fitness.estimate(p, ana)["kcal"], hers)
        self.assertNotIn("%d kcal" % fitness.estimate(p, ana)["kcal"], mine)
        # What Ana's agent shares with the planner never includes her body data.
        self.assertNotIn("body", tests_shared_fields())


def tests_shared_fields():
    from tests.scenarios import SHARED_FIELDS
    return SHARED_FIELDS


class ClaudeParsingTests(unittest.TestCase):
    """U: Claude's reading of a request is checked field by field; the rules fill any gap."""
    TEXT = ("Plan Saturday starting at St Stephens Catholic Church in Portland, visit a coffee shop, a park and a "
            "restaurant. Start time is 10am. Finish time is no later than 3pm. Must not walk more than 3km.")

    def test_U1_checks_keep_claude_honest(self):
        from planner import understand
        good = {"city": "Portland", "start_place": "St Stephens Catholic Church", "start_time": "10:00",
                "end_time": "15:00", "max_walk_km": 3, "day": "Saturday", "stops": ["cafe", "park", "food"],
                "avoid_hills": False, "prefer_hills": False, "with_ana": False, "dog": False}
        r = understand.merge(self.TEXT, good, request.parse(self.TEXT))
        self.assertEqual((r["city"], r["start_place"], r["start_min"], r["end_min"], r["max_walk_km"]),
                         ("Portland", "St Stephens Catholic Church", 600, 900, 3.0))
        self.assertEqual(r["parsed_by"], "claude")
        # An invented place, a bad time, an unknown stop: each falls back to the rules' answer.
        bad = dict(good, start_place="Pioneer Courthouse Square", start_time="25:99", stops=["zoo"], city="Seattle")
        r = understand.merge(self.TEXT, bad, request.parse(self.TEXT))
        self.assertEqual(r["start_place"], "St Stephens Catholic Church")
        self.assertEqual(r["city"], "Portland")
        self.assertEqual(r["start_min"], 600)
        self.assertEqual(r["categories"], ["cafe", "park", "food"])
        # San Francisco is the built-in demo city.
        sf = "Plan Saturday in San Francisco starting at Noe Valley, coffee"
        self.assertIsNone(understand.merge(sf, dict(good, city="San Francisco", start_place="Noe Valley"),
                                           request.parse(sf))["city"])

    def test_U2_no_key_means_rules(self):
        from planner import understand
        old = os.environ.get("PLANNER_PARSER")
        os.environ["PLANNER_PARSER"] = "rules"
        try:
            r = understand.understand(self.TEXT)
        finally:
            if old is None:
                os.environ.pop("PLANNER_PARSER", None)
            else:
                os.environ["PLANNER_PARSER"] = old
        self.assertEqual(r["parsed_by"], "rules")
        self.assertEqual(r["start_place"], "St Stephens Catholic Church")


class VenueFactsTests(unittest.TestCase):
    """V: reading Keenable's prompted-fetch answers, and picking the venue's own page."""

    def test_V1_reads_the_answer(self):
        from planner import keenable
        a = keenable.parse_facts("same_place: yes\nsaturday_hours: 06:30-20:00\ndogs: patio\nvegetarian: yes")
        self.assertEqual(a, {"same_place": True, "hours": ("06:30", "20:00"), "dogs": "patio", "vegetarian": True})
        a = keenable.parse_facts("same_place: yes\nsaturday_hours: 6:30am-8:00pm\ndogs: unknown\nvegetarian: unknown")
        self.assertEqual((a["hours"], a["dogs"], a["vegetarian"]), (("06:30", "20:00"), None, None))
        a = keenable.parse_facts("same_place: no\nsaturday_hours: closed\ndogs: no\nvegetarian: no")
        self.assertEqual((a["same_place"], a["hours"], a["dogs"], a["vegetarian"]), (False, "closed", "no", False))

    def test_V2_own_site_only_for_closed(self):
        from planner import keenable
        self.assertTrue(keenable.own_site("https://ritualcoffee.com/locations", "Ritual Coffee Roasters (Valencia)"))
        self.assertFalse(keenable.own_site("https://la-taqueria.cafe-encore.com/", "La Taqueria"))
        page = keenable.best_page([{"url": "https://www.yelp.com/x", "title": "Best tacos"},
                                   {"url": "https://www.lataqueria.com/", "title": "Home"}], "La Taqueria")
        self.assertEqual(page["url"], "https://www.lataqueria.com/")


class DecisionTests(unittest.TestCase):
    """J: the decision step ranks and explains; it can't pick outside the safe shortlist."""

    def pool(self):
        c = ctx("me", "ana", "biscuit", dog=True)
        cands = {cat: search.load_seed(cat) for cat in ("cafe", "museum", "food", "beach")}
        out = []
        for fixed in all_branches(cands, c):
            out += [("b", it) for it in routes.route_branch(fixed, cands, c)[0]]
        return out, c

    def test_J1_rules_choose_and_explain(self):
        from planner import decide
        pool, c = self.pool()
        chosen, reasons = decide.by_rules(pool)
        self.assertEqual([it["stops"] for _, it in chosen], [it["stops"] for _, it in routes.pick_diverse(pool)])
        self.assertEqual(len(reasons), len(chosen))
        self.assertIn("%d min" % chosen[0][1]["travel_min"], reasons[0])
        self.assertIn("than plan 1", reasons[1])

    def test_J2_bad_picks_and_subq_fall_back_to_rules(self):
        from planner import decide
        pool, c = self.pool()
        shortlist = routes.pick_diverse(pool, n=decide.SHORTLIST)
        chosen, reasons = decide.validate([{"id": 99, "reason": "x"}, {"id": 1, "reason": "Good."},
                                           {"id": 1, "reason": "dup"}], shortlist, 3)
        self.assertEqual((len(chosen), reasons), (1, ["Good."]))  # unknown and repeated ids dropped
        with self.assertRaises(ValueError):
            decide.validate([{"id": -1, "reason": "x"}], shortlist, 3)
        saved = {k: os.environ.get(k) for k in ("PLANNER_DECIDER", "SUBQ_API_KEY")}
        os.environ["PLANNER_DECIDER"] = "subq"
        os.environ.pop("SUBQ_API_KEY", None)  # test the fallback without a key, even if .env has one
        try:
            chosen, reasons, by, note = decide.decide(pool, c)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(by, "rules")
        self.assertIn("SUBQ_API_KEY", note)  # no key: falls back to the rules and says why
        self.assertTrue(chosen)

    def test_J3_subq_request_and_ranking(self):
        """The request follows SubQ's Decisions API; probabilities set the order."""
        from planner import decide
        pool, c = self.pool()
        shortlist = routes.pick_diverse(pool, n=decide.SHORTLIST)
        req = decide.subq_request(shortlist, c)
        self.assertEqual(req["model"], "subq-snap-1.1")
        q = req["questions"]["best_plan"]
        self.assertEqual(q["type"], "choice")
        self.assertTrue(2 <= len(q["criteria"]) <= 255)
        self.assertEqual(sorted(q["criteria"]), sorted(req["state"]["candidates"]))
        json_size = len(__import__("json").dumps(req))
        self.assertLess(json_size, 100000)  # far under the 384K-token window
        # A response shaped like the docs' example: answers -> best_plan -> probabilities.
        fake = {"model": "subq-snap-1.1", "usage": {"input_tokens": 900, "output_tokens": 0},
                "answers": {"best_plan": {"type": "choice", "choice": "p2", "confidence": 0.7,
                                          "probabilities": {"p0": 0.1, "p1": 0.05, "p2": 0.7, "p3": 0.15}}}}
        ranked = decide.subq_rank(fake, shortlist)
        self.assertEqual([it for it, _ in ranked], [shortlist[2], shortlist[3], shortlist[0]])
        with self.assertRaises(ValueError):
            decide.subq_rank({"answers": {"best_plan": {"choice": "zz", "probabilities": {"zz": 1}}}}, shortlist)


class StartPointTests(unittest.TestCase):
    """Q: no exploration until the starting point is clear."""

    def test_Q1_missing_start_means_asking(self):
        for text in ("Plan Saturday in Portland Oregon, coffee and a park",
                     "Plan Saturday with Ana and Biscuit: coffee, a museum, burritos, beach at sunset"):
            start, _, problem = planner.resolve_start(request.parse(text))
            self.assertIsNone(start, text)
            self.assertEqual(problem[0], "missing", text)
            self.assertIn("Where should we start", problem[1])

    def test_Q1b_demo_start_needs_no_lookup(self):
        start, _, problem = planner.resolve_start(request.parse(
            "Plan Saturday with Ana and Biscuit starting from Noe Valley: coffee, a museum, burritos, beach at sunset"))
        self.assertIsNone(problem)
        self.assertIs(start, routes.START)

    def test_Q3_reply_continues_the_request(self):
        import server
        run = planner.Run()
        run.state.update(needs_start=True, original="Plan Saturday in Portland Oregon, coffee and a park.")
        action, text = server.interpret(run, "Me", "St Stephen's Catholic Church")
        self.assertEqual(action, "plan")
        r = request.parse(text)
        self.assertEqual((r["city"], r["start_place"]), ("Portland Oregon", "St Stephen's Catholic Church"))
        self.assertEqual(sorted(r["categories"]), ["cafe", "park"])
        action, text = server.interpret(run, "Me", "starting at the Pearl District.")
        self.assertEqual(request.parse(text)["start_place"], "the Pearl District")
        # A brand-new request replaces the pending one instead of being glued onto it.
        action, text = server.interpret(run, "Me", "Plan Sunday in Seattle starting at Pike Place Market, coffee")
        self.assertEqual(request.parse(text)["city"], "Seattle")


if __name__ == "__main__":
    unittest.main()
