"""Unit tests A, B, D. Fast, no disk, no network. Method names start with the test ID."""
import copy
import json
import os
import unittest

os.environ.setdefault("PLANNER_PARSER", "rules")  # unit tests never call OpenAI or Claude

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
        self.assertEqual(lim["max_km"], 6.4)  # Ana's 4 miles beats my 9 km and Biscuit's 10
        self.assertEqual(lim["pace_kmh"], 4.8)  # 3 mph
        self.assertFalse(lim["avoid_hills"])    # nobody avoids hills
        slow = copy.deepcopy(profiles("ana")[0])
        slow["walking"].update(pace_kmh=4.0, hills="avoid")
        lim = routes.combine(profiles("me") + [slow], request.parse("coffee"))["limits"]
        self.assertEqual(lim["pace_kmh"], 4.0)
        self.assertTrue(lim["avoid_hills"])

    def test_A2_diets_merge(self):
        self.assertEqual(ctx("me", "ana")["limits"]["diets"], ["gluten_free"])
        self.assertEqual(ctx("me")["limits"]["diets"], [])

    def test_A2b_gluten_free_and_loves(self):
        ana = profiles("ana")[0]
        self.assertTrue(routes.loves(ana, "food", ["burger"]))        # "burgers" = OSM burger
        self.assertTrue(routes.loves(ana, "food", ["steak_house"]))   # "steaks"
        self.assertTrue(routes.loves(ana, "cafe", []))                # coffee at a cafe
        self.assertFalse(routes.loves(ana, "food", ["mexican"]))
        self.assertTrue(routes.dislikes(ana, "crowd") and routes.dislikes(ana, "indoor"))
        c = routes.combine(profiles("me") + [ana], request.parse("coffee"), start=TOWN_START, conditions=TOWN_WEATHER)
        cafe = dict(TOWN["cafe"][0], gluten_free_ok=False)
        self.assertIn("Corner Coffee: no gluten-free options", routes.simulate([("cafe", cafe)], c)["violations"])
        unknown = routes.simulate([("cafe", dict(cafe, gluten_free_ok=None))], c)
        known = routes.simulate([("cafe", dict(cafe, gluten_free_ok=True))], c)
        self.assertFalse(unknown["violations"])
        self.assertGreater(known["scores"]["Ana"], unknown["scores"]["Ana"])

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

    KOREAN = ("Propose a Saturday plan with Ana starting at St Stephens Catholic Church in Portland that includes a visit "
              "to a coffee place, a dog-friendly park, and a food truck with Korean or Japanese food. Starting at 10am "
              "and must be back at the place of departure by 3pm.")

    def test_R9_cuisine_food_truck_and_return(self):
        r = request.parse(self.KOREAN)
        self.assertEqual((r["start_place"], r["city"], r["start_min"], r["end_min"]),
                         ("St Stephens Catholic Church", "Portland", 600, 900))
        self.assertEqual(r["categories"], ["cafe", "park", "food"])
        self.assertEqual(r["cuisines"], ["korean", "japanese"])
        self.assertTrue(r["food_truck"] and r["return_to_start"])

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

    def test_W6_walk_back_to_the_start_counts(self):
        base = self.BASE.replace("2 km", "3 km")  # the walk back adds distance
        one_way = town_plans(base)[0]
        round_trip = town_plans(base + ", back at the place of departure by 1pm")[0]
        # with the 2 km limit, the same round trip is too long: the way back counts
        self.assertEqual(town_plans(self.BASE + ", back at the place of departure by 1pm"), [])
        self.assertIsNone(one_way["return_leg"])
        back = round_trip["return_leg"]
        self.assertGreater(back["km"], 0)
        self.assertAlmostEqual(round_trip["walk_km"], sum(l["km"] for l in round_trip["legs"]) + back["km"], places=1)
        self.assertGreater(routes.to_min(round_trip["end"]), routes.to_min(round_trip["stops"][-1]["leave"]))
        self.assertLessEqual(routes.to_min(round_trip["end"]), 13 * 60)

    def test_W7_cuisine_and_food_trucks(self):
        from planner import places
        pepinos = {"name": "Pepino's", "amenity": "fast_food", "cuisine": "mexican"}
        self.assertFalse(places.matches_cuisine(pepinos, ["korean", "japanese"]))
        self.assertFalse(places.is_food_truck(pepinos))
        self.assertTrue(places.matches_cuisine({"name": "Mio Sushi", "cuisine": "sushi"}, ["japanese"]))
        self.assertTrue(places.matches_cuisine({"name": "Seoul Kitchen"}, ["korean"]))  # untagged, named
        self.assertTrue(places.is_food_truck({"name": "Fomo Chicken", "street_vendor": "yes", "cuisine": "korean"}))

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
        # Pandolf, 2 km in 25 min (V = 1.333 m/s), flat, no pack:
        # M = 1.5 x 78 + 78 x 1.5 x 1.333^2 = 325 W; x 1500 s / 4184 = 117 kcal
        self.assertEqual(fitness.estimate(flat_plan(), body)["kcal"], 117)
        # With 10 kg: + 2 x 88 x (10/78)^2 = 2.9 W, and the walking term on 88 kg = 354.6 W -> 127 kcal
        loaded = fitness.estimate(flat_plan(), dict(body, backpack_kg=10))
        self.assertEqual(loaded["load_kg"], 88)
        self.assertEqual(loaded["kcal"], 127)
        # Heavier packs cost more than their weight: the (L/W)^2 term
        heavy = fitness.pandolf_watts(78, 30, 1.333, 0) - fitness.pandolf_watts(78, 0, 1.333, 0)
        self.assertGreater(heavy, 30 * 1.5 * 1.333 ** 2 + 1)
        self.assertNotIn("kcal", fitness.estimate(flat_plan(), {"height_cm": 178}))  # no weight, no calories

    def test_K3_climbing_burns_more(self):
        body = {"weight_kg": 78, "height_cm": 178}
        flat, hilly = fitness.estimate(flat_plan(), body), fitness.estimate(flat_plan(climb=100), body)
        # 100 m up over 2 km = 5% grade: + 0.35 x 1.333 x 5 x 78 = 182 W for 1500 s = 65 kcal
        self.assertEqual(hilly["kcal"] - flat["kcal"], 65)

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


