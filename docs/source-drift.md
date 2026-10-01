# Source drift

Job boards are external systems with no promise that they'll stay the same. Their docs are often wrong, field meanings change from one source to the next, and boards disappear. This page covers what was found, how the pipeline adapts to it, and the evidence behind each source decision.

## Principles

1. **Check against live data, not the docs.** Every quirk below was found by checking real responses. None of them were in the documentation.
2. **Derive facts from the posting, not from how it's filed.** A company's watchlist entry says which list it's on. It says nothing about where a given role is. Remote status and country always come from the posting itself.
3. **Record what a date means.** Sources report different kinds of dates, so each row carries a `date_basis` column.
4. **Rejecting a source requires evidence, and the evidence gets written down.** Every rejected source has a recorded reason, so nobody spends time probing it again.

## Quirks found in live data

| Source | Documented / assumed | Actual behavior | Adaptation |
|---|---|---|---|
| **Greenhouse** | `updated_at` looks like a posting date | It's the **last edit**. A stale role edited today looks new. The public API has no publish date. | `date_basis = updated`. The digest writes "updated Sep 18", never "posted". Date never ranks above fit. |
| **Greenhouse** | "Location Type" metadata marks remote roles | Often unset, while the location string says "Santa Clara, CA or Remote" | Treat the word `remote` in the location string as a remote signal too |
| **Ashby** | `location` is the location | Remote is often a **secondary** location on an office-based role | Merge primary and secondary locations before classifying |
| **Himalayas** | `limit=100` | **Silently capped at 20**. Paging by 100 skipped 80 jobs per page. | Advance the offset by however many rows actually came back |
| **Himalayas** | `category`, `search`, `seniority` filters | **None of them work.** The responses are byte-identical and unfiltered. | All narrowing happens client-side. Scan newest-first and stop when postings fall outside a 30 h window. |
| **Himalayas** | `pubDate` string | It's a Unix int | Parse it as an int |
| **speedrun** | A portfolio flag named `portfolio` | That field **never existed**. It's `tier` (`a16z` / `market`). Reading the wrong key labeled every row "market" and lost the signal without any error. | Read `tier`. Rows get labeled `speedrun:a16z`. |
| **speedrun** | One remote field | Two remote fields that disagree | Treat a role as remote if either field says so |
| **speedrun** | OR queries | Not supported | One request per term, deduped on job id |
| **HN Who's Hiring** | Structured posts | Free text. The first line is usually `Company \| Role \| Location`. | Parse the first line best-effort. The judge cleans up company and title. |
| **WeWorkRemotely** | JSON API | RSS only. `"Company: Role"` is packed into the title. | Split on the first `: ` |
| **All ATS** | One role, one URL | One role is listed once **per location**, each with its own URL | Within a run, collapse duplicates on `(company, title)` and merge the locations |
| **All ATS** | URLs are stable | URLs can be reissued when a posting is edited | Dedup on identity, not URL ([details](dedup-and-state.md)) |

## Location classification

Location is the most error-prone field. The order of the checks matters, and each rule was added after a specific mistake:

- **Explicit location beats marketing copy.** "Worldwide" was checked first at one point, and roles in Bangalore, Toronto, Nairobi, and Edinburgh came through labeled worldwide because the description said "global".
- **A remote role limited to an out-of-scope country is out of scope.** It's not remote-eligible just because it says remote.
- **The employer's HQ is only a fallback hint.** It's used only for remote postings that name no country, and never overrides an explicit location.
- **Remote with no country gets kept and flagged `(VERIFY)`.** It isn't dropped quietly.
- **Every drop records a reason** (`on-site, location unclear (...)`), so the run log explains why the result set got smaller.

## Coverage strategy: grow the company list, not the number of boards

The first run made it clear: **every top-scoring (fit 5) match came from the curated company watchlist**, and none came from generic boards. Coverage grows by finding *companies*, through a separate weekly discovery step that collects ATS board URLs from curated startup job sites (so the slugs are already confirmed working, not guessed). Adding more generic job boards doesn't help.

## Sources evaluated and rejected

| Source | Verdict | Evidence |
|---|---|---|
| datasciencejobs.com | Blocked | Cloudflare challenge on every path, including `/feed`, `/rss`, `/api/jobs`. Getting past it would mean defeating an anti-bot measure on purpose. |
| datascience-jobs.com | Dead | No DNS |
| ai-jobs.net | Dead feed | 301 redirect to an HTML-only page, no API |
| Working Nomads | Low yield | Returns exactly 50 jobs no matter the filter. 4 ML title matches, 3 of them data-annotation gigs. |
| RemoteOK | Low yield | 99 jobs → 6 usable |
| Remotive | Low yield | 15 per category → 2 usable |
| Jobicy | Empty | 0 results on the relevant tag |
| VC portfolio boards (consider.com-backed) | Gated | Every API path returns **403**, not 404, so the data exists but access is blocked on purpose. Covered through another source that has the same portfolio. |
| YC company directory | Not watchlistable | 0 of 48 matching companies had a reachable ATS board (most are ≤5 people with custom careers pages). Used as an outreach list instead. |

**Pattern:** Generic remote boards are full of data-annotation and "AI trainer" gigs, and the same vendor shows up across three boards. They cost judge calls and triage time and add almost nothing.

## Ongoing detection

- Per-board warnings + the repeat-failure tally ([failure-modes.md](failure-modes.md#warnings-existed-but-reached-nobody))
- `--check-watchlist` after any watchlist change
- Fellowship sources that died (403, 404, a JS-challenge page served where RSS should be) were each recorded with the observed response and removed
