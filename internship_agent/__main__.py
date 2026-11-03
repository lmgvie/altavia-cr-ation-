"""CLI:  python -m internship_agent <command>

  run [--once] [--limit N]   start the loop (dry_run mode: previews N letters and exits)
  discover [--region NAME]   run one discovery round now
  seed FILE.csv              import your own list of companies (scored the same way)
  offers [--region NAME]     search online job offers now and prepare their kits
  digest                     build (and in send/draft mode, email you) today's digest now
  done REF [REF ...]         mark job offers as applied (REF = the #number in the digest)
  status                     pipeline and today's counters
  reset-previews             re-queue companies that were only previewed in dry_run
"""

import argparse
import json
import logging
from datetime import datetime

from .settings import LOGS_DIR, load_settings


def main():
    p = argparse.ArgumentParser(prog="internship_agent")
    p.add_argument("--config", help="path to criteria.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true", help="stop after today's target instead of looping")
    r.add_argument("--limit", type=int, help="override daily_target (handy with dry_run)")
    d = sub.add_parser("discover")
    d.add_argument("--region", help="region name from criteria.yaml")
    s = sub.add_parser("seed")
    s.add_argument("csv")
    o = sub.add_parser("offers")
    o.add_argument("--region", help="region name from criteria.yaml")
    sub.add_parser("digest")
    dn = sub.add_parser("done")
    dn.add_argument("refs", nargs="+")
    sub.add_parser("status")
    sub.add_parser("reset-previews")
    args = p.parse_args()

    LOGS_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(LOGS_DIR / f"internship_{datetime.now():%Y%m%d}.log", encoding="utf-8")],
    )
    for noisy in ("httpx", "httpx2", "anthropic", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_settings(args.config)

    if args.cmd in ("status", "reset-previews", "done"):
        from .settings import DB_FILE
        from .store import Store
        store = Store(DB_FILE)
        if args.cmd == "status":
            print(json.dumps({"mode": cfg["engine"]["mode"], **store.stats()}, indent=2))
        elif args.cmd == "done":
            for ref in args.refs:
                o = store.offer_by_ref(int(ref.lstrip("#")) if ref.lstrip("#").isdigit() else ref)
                if o is None:
                    print(f"{ref}: not found")
                    continue
                store.set_offer_status(o["url"], "applied")
                print(f"{ref}: {o['title']} — {o['company']} marked as applied")
        else:
            print(f"{store.reset_previews()} companies re-queued.")
        return

    from .agent import Agent
    agent = Agent(cfg)
    if args.cmd == "run":
        agent.run(once=args.once, limit=args.limit)
    elif args.cmd == "discover":
        region = None
        if args.region:
            region = next((x for x in cfg["market"]["regions"] if x["name"].lower() == args.region.lower()), None)
            if region is None:
                raise SystemExit(f"Unknown region {args.region!r}")
        agent.run_discovery(region)
    elif args.cmd == "offers":
        region = None
        if args.region:
            region = next((x for x in cfg["market"]["regions"] if x["name"].lower() == args.region.lower()), None)
            if region is None:
                raise SystemExit(f"Unknown region {args.region!r}")
        agent.run_offer_search(region)
        agent.prepare_offer_kits()
    elif args.cmd == "digest":
        agent.check_replies(force=True)
        if not agent.maybe_send_digest(force=True):
            print("Today's digest was already sent (or digest.enabled is false).")
    elif args.cmd == "seed":
        added, qualified = agent.seed_from_csv(args.csv)
        print(f"{added} companies imported, {qualified} qualified.")


if __name__ == "__main__":
    main()
