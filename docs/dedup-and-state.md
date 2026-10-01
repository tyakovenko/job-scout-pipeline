# Dedup and state

## State model

```
seen.json
├── urls        every URL ever fetched
├── identities  {"company|title": posted_date_when_last_fetched}
└── surfaced    identities actually shown in a delivered digest   ← absolute block

backlog.json    rows kept but not yet confirmed delivered (deferred by caps, or awaiting send)
```

| Set | Written by | Means |
|---|---|---|
| `urls`, `identities` | Every run, for every **fetched** listing | "We've looked at this" |
| `surfaced` | Only `--mark-delivered`, after the send is confirmed | "The person has seen this" |
| `backlog` | Every run | "Worth showing, not delivered yet" |

**A role is reconsidered** when its identity is unseen, **or** its board date is strictly newer than the stored one, **and** it's not in `surfaced`.

## The blindness incident

**Symptom:** The digest surfaced 1, 1, and 2 roles on three days in a row, out of about 460 watchlist rows fetched daily from 98 boards.

**Diagnosis (measured on one day's fetch):**

| Measure | Count |
|---|---|
| Watchlist rows suppressed as seen | **460 / 460** |
| New by identity | 0 |
| Suppressed rows that had never appeared in any digest | 364 |
| Posted or edited in the past 7 days | 34 |
| Brand-new URL under a title already seen (new reqs read as duplicates) | 5 |

**Cause:** `identities` was a flat list, and every *fetched* role was added to it, including roles the judge scored below threshold. A single sighting blocked a role forever. A recent expansion of location scope had used up the new pool in two runs (32 of the 41 roles ever surfaced), and nothing could refill it. The pipeline wasn't seeing a quiet market. It had made itself blind.

**Ruled out first:** The daily top-30 cap. It had been hit on only one day, and all 6 held-back roles carried forward and surfaced the next day.

### Fix: freshness in the dedup key

`identities` changed from a list to `{identity: last_posting_date}`. If a role's board date moves forward, it gets a second look from the judge. Three constraints keep this from flooding the digest:

1. **`surfaced` still blocks absolutely.** Freshness only reaches roles that were fetched, rejected, and never shown. An edit earns another judge pass, not a guaranteed place in the digest.
2. **A missing stored date means "unknown", not "infinitely old".** Otherwise the first run after the format change would have reopened all ~460 rows at once. Dates fill in on first fetch.
3. **Store the max date per identity.** 13 identities in a single fetch were two different reqs sharing one `company|title`. With last-write-wins, the newer one looked "moved" on every run, which meant a false reopen every day forever. This was caught in testing before deploy.

### Alternatives rejected

| Option | Why not |
|---|---|
| Add location to the identity key | Old keys can't be rebuilt with a location, so all ~660 existing identities would read as new and flood the next digest. It also fixes the smaller problem: most misses were re-posts and edits, not location collisions. |
| Expire seen entries after N days | Brings back roles already declined, on a timer, with no sign anything about them changed |
| Dedup on URL only | That was the original bug. ATS boards reissue URLs on edit. |
| Raise the daily cap | It was never the bottleneck. On the diagnosis day the pool held 1 role. |

**Cost:** Each edit to a Greenhouse role costs one more judge call. That's bounded by how often boards actually change: 1 reopen across 578 rows on the first day.

## Delivery is what marks a role as shown

```
scout.py            → CSV + rows queued in backlog.json    (nothing marked as shown)
digest sent + sentinel confirmed
                    → scout.py --mark-delivered <csv>      (rows move to surfaced)
```

Originally `scout.py` marked rows `surfaced` itself, when it wrote the CSV. Two consequences:

- A **failed send** used up exactly the roles it failed to deliver.
- **Any manual run** quietly used up roles nobody saw.

Now a failed send, a manual run, or a crash before delivery all leave rows queued, and the next run offers them again. The backlog drops rows older than 30 days, so a role that never gets delivered doesn't stay queued forever.

A second bug only showed up after this fix: the "nothing new fetched → exit" check ran *before* the backlog was loaded, so queued roles were stuck on any day without new listings. The backlog now loads before that check.

## Ranking

Sort key: `(-fit, -posted_date, company)`.

- **Fit always beats date.** A fit-5 role from last month outranks a fit-4 from this morning.
- **Undated rows sort last in their fit band.** A missing date isn't evidence that a role is new.
- Date never outranks fit partly because of the Greenhouse `updated_at` problem: an edited stale role would otherwise climb the digest.
- **Per-company cap (3)** and **daily cap (30)**: rows held back by either go to the backlog and compete again the next day. Neither cap throws anything away.

## Backup

`seen.json` and `backlog.json` are committed and pushed at the end of every run, but only if they changed and only for files that exist (`git add` on a missing path fails the whole command chain). Output CSVs are never pushed.
