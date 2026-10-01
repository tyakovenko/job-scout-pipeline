# Testing

## What the tests protect

The tests target failures **you can't see in the output**: cases where the scout keeps exiting 0 and just stops finding things. Two suites, about 1 s each, both run the real `scout.main()` with fetching replaced by a stub (no network, no judge cost):

| Suite | Pins down |
|---|---|
| **Delivery** | If a run's delivery is never confirmed, its roles come back the next run. Once `--mark-delivered` runs, they never come back. |
| **Freshness** | No flood on the first run after the format change. No reopen when nothing moved, even when identities collide. A moved role reopens, and only that role. A surfaced role stays blocked **and never reaches the judge**. `--rescue-recent` respects both its time window and `surfaced`. |

## Three ways the tests lied

All three came up while writing these suites. Each produced a test that passed against broken code.

| Trap | Why it passed | Fix |
|---|---|---|
| Counting rows in the **newest CSV on disk** | A run that writes no CSV leaves the previous one as newest, so the test reads an old file | Check the file's modification time, not its name. Filenames are timestamped to the minute, so two runs in the same minute reuse the path. |
| Asserting only that something is **absent** | A total fetch failure (exit 1, no CSV) looks identical to correct suppression. Under mutation testing, the count-only version **passed** against it. | Check the run was healthy first (exit code, fetch count), *then* check that it suppressed |
| Asserting the judge saw nothing while the test harness passes `--no-judge` | Always true, so it proves nothing | Run with the judge stubbed on (`use_judge=True`) |

## Mutation testing

Both suites were mutation-tested: deliberately break the code, then confirm a test fails. Two mutants still survive the freshness suite. That's intentional, and the reasons are written in the suite's docstring, so a later session won't spend time trying to kill them.

## Live-data audits

Unit tests can't catch source drift, because the stub returns whatever the test says. The quirks in [source-drift.md](source-drift.md) were all found by checking against live API responses: comparing filtered and unfiltered results byte for byte, counting rows per page against the requested limit, and checking that a field the code reads actually exists in the response.
