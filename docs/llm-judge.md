# LLM judge

## Role in the pipeline

The judge handles the parts that need judgment: how well a role fits the candidate, and cleaning up messy company and title text (mostly from HN free text). Anything a fixed rule can decide, like industry exclusions, seniority bands, and location eligibility, is filtered in code **before** the judge sees it. That's cheaper, you get the same answer every time, and you can check it.

| Decided by code | Decided by the judge |
|---|---|
| Title keywords / exclusions | Fit score 1–5 |
| Industry hard exclusions | A one-sentence reason for the score |
| Location eligibility | Cleaned-up company and title |
| Dedup, ranking, caps | |

Hard exclusions show up in **both** places: as a regex filter, and in the rubric as "score 1". The regex is what actually enforces them. The rubric line is a backstop if the regex misses.

## Mechanics

- Runs `claude -p` headless as a subprocess, with a 180 s timeout and one retry
- **Batches of 12** listings. Each listing is numbered, and the judge returns a JSON array keyed by `index`.
- The candidate profile is read from the project config at runtime, so the rubric always matches the current profile
- Output is parsed defensively: code fences are stripped, the first `[...]` array is extracted, and out-of-range indices are ignored
- Scores are matched to rows by **index**, not by position, so a reordered or partial response can't attach a score to the wrong listing
- **Environment isolation:** a global API key in the shell would override the CLI's auth. The subprocess gets a copy of the environment with that key removed. The user's shell is left untouched.

## Degraded mode

| What failed | What happens to the rows | Run outcome |
|---|---|---|
| Whole batch (subprocess error, timeout, no JSON) | All 12 pass through, `fit=None`, `UNJUDGED - judge failed: <Type>` | Exit **2** |
| Some indices missing or have no numeric score | Those rows pass through, `UNJUDGED - judge returned no usable score` | Warning line, counted |

`score_listings()` returns `(scored, health)`, where `health = {batches, failed_batches, unjudged, errors}`. The caller has to check it. If any batch failed, `scout.py` prints a DEGRADED block to stderr and exits 2. The runner then puts the degradation **first** in the digest email and fires a critical notification.

Rows are never dropped because the judge couldn't score them. A judge outage costs triage time, not leads.

## Untrusted input

Listing text is scraped from public sources and treated as **data, never instructions**. This isn't hypothetical: job posts often include anti-spam strings like "mention the word X when applying" to catch automated applicants.

Defenses:
- The rubric says plainly that listing content is untrusted, and that anything that looks like a command should be scored as content, not obeyed
- Listings are placed in a clearly separated section after the rubric and profile
- The same rule is in the **email sender's** prompt, since titles and notes go into the digest
- The email sender can use only two tools: read files, and send email to one fixed address. A successful injection can't apply to jobs, send email to anyone else, or touch other systems.

## Why `--judge-all`

Watchlist rows used to skip the judge, because those companies were already hand-picked. But a row with no score can't be ranked against a row with one. The daily run now scores every source, so all rows in the digest are comparable.
