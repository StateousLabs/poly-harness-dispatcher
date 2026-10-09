"""Fake codex/opencode CLI whose model catalogue command is slow and counted; any other call is fake_harness.

FAKE_CATALOG_DELAY  seconds to sleep before printing the catalogue (default 0)
FAKE_CATALOG_COUNT  file to append one line to per catalogue invocation
FAKE_CATALOG_FAIL   set to 1 to exit 3 from the catalogue command
FAKE_CATALOG_MODELS comma-separated model names to list (default gpt-5.6-terra)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    args = sys.argv[1:]
    if args[:2] == ["debug", "models"] or args[:1] == ["models"]:
        if path := os.environ.get("FAKE_CATALOG_COUNT"):
            with open(path, "a") as f:
                f.write("x\n")
        if os.environ.get("FAKE_CATALOG_FAIL") == "1":
            return 3
        time.sleep(float(os.environ.get("FAKE_CATALOG_DELAY", "0")))
        names = os.environ.get("FAKE_CATALOG_MODELS", "gpt-5.6-terra").split(",")
        if args[0] == "debug":
            print(json.dumps({"models": [{"slug": n} for n in names]}))
        else:
            print("\n".join(names))
        return 0
    import fake_harness
    return fake_harness.main()


if __name__ == "__main__":
    sys.exit(main())