class DecisionMessageTests(unittest.TestCase):
    """H7: the final decision has the same icons as the plans."""

    def test_H7_decision_has_icons(self):
        run = planner.Run()
        plans = [dict(flat_plan(), rank=i, end="12:00", stops=[
            {"name": "Ritual", "category": "cafe", "arrive": "10:20"},
            {"name": "Laurelhurst Park", "category": "park", "arrive": "11:00"}]) for i in (1, 2)]
        for p in plans:
            p["scores"] = {"Me": 0.8, "Ana": 0.8}
        run.state.update(plans=plans, votes={}, voters=["Me", "Ana"])
        planner.vote(run, "Me", 2)
        planner.vote(run, "Ana", 2)
        texts = [e["text"] for e in run.events if e["type"] == "message"]
        decided = [t for t in texts if t.startswith("✅ Decided")]
        self.assertEqual(len(decided), 2)  # one per phone
        for t in decided:
            self.assertIn("☕ Ritual", t)
            self.assertIn("🌳 Laurelhurst Park", t)
            self.assertIn("🕙 leave at", t)
        self.assertTrue(any(t.startswith("👍") for t in texts))


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
                "avoid_hills": False, "prefer_hills": False, "with_ana": False, "dog": False,
                "cuisines": [], "food_truck": False, "return_to_start": False}
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

    def test_U2_no_llm_means_stop(self):
        """No model connected: nothing is guessed, the agent says so and contacts nobody."""
        from unittest import mock
        from planner import understand
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PLANNER_PARSER")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(understand.NoLLM):
                understand.understand(self.TEXT)
            run = planner.Run()
            planner.plan(run, self.TEXT, confirmed=True)
        types = [e["type"] for e in run.events]
        self.assertIn("no_llm", types)
        self.assertNotIn("share_request", types)
        self.assertIn("No LLM is connected", [e["text"] for e in run.events if e["type"] == "message"][-1])
        # The rule-based reader exists only for tests, and only when asked for.
        with mock.patch.dict(os.environ, {"PLANNER_PARSER": "rules"}):
            self.assertEqual(understand.understand(self.TEXT)["parsed_by"], "rules")


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


