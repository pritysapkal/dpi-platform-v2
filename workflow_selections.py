"""Which tests each automated run executes, written down once.

Both the workflow files and the test that checks "every test runs somewhere" read this table, so they cannot drift apart.
    python workflow_selections.py push-fixture     # prints the pytest -m expression for that run
"""

import sys

# name -> (pytest -m expression, database the run points DPIP_DB_PATH at)
WORKFLOWS = {
    "push-fixture": ("not frozen_data", "fixture"),            # every push: code logic on the 50 made-up PRs
    "push-frozen": ("frozen_data", "frozen"),                  # every push: README findings on the saved real pull
    "scheduled-live": ("no_db or any_data", "live"),           # daily: only tests that hold on whatever the data is today
}

if __name__ == "__main__":
    print(WORKFLOWS[sys.argv[1]][0])
