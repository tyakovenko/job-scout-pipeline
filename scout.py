#!/usr/bin/env python3
"""Job scout: fetch -> filter -> judge (aggregators only) -> ranked CSV.

Nothing here applies to anything, and nothing here writes to the Google Sheet --
the Drive tooling cannot write cells into an existing Sheet (see CLAUDE.md
"Known constraint"). This produces a CSV in scripts/out/ for review and import.

Usage:
    python3 scout.py                    # full run
    python3 scout.py --no-judge         # skip the LLM judge entirely
    python3 scout.py --check-watchlist  # verify every ATS slug resolves
    python3 scout.py --min-fit 4        # raise the keep threshold

Exit codes:
    0 = clean run
    1 = hard failure (nothing fetched)
    2 = DEGRADED run -- results exist but the judge failed for some batches.
        Cron/digest MUST treat this as a problem, not a success.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sources

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
SEEN_CACHE = HERE / "seen.json"
BACKLOG = HERE / "backlog.json"
BACKLOG_MAX_AGE_DAYS = 30
DEFAULT_MIN_FIT = 3


def _identity(L: dict) -> str:
    """Stable key for a role, independent of its URL.

    ATS boards reissue a new URL when a role is edited or reposted, so URL-only
    dedup lets the same job reappear in tomorrow's digest as if it were new.
    """
    return f"{(L.get('company') or '').strip().lower()}|{(L.get('title') or '').strip().lower()}"


def _posted(L: dict) -> datetime | None:
    """Posting timestamp, or None when the board gave nothing parseable.

    NOT uniformly "when this role was first posted": Greenhouse exposes
    `updated_at` (last EDIT) while Ashby gives a true `publishedAt`. So a
    Greenhouse role edited today looks brand new. That asymmetry is the whole
    reason freshness only ever RE-OPENS a role for judging (below) and never
    puts it straight into the digest -- an edit earns a second look, not a slot.
    """
    raw = (L.get("posted_date") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_seen() -> tuple[set[str], dict[str, str]]:
    """Returns (urls, {identity: posted_date-when-last-fetched}).

    `identities` was a flat list until 2026-09-21. A role's identity alone said
    "fetched before, never show again", which made every rejection permanent:
    on 2026-09-21, 460 of 460 watchlist rows were suppressed this way, 34 of
    them re-posted or edited within the previous week. Storing the posting date
    alongside lets a role that has genuinely moved since we last saw it come
    back for judging. A bare list still loads -- dates backfill on first run.
    """
    if not SEEN_CACHE.exists():
        return set(), {}
    d = json.loads(SEEN_CACHE.read_text())
    ids = d.get("identities", [])
    if isinstance(ids, list):
        ids = {i: "" for i in ids}
    return set(d.get("urls", [])), ids


def save_seen(urls: set[str], identities: dict[str, str], surfaced: set[str]) -> None:
    """`surfaced` = roles actually shown in a digest, as opposed to merely fetched.

    Kept separate because a fetched-but-not-yet-surfaced role must still be
    eligible to appear later from the backlog.
    """
    SEEN_CACHE.write_text(json.dumps(
        {"urls": sorted(urls),
         "identities": dict(sorted(identities.items())),
         "surfaced": sorted(surfaced),
         "updated": datetime.now(timezone.utc).isoformat()}, indent=1))



def _record_dates(ids: dict[str, str], listings: list[dict]) -> None:
    """Store each identity's posting date, keeping the NEWEST when rows collide.

    13 identities in a single 2026-09-21 fetch were two distinct reqs sharing one
    company|title (Baseten "AI Engineer", Sierra "Strategist, Agent Development",
    ...). Last-write-wins let the sibling with the newer date read as "moved since
    last run" on every subsequent run -- a permanent daily false reopen.
    """
    for L in listings:
        ident = _identity(L)
        posted = _posted(L)
        if not posted:
            ids.setdefault(ident, "")
            continue
        prev = ids.get(ident) or ""
        if prev:
            try:
                if datetime.fromisoformat(prev) >= posted:
                    continue
            except ValueError:
                pass
        ids[ident] = posted.isoformat()


def _backfill_dates(urls: set[str], ids: dict[str, str], surfaced: set[str],
                    listings: list[dict], args) -> None:
    """Record today's posting dates even on a run that surfaces nothing.

    Without this the early "no new listings" return skips save_seen entirely, so
    pre-2026-09-21 entries keep their empty date forever and the reopen check
    above can never fire for them.
    """
    if args.no_dedup:
        return
    _record_dates(ids, listings)
    save_seen(urls | {L["url"] for L in listings if L.get("url")}, ids, surfaced)


def load_backlog() -> list[dict]:
    """Roles that passed the bar but were not surfaced yet.

    Taya applies to a few roles a day, so the digest is capped with --top. Without
    a backlog those unsurfaced roles would be lost for good: seen.json records
    every FETCHED listing, so they would never be re-fetched as new. Carrying them
    forward lets a good role wait its turn instead of vanishing.
    """
    if not BACKLOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKLOG_MAX_AGE_DAYS)
    rows, stale = [], 0
    for r in json.loads(BACKLOG.read_text()).get("rows", []):
        try:
            first = datetime.fromisoformat(r.get("_first_seen", ""))
        except ValueError:
            first = datetime.now(timezone.utc)
        # A months-old posting is usually filled or closed -- carrying it forever
        # would slowly fill the digest with dead links.
        if first < cutoff:
            stale += 1
            continue
        rows.append(r)
    if stale:
        print(f"  ({stale} backlog rows dropped as stale, >{BACKLOG_MAX_AGE_DAYS}d old)")
    return rows


def save_backlog(rows: list[dict]) -> None:
    BACKLOG.write_text(json.dumps(
        {"rows": rows, "updated": datetime.now(timezone.utc).isoformat()}, indent=1))


def mark_delivered(csv_path: Path) -> int:
    """Record that the rows in `csv_path` actually reached Taya.

    Split out from the run because writing a CSV is not delivering it. Called by
    run-daily.sh only after the digest prints its send sentinel, so a failed send
    leaves the rows queued in backlog.json for tomorrow instead of burning them.
    """
    if not csv_path.exists():
        print(f"!! {csv_path} does not exist", file=sys.stderr)
        return 1
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    ids = {_identity({"company": r.get("Company", ""), "title": r.get("Job Title", "")})
           for r in rows}
    ids.discard("|")

    d = json.loads(SEEN_CACHE.read_text()) if SEEN_CACHE.exists() else {}
    seen_ids = d.get("identities", [])
    if isinstance(seen_ids, list):
        seen_ids = {i: "" for i in seen_ids}
    before = set(d.get("surfaced", []))
    save_seen(set(d.get("urls", [])), seen_ids, before | ids)

    # Drop the delivered rows from the queue -- they have been shown now.
    if BACKLOG.exists():
        carried = json.loads(BACKLOG.read_text()).get("rows", [])
        remaining = [r for r in carried if _identity(r) not in ids]
        save_backlog(remaining)
        dropped = len(carried) - len(remaining)
    else:
        dropped = 0
    print(f"marked {len(ids)} delivered ({len(ids - before)} new), "
          f"{dropped} cleared from the backlog")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM judge")
    ap.add_argument("--judge-all", action="store_true",
                    help="also judge watchlist results, so every row gets a comparable fit score")
    ap.add_argument("--check-watchlist", action="store_true", help="verify ATS slugs and exit")
    ap.add_argument("--mark-delivered", metavar="CSV",
                    help="record that this CSV's rows reached Taya, and clear them "
                         "from the backlog. Run ONLY after the digest actually sent.")
    ap.add_argument("--top", type=int, default=0,
                    help="surface only the N best roles this run; the rest carry "
                         "forward in backlog.json and compete again tomorrow (0 = all)")
    ap.add_argument("--per-company", type=int, default=3,
                    help="max roles per company in the output (default 3; 0 = no cap)")
    ap.add_argument("--min-fit", type=int, default=DEFAULT_MIN_FIT,
                    help=f"minimum judged fit to keep (default {DEFAULT_MIN_FIT})")
    ap.add_argument("--no-dedup", action="store_true", help="ignore seen.json")
    ap.add_argument("--rescue-recent", type=int, default=0, metavar="DAYS",
                    help="one-off backfill: also reconsider roles posted/edited in "
                         "the last DAYS that were fetched before but never surfaced. "
                         "Costs judge calls on roles already rejected once, so it is "
                         "opt-in rather than part of the daily run (0 = off)")
    ap.add_argument("--watchlist", help="path to an alternative watchlist file (trial runs)")
    ap.add_argument("--only-watchlist", action="store_true",
                    help="skip the aggregators, poll the watchlist only")
    args = ap.parse_args()

    if args.watchlist:
        sources.set_watchlist(args.watchlist)
        print(f"using watchlist: {args.watchlist}")

    if args.mark_delivered:
        return mark_delivered(Path(args.mark_delivered))

    if args.check_watchlist:
        print("Checking watchlist slugs...\n")
        bad = 0
        for name, status, detail in sources.check_watchlist():
            flag = "OK  " if status == "OK" else "FAIL"
            if status != "OK":
                bad += 1
            print(f"  [{flag}] {name:20s} {detail}")
        print(f"\n{bad} broken." if bad else "\nAll slugs resolve.")
        return 1 if bad else 0

    if args.only_watchlist:
        print("Fetching watchlist (ATS) only...")
        listings = sources.fetch_watchlist()
    else:
        listings = sources.fetch_all()
    if not listings:
        print("\n!! NOTHING FETCHED - every source failed or filtered to zero.", file=sys.stderr)
        return 1

    seen_urls, seen_ids = (set(), {}) if args.no_dedup else load_seen()

    surfaced_ids = set()
    if SEEN_CACHE.exists() and not args.no_dedup:
        surfaced_ids = set(json.loads(SEEN_CACHE.read_text()).get("surfaced", []))

    # A listing earns a look if it is genuinely unseen, OR if it has moved on the
    # board since we last fetched it. Both gates still sit in front of the
    # `surfaced` filter further down, so nothing Taya has already been shown can
    # come back through here -- this only reaches the pool of roles that were
    # fetched, silently rejected, and never made a digest.
    fresh, reopened, rescued = [], 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.rescue_recent)
              if args.rescue_recent else None)
    for L in listings:
        if not L["url"]:
            continue
        ident = _identity(L)
        if L["url"] not in seen_urls and ident not in seen_ids:
            fresh.append(L)
            continue
        if ident in surfaced_ids:
            continue  # already shown once; an edit does not re-earn a slot
        posted, prev = _posted(L), seen_ids.get(ident) or ""
        # prev == "" is a pre-2026-09-21 entry with no recorded date. Treat it as
        # unknown, NOT as "older than everything" -- otherwise the first run after
        # the format change reopens all ~460 rows at once. Dates backfill below.
        if posted and prev:
            try:
                if posted > datetime.fromisoformat(prev):
                    fresh.append(L)
                    reopened += 1
                    continue
            except ValueError:
                pass
        if cutoff and posted and posted >= cutoff:
            fresh.append(L)
            rescued += 1

    extra = []
    if reopened:
        extra.append(f"{reopened} reopened (moved on the board)")
    if rescued:
        extra.append(f"{rescued} rescued by --rescue-recent {args.rescue_recent}d")
    print(f"\n{len(listings)} fetched, {len(fresh)} new after dedup"
          + (f" [{', '.join(extra)}]" if extra else "") + ".")
    # Load the queue BEFORE deciding there is nothing to do. A quiet fetch day
    # does not mean an empty digest: the backlog holds roles deferred by --top
    # and roles still awaiting delivery confirmation. Returning here without
    # checking it stranded them until the next day something fresh arrived --
    # and after the delivery fix, "awaiting confirmation" is the normal state of
    # every undelivered role, so this was the difference between a failed send
    # retrying tomorrow and never retrying at all.
    carried = [] if args.no_dedup else load_backlog()

    if not fresh and not carried:
        print("No new listings since last run.")
        _backfill_dates(seen_urls, seen_ids, surfaced_ids, listings, args)
        return 0
    if not fresh:
        print(f"No new listings; re-offering {len(carried)} from the backlog.")

    # Watchlist results are already a curated company list -- judging them only
    # re-confirms a decision made when the company was added. Judge the noisy
    # aggregator output only. (CLAUDE.md "Ranking".)
    # --judge-all forces watchlist rows through the judge too. Needed whenever the
    # output must be sorted by relevance across ALL sources -- an unjudged row has
    # no score, so it cannot be ranked against a judged one.
    if args.judge_all:
        targeted, aggregated = [], fresh
    else:
        targeted = [L for L in fresh if L.get("targeted")]
        aggregated = [L for L in fresh if not L.get("targeted")]
        for L in targeted:
            L["fit"] = ""
            L["note"] = "watchlist - not judged"

    health = {"failed_batches": 0, "unjudged": 0, "errors": []}
    if aggregated and not args.no_judge:
        print(f"\nJudging {len(aggregated)} aggregator listings "
              f"({len(targeted)} watchlist results skip the judge)...")
        import judge
        aggregated, health = judge.score_listings(aggregated)
    elif args.no_judge:
        for L in aggregated:
            L["fit"] = ""
            L["note"] = "judge skipped (--no-judge)"

    # fit=None means UNJUDGED -- always keep those, never let a judge failure
    # quietly filter a listing out of the results.
    kept = targeted + [
        L for L in aggregated
        if L.get("fit") is None or L.get("fit") == "" or L["fit"] >= args.min_fit
    ]

    # Merge in roles carried over from previous runs so they compete on fit with
    # today's arrivals rather than being stuck behind them forever.
    now_iso = datetime.now(timezone.utc).isoformat()
    for L in kept:
        L.setdefault("_first_seen", now_iso)
    if carried:
        print(f"  ({len(carried)} roles carried forward from the backlog)")
    pool = kept + carried

    # Drop anything already surfaced in an earlier digest. (surfaced_ids is read
    # once, up at the dedup step, which also consults it.)
    pool = [L for L in pool if _identity(L) not in surfaced_ids]

    # De-duplicate the pool itself (a carried role can also be re-fetched today).
    by_id = {}
    for L in pool:
        by_id.setdefault(_identity(L), L)
    pool = list(by_id.values())

    # Fit first, then recency. Recency is only a TIEBREAK: a fit-5 role from last
    # month still outranks a fit-4 posted this morning. Within a fit band the
    # newer posting wins, because an older one is likelier to be filled or deep in
    # a pile of applicants. Undated rows sort last within their band rather than
    # first -- a missing date is not evidence of freshness.
    _OLDEST = datetime.min.replace(tzinfo=timezone.utc)
    pool.sort(key=lambda L: (-(L.get("fit") or 0),
                             -(_posted(L) or _OLDEST).timestamp(),
                             L.get("company", "").lower()))

    # Cap per company. Big employers otherwise swamp the digest -- Anthropic alone
    # returned 30 rows in the first run and Sierra 11 near-identical ones. Applied
    # AFTER judging so the three that survive are the three best-scoring, not the
    # three that happened to come back first from the API.
    if args.per_company > 0:
        per, capped, dropped = {}, [], 0
        for L in pool:
            c = (L.get("company") or "").strip().lower()
            if per.get(c, 0) >= args.per_company:
                dropped += 1
                continue
            per[c] = per.get(c, 0) + 1
            capped.append(L)
        if dropped:
            print(f"  ({dropped} rows held back by the {args.per_company}-per-company cap)")
        # Rows the cap held back are NOT discarded -- they return to the backlog
        # below and can surface once that company's earlier roles are dealt with.
        held = [L for L in pool if L not in capped]
        pool, overflow = capped, held
    else:
        overflow = []

    if args.top > 0 and len(pool) > args.top:
        kept, deferred = pool[:args.top], pool[args.top:]
        print(f"  (surfacing top {args.top}; {len(deferred)} deferred to the backlog)")
    else:
        kept, deferred = pool, []

    if not args.no_dedup:
        # `kept` is carried too, not just the leftovers. Writing the CSV is not
        # delivery -- the digest still has to send. Until --mark-delivered says it
        # did, these rows stay queued so a failed send (or a standalone scout.py
        # run) re-offers them tomorrow instead of losing them.
        for L in kept:
            L["_pending_delivery"] = True
        save_backlog(deferred + overflow + kept)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_csv = OUT_DIR / f"scout-{stamp}.csv"
    # Column order matches the Google Sheet so a paste lands correctly:
    # Job Title | Company | Job Description Link | Expected Salary | Notes | Status
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        # No Status column: Taya sets status herself in her tracker, so emitting
        # one risks overwriting her values (and her data validation) on paste.
        # Expected Salary dropped too -- not in her restructured sheet.
        w.writerow(["Job Title", "Company", "Job Description Link",
                    "Fit", "Posted", "Date basis", "Track", "Location", "Comp",
                    "Why eligible", "Source", "Notes"])
        for L in kept:
            note = L.get("note", "")
            if L.get("fit") is None:
                note = f"[UNJUDGED] {note}"
            posted = _posted(L)
            w.writerow([L.get("title", ""), L.get("company", ""), L.get("url", ""),
                        L.get("fit", ""), posted.date().isoformat() if posted else "",
                        L.get("date_basis", "") if posted else "",
                        L.get("track", ""), L.get("location", ""),
                        L.get("comp", ""), L.get("geo_reason", ""), L.get("source", ""), note])

    if not args.no_dedup:
        # Record every FETCHED listing, not just the kept ones -- otherwise a role
        # judged below threshold today comes back as "new" tomorrow, forever.
        d = json.loads(SEEN_CACHE.read_text()) if SEEN_CACHE.exists() else {}
        # `surfaced` is NOT touched here. It means "Taya has seen this", and
        # writing a CSV is not evidence of that -- run-daily.sh still has to send
        # the digest, and it can fail. Marking here destroyed the roles a failed
        # send never delivered, and silently consumed roles on any manual run.
        # --mark-delivered writes it, after the send sentinel. (2026-09-21)
        # Store each identity's CURRENT posting date, so "has it moved since we
        # last looked?" is answerable on the next run.
        _record_dates(seen_ids, listings)
        save_seen(seen_urls | {L["url"] for L in listings if L.get("url")},
                  seen_ids, set(d.get("surfaced", [])))

    # Count from the FINAL kept list -- counting `targeted` here predates the
    # per-company cap and produced a negative aggregator count.
    n_watchlist = sum(1 for L in kept if str(L.get("source", "")).startswith("watchlist"))
    print(f"\n{len(kept)} kept -> {out_csv}")
    print(f"  watchlist: {n_watchlist}  |  aggregator: {len(kept) - n_watchlist}")
    import collections as _c
    print("  by track:", dict(_c.Counter(L.get("track", "?") for L in kept)))

    if health["failed_batches"]:
        print("\n" + "=" * 62, file=sys.stderr)
        print("RUN DEGRADED - THE JUDGE FAILED. Results are incomplete.", file=sys.stderr)
        print(f"  failed batches : {health['failed_batches']}", file=sys.stderr)
        print(f"  unjudged rows  : {health['unjudged']} (in CSV, tagged [UNJUDGED])", file=sys.stderr)
        for e in health["errors"]:
            print(f"  - {e}", file=sys.stderr)
        print("=" * 62, file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