class SlowVenueTests(unittest.TestCase):
    def test_V3_a_slow_venue_cannot_stall_the_search(self):
        """V3: a hung page (like Coe Circle's, 40 s) no longer holds up its whole search step."""
        import time as _t
        from planner import keenable
        real = keenable.verify

        def fake(v, category, city="San Francisco"):
            if v["name"] == "Slow Park":
                _t.sleep(5)
            return dict(v, verified=["hours (page)"])
        keenable.verify = fake
        try:
            t = _t.time()
            out = keenable.verify_all([{"name": "Quick Park"}, {"name": "Slow Park"}], "park", deadline_s=0.5)
            took = _t.time() - t
        finally:
            keenable.verify = real
        self.assertLess(took, 2)
        self.assertEqual(out[0]["verified"], ["hours (page)"])
        self.assertIn("not checked within", out[1]["verify_error"])  # keeps its map facts, with a note


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
            self.assertIn("Where", problem[1])
            self.assertNotIn("San Francisco", problem[1], "the question must not assume a city")
            self.assertNotIn("Ferry Building", problem[1])
        # With a city, the question names it; without one, it asks for the city too.
        self.assertIn("in Portland Oregon", planner.resolve_start(request.parse(
            "Plan Saturday in Portland Oregon, coffee and a park"))[2][1])
        self.assertIn("the city", planner.resolve_start(request.parse("Plan Saturday, coffee and a park"))[2][1])

    def test_Q1c_address_reply_keeps_its_city(self):
        for reply in ("4055 SE Salmon St, Portland OR 97214", "4055 SE Salmon St, Portland Or 97214",
                      "4055 SE Salmon St, Portland, OR"):
            r = request.parse("Plan Saturday with Ana, coffee and a park. Starting at %s." % reply)
            self.assertEqual((r["start_place"], r["city"]), ("4055 SE Salmon St", "Portland OR"), reply)

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


RIVERSIDE = ("Plan Saturday with Ana and Biscuit starting from St Stephen's Catholic Church in Portland Oregon: "
             "coffee, a museum, burritos, riverside at sunset")


