"""Run the search step once and save the results, so scenario branches can reuse them.

    python3 -m tests.record_search <output.json>
"""
import json
import sys

from planner import planner, routes, search

if __name__ == "__main__":
    req = planner.parse_request("Plan Saturday with Ana and Biscuit: coffee, a museum, burritos, beach at sunset")
    ctx = routes.combine([planner.load_profile("me"), planner.load_profile("biscuit"),
                          planner.load_profile("ana")], req)
    out, sources = {}, {}
    for cat in req["categories"]:
        out[cat], sources[cat] = search.search_category(cat, ctx)
    with open(sys.argv[1], "w") as f:
        json.dump(out, f, indent=1)
    print("RECORDED " + json.dumps(sources))
