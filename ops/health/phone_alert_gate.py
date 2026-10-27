#!/usr/bin/env python3
"""Phone routing check only: exit10 means exact approved nonpaging class.

Everything else, including import/parse failure, must retain the caller's send.
No network, DB, service reads, or logging/report changes in this adapter.
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    from project_mai_tai.phone_alert_policy import phone_decision
    phone, _ = phone_decision(args.title, sys.stdin.read())
    return 0 if phone else 10


if __name__ == "__main__":
    raise SystemExit(main())