class ConfirmFirstTests(unittest.TestCase):
    """N: the agent shows what it understood and waits for "yes" before contacting Ana."""

    def test_N1_riverside_at_sunset(self):
        r = request.parse(RIVERSIDE)
        self.assertEqual(r["categories"], ["cafe", "museum", "food", "waterfront"])
        self.assertEqual(r["sunset_stop"], "waterfront")
        self.assertEqual(request.parse("coffee, a museum, burritos, beach at sunset")["sunset_stop"], "beach")
        self.assertEqual(routes.orders(["waterfront", "cafe"], "waterfront"), [("cafe", "waterfront")])
        # Otherwise the stops are visited in the order asked: coffee, a park, then lunch, never lunch first.
        self.assertEqual(routes.orders(["cafe", "park", "food"], None), [("cafe", "park", "food")])
        self.assertEqual(request.parse("Plan Saturday in Testville: coffee, a park and a restaurant for lunch")
                         ["categories"], ["cafe", "park", "food"])

    def test_N2_nothing_sent_before_yes(self):
        import server
        from unittest import mock
        start = {"name": "St Stephen's Catholic Church", "lat": 45.5145, "lon": -122.6197}
        weather = {"sunset": "16:40", "date": "2026-11-14",
                   "hourly": {str(h): {"all": {"temp_c": 11 + (h > 12), "wind_kmh": 14, "fog": False}} for h in range(8, 20)}}
        run = planner.Run()
        with mock.patch.object(planner, "resolve_start", return_value=(start, weather, None)):
            planner.plan(run, RIVERSIDE, confirmed=False)
        types = [e["type"] for e in run.events]
        self.assertIn("confirm", types)
        self.assertNotIn("share_request", types)  # Ana hears nothing yet
        said = [e["text"] for e in run.events if e["type"] == "message"][-1]
        for bit in ("Here's what I understood", "riverside", "at sunset", "Ana", "Biscuit", "Reply yes"):
            self.assertIn(bit, said)
        self.assertEqual(server.interpret(run, "Me", "yes"), ("plan_confirmed", RIVERSIDE))
        action, text = server.interpret(run, "Me", "make it a beach instead")
        self.assertEqual(action, "plan")
        self.assertTrue(text.startswith(RIVERSIDE.rstrip(".")) and text.endswith("make it a beach instead"))
        brief = planner.weather_brief(weather, request.parse(RIVERSIDE), "Portland Oregon")
        self.assertEqual(brief, "🌤️ Weather for Saturday in Portland Oregon: 11–12°C, wind up to 14 km/h, no fog. "
                                "🌅 Sunset 16:40.")


class StreetCrimeTests(unittest.TestCase):
    """C: walks past a street-crime hotspot at that hour become a bus or an Uber/Lyft/taxi."""

    A = {"name": "A", "lat": 45.5290, "lon": -122.6765}
    B = {"name": "B", "lat": 45.5250, "lon": -122.6770}

    def index(self, hour, n):
        from planner import crime
        incidents = [(45.5270, -122.6767, hour, "Robbery", "700 BLOCK OF SW 6TH AVE")] * n
        incidents += [(45.50 + i * 0.002, -122.60, h, "Assault", "") for i in range(60) for h in (2, 15)]
        return crime.pair_cells(crime.build_index(incidents), [self.A, self.B])

    def ctx(self, tolerance, bus=True):
        opt = {"route": "FX2", "route_name": "", "board": "Union Station", "alight": "SW 6th & Pine", "stops": 3,
               "board_at": [0, 0], "alight_at": [0, 0], "ride_km": 0.5, "walk_km": 0.1, "min": 12}
        return {"crime": self.index(15, 9), "limits": {"crime_tolerance": tolerance},
                "bus": {"pairs": {routes.transit_key(self.A): {routes.transit_key(self.B): opt}} if bus else {}}}

    def test_C1_low_tolerance_takes_the_bus(self):
        lg = routes.leg(self.A, self.B, 4.5, None, self.ctx("low"), 15 * 60)
        self.assertEqual((lg["mode"], lg["bus"]["route"]), ("ride", "FX2"))
        self.assertIn("700 Block of Sw 6Th Ave", lg["avoids"]["street"])

    def test_C2_no_bus_means_a_taxi(self):
        lg = routes.leg(self.A, self.B, 4.5, None, self.ctx("low", bus=False), 15 * 60)
        self.assertTrue(lg.get("rideshare"))
        self.assertTrue(lg["avoids"])

    def test_C3_the_hour_matters_and_high_tolerance_walks(self):
        self.assertEqual(routes.leg(self.A, self.B, 4.5, None, self.ctx("low"), 9 * 60)["mode"], "walk")
        lg = routes.leg(self.A, self.B, 4.5, None, self.ctx("high"), 15 * 60)
        self.assertEqual(lg["mode"], "walk")
        self.assertTrue(lg["crime"])

    def test_C6_only_serious_violence_reroutes(self):
        from planner import crime
        drugs = [(45.5270, -122.6767, 15, "Drug/Narcotic Violations", "700 BLOCK OF SW 6TH AVE")] * 65
        c = dict(self.ctx("low"), crime=crime.pair_cells(crime.build_index(drugs), [self.A, self.B]))
        lg = routes.leg(self.A, self.B, 4.5, None, c, 15 * 60)
        self.assertEqual(lg["mode"], "walk")  # 65 drug offenses: noted, not a reason to ride
        self.assertIn("65 street crimes (65 drug offenses)", crime.describe(lg["crime"]))
        self.assertEqual(crime.crime_type("ASSAULT AGGRAVATED: HANDGUN"), "violent")
        self.assertEqual(crime.crime_type("BATTERY DOMESTIC BATTERY SIMPLE"), None)  # indoors, not street
        self.assertEqual(crime.crime_type("THEFT POCKET-PICKING"), "theft")

    def test_C7_crime_rides_lower_the_score(self):
        stops = [("cafe", {"name": "B", "lat": self.B["lat"], "lon": self.B["lon"], "open": "06:00", "close": "22:00",
                           "dogs": "patio", "tags": []})]
        base = routes.combine(profiles("me"), request.parse("coffee"), start=self.A, conditions=TOWN_WEATHER)
        base["start_min"] = 15 * 60
        safe = routes.simulate(stops, dict(base, crime=None))
        risky = routes.simulate(stops, dict(base, **{k: v for k, v in self.ctx("low").items() if k != "limits"}))
        self.assertEqual(risky["crime_rides"], 1)
        self.assertLess(risky["group_score"], safe["group_score"])
        self.assertTrue(any("instead of walking" in n and "robberies" in n for n in risky["notes"]), risky["notes"])

    def test_C4_most_cautious_person_decides(self):
        me = profiles("me")
        ana = copy.deepcopy(me[0])
        me[0]["safety"] = {"crime_tolerance": "high"}
        ana.update(id="ana", name="Ana", safety={"crime_tolerance": "low"})
        self.assertEqual(routes.combine(me + [ana], request.parse("coffee"))["limits"]["crime_tolerance"], "low")
        del me[0]["safety"]
        self.assertEqual(routes.combine(me, request.parse("coffee"))["limits"]["crime_tolerance"], "low")

    def test_C5_city_files_exist(self):
        from planner import crime
        for city in ("Portland Oregon", "San Francisco", "Seattle", "Chicago"):
            idx = crime.load(city)
            self.assertTrue(idx and idx["incidents"] > 1000, city)
        self.assertIsNone(crime.load("Boise"))


