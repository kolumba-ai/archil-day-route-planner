"""Download the past 12 months of street crime for each city into planner/crime_data/.

The planner only reads these files. Keeping them fresh is a separate service's job: run this
script on a schedule (e.g. monthly; Portland publishes monthly, the Socrata cities daily).

    python3 scripts/update_crime.py                 # every city
    python3 scripts/update_crime.py portland        # one city
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from planner import crime  # noqa: E402

for src in (sys.argv[1:] or list(crime.SOURCES)):
    try:
        idx = crime.update(src)
        print("%-14s %6d street crimes in %5d cells (%s) -> %s"
              % (src, idx["incidents"], len(idx["cells"]), idx["covers"], crime.path_for(src)))
    except Exception as e:
        print("%-14s FAILED: %r" % (src, e))
