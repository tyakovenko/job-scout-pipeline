# Failure modes

Every entry here happened in production or showed up in testing before deploy. Each one has the same shape: what you'd see, what was actually wrong, how it's caught now, and how it's handled.

The common thread is that almost none of them crashed anything. They returned less data with exit code 0.

---

## Scheduling and environment

### Run fires before the network is up
- **Symptom:** 98 of 98 boards failed with `ConnectionError`. If that had been passed along as-is, the digest would have read "0 new roles".
- **Cause:** The systemd timer uses `Persistent=true`, so a run missed during suspend fires the moment the laptop wakes. That's before wifi reconnects. A small random start delay wasn't enough.
- **Fix:** A connectivity probe against a real ATS host, up to 15 tries × 20 s. The first version waited about 2 minutes and aborted **44 seconds** before wifi came up (logs: abort 19:32:38, connected 19:33:22). The window is now about 5 minutes. Waiting costs nothing when the network is already up, so it's safer to wait longer.
- **If the probe still fails:** Exit 1 with a critical desktop notification. It never moves on to fetching.

### Plain cron skips runs during suspend
- **Fix:** A systemd user timer with `Persistent=true`, so an overnight suspend means a late digest, not a missing one.

### Cloud scheduler can't reach the job boards
- **Symptom:** A trial cloud-scheduled run pushed to git fine but got nothing from any board.
- **Cause:** The cloud sandbox blocks outbound traffic to `api.ashbyhq.com` and `boards-api.greenhouse.io`.
- **Decision:** Run locally. A cloud run would have emailed "0 new roles" every day, which looks just like a quiet market. The disabled cloud job is kept as a record of why.

### Wrong exit code reported
- **Symptom:** `scout.py` exited 1 and the runner logged a clean run.
- **Cause:** The runner read `PIPESTATUS` after `{ ... } >> log`. That's a redirect, not a pipe, so `PIPESTATUS` wasn't set by it.
- **Fix:** Capture `$?` directly inside the block.

### Reading the previous run's output as today's
- **Symptom (in testing):** When dedup found nothing new, "newest CSV in `out/`" picked up yesterday's file, and yesterday's roles would have gone out again as today's.
- **Fix:** Create a marker file at the start of the run and only accept outputs `-newer` than it.

---

## Sources

### A company's board goes dead
- **Why this is the worst one:** The run looks clean, and one company you care about just stops showing up.
- **Detection:** Every board failure prints a `!` line naming the company, ATS, and slug. `--check-watchlist` checks that every slug resolves; it's run after every watchlist change.
- **Handling:** Skip the board and finish the run. The line goes into the digest and the repeat-failure tally.
- **Example:** A board where every public route returned 404 (all three ATS APIs, the board root, and the job links on the company's own careers page) was removed from the watchlist, with a note saying not to re-add it without a working board.

### Aggregator rate-limits mid-scan
- **Symptom:** Himalayas returns 429 around offset ~2,100 (about 19 hours of postings).
- **Handling:** Keep everything collected so far and log `himalayas TRUNCATED at offset N`. A cut-short scan never looks complete.

### Warnings existed but reached nobody
- **Symptom:** Source failures were printed to a log that nobody opened. Alerts only fired on the exit code, and a single dead source doesn't change the exit code.
- **Fix:** The runner collects all `!` lines from the run log and sends them out three ways:
  1. A `SOURCE WARNINGS` block in the digest (capped at 15 lines, with a count of the rest)
  2. A desktop notification that fires **even when no digest is sent**, since that's exactly when a broken source is easiest to mistake for a quiet day
  3. `source-warnings.tsv`, a tally with one row per distinct failure and a count of runs it appeared in

### One outage drowning out real repeat failures
- **Symptom:** One wifi drop added about 100 per-board rows to the tally, all for boards that did nothing wrong.
- **Fix:** Failures are keyed by who failed and how, with URLs and other per-run detail stripped. HTTP status stays in the key, so a 404 (dead board) and a 429 (rate limit) remain separate entries. Five or more network errors on one host in one run become a single `network outage: <host>` row.

---

## Judge

### Judge batch fails
- **Handling:** All rows in the batch pass through with `fit=None` and the tag `UNJUDGED - judge failed: <ExceptionType>`. The run exits 2, the email states the degradation at the top, and a critical notification fires.
- **Principle:** A judge outage should cost triage time, not lost leads.

### Judge skips some rows in a batch
- **Detection:** Each returned score is matched to its input row by index. Missing indices or non-numeric scores are counted.
- **Handling:** Those rows are tagged `UNJUDGED - judge returned no usable score`, and the count is printed.

### Prompt injection in listing text
- Job posts often include anti-spam strings like "mention the word X when applying". See [llm-judge.md](llm-judge.md#untrusted-input).

---

## Delivery

### Email reported sent but never went out
- **Cause:** The LLM email sender can exit 0 even when it decides not to send.
- **Fix:** The sender is told to print `DIGEST_SENT_OK` as its last line, and only if the email actually went out. The runner checks for that exact line.

### A failed send deleted the roles it failed to deliver
- **Cause:** `scout.py` marked rows as `surfaced` when it wrote the CSV, before delivery. A failed send, or any manual run, used up roles nobody had seen.
- **Fix:** Writing the CSV only adds rows to `backlog.json`. `--mark-delivered <csv>` runs only after the sentinel is confirmed. See [dedup-and-state.md](dedup-and-state.md#delivery-is-what-marks-a-role-as-shown).

### Queued roles stuck on quiet days
- **Cause:** The "nothing new fetched → exit" check ran before the backlog was loaded. After the delivery fix, "waiting for confirmation" is the normal state of every undelivered role, so roles from a failed send were never retried unless something new also came in.
- **Fix:** Load the backlog before deciding there's nothing to do.

---

## State

### Losing state
- `seen.json` and `backlog.json` only exist on one laptop. Every run commits and pushes them. If that push fails, a critical notification fires.

### Pipeline went blind
- The biggest incident. It gets its own write-up: [dedup-and-state.md](dedup-and-state.md#the-blindness-incident).