class BusTests(unittest.TestCase):
    """B: a bus ride needs one route stopping near both ends, in the right direction."""

    def test_B1_direction_matters(self):
        from planner import transit
        stops = {i: {"lat": 45.50 + i * 0.01, "lon": -122.60, "name": "Stop %d" % i} for i in range(5)}
        net = {"stops": stops, "routes": [{"ref": "14", "name": "Bus 14", "stops": [0, 1, 2, 3, 4]}]}
        a, b = {"lat": 45.5001, "lon": -122.6001}, {"lat": 45.5399, "lon": -122.6001}
        self.assertEqual(transit.best_bus(net, a, b)["route"], "14")
        self.assertIsNone(transit.best_bus(net, b, a))  # this route only runs the other way


class NamesLimitsTests(unittest.TestCase):
    """S: departures on the 5 minutes, a name per route, the limits shown and saved with them."""

    def test_S1_departures_round_to_five_minutes(self):
        self.assertEqual(routes.round_departure(600, 642), 640)    # 10:42 -> 10:40
        self.assertEqual(routes.round_departure(600, 643), 645)    # 10:43 -> 10:45
        self.assertEqual(routes.round_departure(630, 643, 645), 645)
        self.assertEqual(routes.round_departure(640, 653, 652), 650)  # not past closing
        self.assertEqual(routes.round_departure(645, 653, 653), 653)  # no 5-minute slot fits: exact
        for p in town_plans(AnyCityTests.BASE):
            for s in p["stops"]:
                self.assertEqual(routes.to_min(s["leave"]) % 5, 0, s)

    def test_S2_names_limits_and_saving(self):
        import json, tempfile
        from unittest import mock
        plans = town_plans(AnyCityTests.BASE)
        for i, p in enumerate(plans, 1):
            p["rank"] = i
        req = request.parse(AnyCityTests.BASE + ", avoiding hills, with Biscuit")
        planner.name_routes(plans, req)  # tests run without an LLM: built-in names
        names = [p["name"] for p in plans]
        self.assertEqual(len(set(names)), len(names))
        self.assertTrue(all("Park" in n for n in names), names)  # the park is what differs
        run = planner.Run()
        run.state["ctx"] = c = routes.combine(profiles("me", "biscuit"), req, start=TOWN_START, conditions=TOWN_WEATHER)
        rows = {r["label"]: r["value"] for r in planner.limits_view(c, req)}
        self.assertEqual(rows["Start"], "Test Church at 09:00")
        self.assertIn("2 km", rows["Walking"])
        self.assertIn("avoid", rows["Hills"])
        self.assertIn("Biscuit", rows["Dog"])
        self.assertIn("low tolerance", rows["Street crime"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(planner, "DATA", d):
            saved = json.load(open(planner.save_outing(run, req, plans)))
        self.assertEqual(saved["request"], req["text"])
        self.assertEqual([r["name"] for r in saved["routes"]], names)
        self.assertEqual(saved["limits"][0]["label"], "Day")


class StartAndStrollTests(unittest.TestCase):
    """P: starts given with cross streets, look-alike places rejected, strolls along a street."""

    def test_P1_place_and_cross_streets(self):
        from planner import places
        self.assertEqual(places.split_place("St Stephens Catholic Church at 41st and Salmon"),
                         ("St Stephens Catholic Church", "41st and Salmon"))
        self.assertEqual(places.split_place("Mission Dolores Park"), ("Mission Dolores Park", None))
        self.assertEqual(places.split_place("Joe's at the corner of 5th & Main"), ("Joe's", "5th & Main"))
        self.assertEqual(places._street_rx("SE 41st Ave"), "(^| )41st( |$)")

    def test_P2_look_alike_places_are_rejected(self):
        from planner import places
        self.assertFalse(places.same_place("St Stephens Catholic Church", "Saint Patrick Catholic Church"))
        self.assertTrue(places.same_place("St Stephens Catholic Church", "Saint Stephen Catholic Church"))
        self.assertTrue(places.same_place("4055 SE Salmon St", "Southeast Salmon Street"))  # addresses
        self.assertEqual(places.short_address({"display": "Saint Stephen Catholic Church, 1112, Southeast 41st "
                                                          "Avenue, Hawthorne, Portland"}), "1112 Southeast 41st Avenue")

    def test_P3_stroll_is_walked_and_ends_down_the_street(self):
        r = request.parse("Plan Saturday in Testville starting at Test Church: coffee, and a stroll down Main Street")
        self.assertIn(r["stroll_street"], ("Main", "Main Street"))
        self.assertIn("stroll", r["categories"])
        c = routes.combine(profiles("me"), r, start=TOWN_START, conditions=TOWN_WEATHER)
        stroll = {"name": "Stroll along Main Street", "lat": 45.5000, "lon": -122.6000, "end_lat": 45.5000,
                  "end_lon": -122.5846, "stroll_km": 1.2, "shops": 40, "path": [[45.5, -122.6], [45.5, -122.5846]],
                  "open": "00:00", "close": "23:59", "dogs": "leash", "tags": ["stroll"]}
        it = routes.simulate([("stroll", stroll), ("cafe", TOWN["cafe"][0])], c)
        self.assertFalse(it["violations"])
        self.assertGreaterEqual(it["walk_km"], 1.2 + it["legs"][1]["km"] - 0.01)  # the stroll is walked
        self.assertGreater(it["legs"][1]["km"], 1.0)  # the next leg starts at the far end of the street
        self.assertEqual(it["stops"][0]["stroll_km"], 1.2)
        self.assertGreater(fitness.estimate(it, {"height_cm": 178})["steps"], 1600)

    def test_P4_summary_never_drops_part_of_the_request(self):
        req = dict(request.parse("Plan Saturday with Ana: coffee and a stroll down Hawthorne Street"),
                   not_supported=["a boat tour"])
        said = planner.understood(req, {"name": "Saint Stephen Catholic Church", "address": "1112 Southeast 41st Avenue",
                                        "lat": 45.5145, "lon": -122.6197}, "Portland Oregon")
        self.assertIn("Saint Stephen Catholic Church, 1112 Southeast 41st Avenue", said)
        self.assertIn("a stroll along Hawthorne", said)
        self.assertIn('I can\'t plan "a boat tour" yet', said)

    def test_P5_named_start_is_found_in_the_city_map_data(self):
        """"ST Stephens Catholic Church" in Portland: the church in the data around the city, not a
        namesake in England (worldwide geocoder) or another Catholic church (fuzzy match)."""
        from unittest import mock
        from planner import places
        self.assertIn("Saint Stephen Catholic Church", places.spellings("ST Stephens Catholic Church"))
        center = {"lat": 45.5202, "lon": -122.6742}
        data = [
            {"type": "way", "center": {"lat": 45.53, "lon": -122.66}, "tags": {"name": "Southeast Stephens Professional Building", "addr:street": "Southeast Stephens Street"}},
            {"type": "way", "center": {"lat": 45.56, "lon": -122.64}, "tags": {"name": "Greater Saint Stephen Missionary Baptist Church", "amenity": "place_of_worship"}},
            {"type": "node", "lat": 45.52, "lon": -122.68, "tags": {"name": "Saint Stephen Preschool", "amenity": "kindergarten"}},
            {"type": "way", "center": {"lat": 45.5145, "lon": -122.6197}, "tags": {"name": "Saint Stephen Catholic Church", "amenity": "place_of_worship", "denomination": "roman_catholic", "addr:housenumber": "1112", "addr:street": "Southeast 41st Avenue"}},
            {"type": "way", "center": {"lat": 45.5347, "lon": -122.6911}, "tags": {"name": "Saint Patrick Catholic Church", "amenity": "place_of_worship", "denomination": "roman_catholic"}},
        ]
        with mock.patch.object(places, "_cached", lambda key, fn, ttl=0: data):
            hit = places.find_by_name("ST Stephens Catholic Church", center)
        self.assertEqual(hit["name"], "Saint Stephen Catholic Church")
        self.assertEqual(hit["address"], "1112 Southeast 41st Avenue")

    def test_P6_web_lookup_is_checked_against_the_map(self):
        """A place the map data has no name for: Claude's web search gives the address, the map places
        it. A different place, or an address far from the city, is not used."""
        from unittest import mock
        from planner import places
        center = {"lat": 45.5202, "lon": -122.6742}
        web = {"found": True, "name": "Common Grounds Coffeehouse", "street_address": "4321 SE Hawthorne Blvd"}
        with mock.patch.object(places, "lookup_on_web", lambda p, c: web), \
                mock.patch.object(places, "find_address", lambda a, c: {"lat": 45.5122, "lon": -122.6178}), \
                mock.patch.object(places, "find_by_name", lambda p, c, radius_m=0: None):
            hit = places.find_on_web("Common Grounds Coffeehouse", "Portland Oregon", center)
            self.assertEqual((hit["address"], hit["found_via"]), ("4321 SE Hawthorne Blvd", "web"))
            self.assertIsNone(places.find_on_web("Common Grounds Coffee Roasters Annex", "Portland Oregon", center))
        with mock.patch.object(places, "lookup_on_web", lambda p, c: web), \
                mock.patch.object(places, "find_address", lambda a, c: {"lat": 47.6, "lon": -122.3}):  # Seattle
            self.assertIsNone(places.find_on_web("Common Grounds Coffeehouse", "Portland Oregon", center))

    def test_P7_stop_near_a_street_is_searched_there(self):
        """"burritos for lunch near Hawthorne": the food stop is looked for around Hawthorne, not the start,
        and the summary says so instead of leaving it out."""
        from unittest import mock
        from planner import places, search
        text = ("Plan Saturday with Ana and Biscuit starting from ST Stephens Catholic Church in Portland, "
                "visiting a cafe, a park, and burritos for lunch near Hawthorne")
        r = request.parse(text)
        self.assertEqual(r["stop_near"], {"food": "Hawthorne"})
        said = planner.understood(r, {"name": "Saint Stephen Catholic Church", "lat": 45.5145, "lon": -122.6197},
                                  "Portland")
        self.assertIn("food near Hawthorne", said)
        self.assertNotIn("can't plan", said)
        seen = {}
        area = {"lat": 45.5121, "lon": -122.6308, "radius_m": 800, "name": "Southeast Hawthorne Boulevard"}
        with mock.patch.object(places, "find_area", lambda w, c, s: area), \
                mock.patch.object(places, "find_venues", lambda cat, center, radius, **kw: seen.update(
                    center=center, radius=radius) or []):
            search.search_city("food", {"start": {"lat": 45.5145, "lon": -122.6197}, "city": "Portland",
                                        "request": r, "limits": {"max_km": 6.4}}, "")
        self.assertEqual((seen["center"]["lat"], seen["radius"]), (45.5121, 800))

    def test_P8_venues_come_from_the_web_and_are_placed_on_the_map(self):
        """Venues from Claude's web search (current hours, closures, dog rules), placed by address on the map;
        closed that day, off the map or out of reach are left out; midnight closing stays open late."""
        from unittest import mock
        from types import SimpleNamespace as NS
        from planner import places, search
        def place(name, addr, **kw):
            v = {"name": name, "street_address": addr, "open_that_day": True, "open": "11:00", "close": "21:00",
                 "dogs": "patio", "dogs_known": True, "gluten_free": "yes", "vegetarian_ok": True,
                 "cuisine": ["mexican"], "food_truck": False, "tags": ["noisy", "hours unconfirmed"], "source": "u"}
            v.update(kw)
            return v
        found = [place("Near Taqueria", "3832 SE Hawthorne Blvd", close="00:00"),
                 place("Closed Saturdays", "3900 SE Hawthorne Blvd", open_that_day=False),
                 place("Far Cantina", "100 Far Rd"), place("Nowhere Grill", "1 Lost Ln")]
        reply = NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"places": found}))])
        client = NS(messages=NS(create=lambda **kw: reply))
        start = {"name": "Saint Stephen Catholic Church", "lat": 45.5145, "lon": -122.6197}
        spots = {"3832 SE Hawthorne Blvd": {"lat": 45.5119, "lon": -122.6237}, "100 Far Rd": {"lat": 45.70, "lon": -122.6}}
        ctx = {"start": start, "city": "Portland", "request": {"day": "Saturday", "cuisines": ["mexican"]},
               "limits": {"max_km": 6.4, "dog": True, "diets": ["gluten_free"], "avoid_foods": []}}
        with mock.patch("anthropic.Anthropic", lambda **kw: client), \
                mock.patch.object(places, "find_addresses", lambda a, c, radius_m=0: spots), \
                mock.patch.object(places, "find_by_name", lambda *a, **k: None), \
                mock.patch.object(places, "geocode_near", lambda *a, **k: None), \
                mock.patch.object(search, "web_venues_available", lambda: True):
            venues, source = search.search_city("food", ctx, "")
        self.assertEqual([v["name"] for v in venues], ["Near Taqueria"])
        v = venues[0]
        self.assertEqual((v["close"], v["gluten_free_ok"], v["address"]), ("23:59", True, "3832 SE Hawthorne Blvd"))
        self.assertIn("noisy", v["tags"])
        self.assertNotIn("hours unconfirmed", v["tags"])
        self.assertIn("web search", source)


if __name__ == "__main__":
    unittest.main()
