#!/usr/bin/env python3
"""Command line entry points, mainly for cron.

    python manage.py fixtures          # re-scrape the fixture list
    python manage.py refresh           # fixtures + a snapshot for every match
    python manage.py show SEU2627H03   # print current availability
"""

import argparse
import sys

from app import app, service


def cmd_fixtures(_args):
    added, updated = service.refresh_fixtures()
    print(f"{added} added, {updated} updated")


def cmd_refresh(_args):
    failures = 0
    for code, status in service.refresh_all():
        print(f"{code}: {status}")
        if status != "ok":
            failures += 1
    return 1 if failures else 0


def cmd_show(args):
    fixture = service.find_fixture(args.code)
    if fixture is None:
        print(f"No fixture with code {args.code}", file=sys.stderr)
        return 1

    availability = service.refresh_fixture(fixture)
    totals = availability["totals"]
    print(f"{fixture.title} — {fixture.kickoff:%a %d %b %Y %H:%M}")
    print(
        f"  sold {totals['sold']:,} of {totals['capacity']:,} "
        f"({totals['percent_sold']}%)"
    )
    print(f"  available {totals['available']:,}")
    print(f"  {totals['unused_blocks']} blocks carry no sellable inventory")

    def show(node, indent):
        if not node["in_use"]:
            return
        label = "SOLD OUT" if node["sold_out"] else f"{node['open_count']:,} left"
        print(
            f"{indent}{node['name'] or node['code']:<22}"
            f"{node['open_count']:>6} / {node['total_count']:<7} {label}"
        )
        for child in node["children"]:
            show(child, indent + "  ")

    for stand in service.build_segment_tree(availability["segments"]):
        print()
        show(stand, "  ")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fixtures", help="refresh the fixture list").set_defaults(func=cmd_fixtures)
    sub.add_parser("refresh", help="snapshot every upcoming fixture").set_defaults(func=cmd_refresh)

    show = sub.add_parser("show", help="print availability for one fixture")
    show.add_argument("code")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    with app.app_context():
        return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
