"""Fit the busy-street model to measured traffic and save it to planner/traffic_model.json.

Downloads Portland's traffic counts (PBOT open data, about 25,000 rows) and the city's streets
from OpenStreetMap, matches each counter to the street under it, and fits how a street's tags
(class, lanes, speed limit, one-way, route number) predict its vehicles a day. The planner only
reads the saved model; run this again when the counts or the map change much (yearly is plenty).

    python3 scripts/fit_traffic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from planner import env  # noqa: E402,F401  (loads .env: the overpass.ch key makes this much faster)
from planner import traffic  # noqa: E402

m = traffic.fit()
print("Fitted on %d counts, R^2 (log) = %.2f -> %s" % (m["counts"], m["r2_log"], traffic.MODEL_FILE))
for c, v in sorted(m["class"].items(), key=lambda kv: -kv[1]):
    print("  %-14s %8.0f vehicles a day at its usual lanes and speed"
          % (c, traffic.estimate_adt({"highway": c})))
print("  coefficients:", m["coef"])
