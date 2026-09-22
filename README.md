# craigslistcash

Twice-daily lead finder for Madison Craigslist **jobs** (`jjj`) and **gigs**
(`ggg`).

It pulls both categories, throws out CDL roles, roofing work, scams, MLM
pitches, surveys and research studies, gig-platform signup ads and corporate
recruiting, scores what survives, has Claude write a one-or-two sentence
summary of each keeper, then appends them to a rolling text file on your
Desktop and fires a macOS notification.

Python 3 standard library only — no `pip install`.

## What you get

`~/Desktop/craigslist-leads.txt`, newest first, grouped by the day found.
**Entries drop off automatically after 4 days.**

```
  Light in-home help on Madison's east side — juicing, chopping vegetables,
  sweeping, folding — about 5 hours a day, 3-4 days a week, $14/hr cash
  weekly; women-only ad requiring name, age and a background check by text.

    Household Support Female Helper Needed
    $14 per hour cash paid weekly · Madison · jobs
    https://madison.craigslist.org/lab/d/madison-household-support-female-helper/7965844179.html
```

The third line tags each lead `jobs` or `gigs` so you know which feed it came
from.

The summary is written by Claude from the full posting body, so it states the
actual work, the pay, and the catch. It says so outright when a posting smells
like a scam — a second line of defense behind the keyword filter, and it has
already caught things the keywords missed.

## Files

| file | role |
|---|---|
| `cljobs.py` | the whole program — fetch, filter, score, summarise, render |
| `config.json` | every tunable; re-read on each run, no reinstall needed |
| `install.sh` | writes and loads the launchd job; re-run to change times |
| `uninstall.sh` | unloads the job, leaves your file and history alone |
| `.categories.json` | cached Craigslist category→URL map, self-refreshes weekly |
| `~/.craigslistcash/state.json` | `seen` IDs, live `entries`, `dismissed` titles |
| `~/.craigslistcash/run.log` | run output, rotated weekly |
| `~/Desktop/craigslist-leads.txt` | the output you read |

## Install

```bash
./install.sh            # runs at 08:30 and 16:30 daily
./install.sh 7 18 15    # or: 07:15 and 18:15
```

Re-run `install.sh` any time to change the times. `./uninstall.sh` removes the
schedule.

## Dismissing leads

**Delete an entry out of the text file and it stays gone.** The next normal run
compares the file against its own state: anything you removed is dropped
permanently and its title remembered, so neither the original posting nor a
repost under a new ID comes back. Dismissals expire after `seen_retention_days`
(21 days).

Delete as much or as little of the entry as you like — it keys on the URL, so
once that line is gone the entry is dismissed.

Two things to know:

- **Only a normal run reconciles deletions.** `--rebuild`, `--vacuum` and
  `--repurge` all regenerate from state and return early, so running one of
  those after deleting entries brings them back. Just let the scheduled run
  pick it up.
- **Deleting the whole file is not a mass-dismiss.** A missing file means
  "regenerate", and the next run rebuilds it from state. To clear everything,
  empty the file but leave it in place.

Any edit other than deleting whole entries — notes, reordering — is overwritten,
because the file is re-rendered from state every run.

## Running it by hand

```bash
./cljobs.py                        # normal run
./cljobs.py --dry-run --explain    # see every decision, change nothing
./cljobs.py --explain              # show why each posting was kept or rejected
./cljobs.py --profile strict       # try a tighter filter for one run
./cljobs.py --vacuum               # prune state, rotate log, report disk usage
./cljobs.py --rebuild              # re-render the text file from saved state
./cljobs.py --repurge              # re-apply hard_block rules to entries
                                   #   already on file and drop new matches
./cljobs.py --reset-seen           # forget history; next run treats all as new
./cljobs.py --no-summarize         # skip Claude, use extractive summaries
./cljobs.py --no-notify            # skip the macOS notification
```

`--explain` is the one to reach for when tuning. It prints every rejected
posting with the exact rule that killed it, and every keeper with its score
breakdown. Pair it with `--dry-run` to change nothing.

Run `--repurge` after tightening `hard_block`: new rules otherwise only affect
future runs, leaving already-listed postings on the file. It matches against the
stored title and Claude's summary rather than the original body — which catches
extra things the summary revealed, but means a summary that merely *mentions* a
blocked word drops that entry. Check its log lines; anything dropped by mistake
returns on the next run if you loosen the rule.

## How it works

Each run, in order:

1. **Rotate the log** if it is a week old or over 1 MB.
2. **Reconcile dismissals** — entries missing from the text file are dropped
   from state and remembered. *(Skipped by `--rebuild`/`--vacuum`/`--repurge`,
   which return before this point.)*
3. **Fetch both categories** from `sapi.craigslist.org`, Craigslist's own JSON
   search API — the same endpoint their website calls. The legacy RSS feeds
   return HTTP 403 and are not usable. ~550 postings.
4. **Drop anything already seen** or older than `max_age_days`.
5. **Collapse reposts** by normalised title + location, also against entries
   already on file and past dismissals. Craigslist reposts heavily — a typical
   run collapses 50–75 duplicates.
6. **Title-only block pass** rejects obvious junk *before* spending a request
   on its body.
7. **Fetch bodies** for survivors, newest first, capped at `max_body_fetches`
   and spaced by `request_delay_sec`.
8. **Score** against title + body + company + price: hard blocks first, then
   weighted positive/negative patterns, then the human-authorship heuristic.
   Rank, and cap to the profile's limit.
9. **Summarise** keepers via the `claude` CLI, in batches of
   `summarize_batch_size`.
10. **Persist** — mark seen, merge entries, prune to `keep_days`, re-render the
    text file from state, then notify if anything is new.

The text file is a rendered view of `state.json`, never the source of truth —
which is why dismissals are reconciled *from* it at step 2 before anything
overwrites it at step 10.

## Tuning the filter

Everything lives in `config.json` and is re-read on every run.

### Threshold

```json
"profile": "balanced"
```

| profile | min score | cap | behaviour |
|---|---|---|---|
| `strict` | 6 | 12 | only postings clearly hitting cash / short-term / real-person |
| `balanced` | 2 | 25 | **default** — hard blocks, then anything scoring positive |
| `loose` | -999 | 60 | hard blocks only; everything else comes through |

On a clean run these yield roughly 12 / 25 / 60 leads. Change the word to switch
profiles; the thresholds themselves live under the `profiles` key, where you can
nudge `min_score` or `max_results` on any of the three.

### Rules

`hard_block` — a single match rejects the posting outright, whatever it scores.
168 patterns in nine groups:

| group | patterns | group | patterns |
|---|---|---|---|
| `scam` | 38 | `mlm` | 19 |
| `corporate_recruiting` | 29 | `gig_platform_signup` | 18 |
| `survey_research` | 24 | `misc_bad_fit` | 13 |
| `cdl` | 11 | `lead_gen_pitch` | 9 |
| `roofing` | 7 | | |

To stop excluding a whole category, delete its group or set it to `[]`.

`positive` / `negative` — weighted regexes, each `{"w": weight, "re": pattern,
"why": label}`. The label is what `--explain` prints. Add your own freely.

All patterns are case-insensitive Python regexes, and a bad one fails loudly on
the next run rather than silently matching nothing.

Separately, the script scores *did a human write this*: first-person singular
(`I`, `my`, `me` — deliberately **not** `we`/`our`, which every corporate ad
uses), a phone number in the body, and a short human post length, against
penalties for all-caps shouting and boilerplate-length postings.

### Other knobs

| key | meaning |
|---|---|
| `area_id` / `area_host` | `165` / `madison` — change both to track another city |
| `categories` | `["jjj", "ggg"]` — all jobs and all gigs |
| `keep_days` | how long entries stay in the text file (default 4) |
| `seen_retention_days` | how long IDs and dismissals are remembered (default 21) |
| `output_file` / `state_file` | where the text file and state live |
| `notify` | macOS notification on/off |
| `summarize` | `false` uses extractive summaries, no Claude call |
| `claude_bin` / `claude_model` | CLI path and model (default `claude-opus-5`) |
| `claude_timeout_sec` | per-batch summariser timeout (default 240) |
| `summarize_batch_size` | postings per Claude call (default 20) |
| `max_body_fetches` | per-run cap on body downloads (default 200) |
| `request_delay_sec` | spacing between body fetches, to stay polite |
| `max_age_days` | ignore postings older than this |
| `log_file` / `log_rotate_days` / `log_max_bytes` | log path, 7 days, 1 MB |

## Disk usage

Nothing grows without bound. `state.json` prunes itself on every run — entries
expire after `keep_days`, `seen` IDs and dismissals after `seen_retention_days`
— capping it around 250 KB at the busiest. `run.log` is the one file launchd
appends to forever, so the script rotates it weekly or past 1 MB, keeping one
generation as `run.log.1`. Rotation truncates in place rather than renaming,
because launchd holds that file open in append mode and a rename would leave it
writing to a detached inode. Worst case for the whole tool is a couple of MB.

```bash
./cljobs.py --vacuum
```

Prunes expired state, rotates the log, prints before/after counts. Fetches
nothing, safe any time.

## Notes

- Summaries shell out to the `claude` CLI, using your existing Claude Code login
  — no API key needed. If the CLI is missing, unauthenticated or times out, the
  run **does not fail**: it falls back to an extractive summary of the posting's
  opening sentences and says so in the log.
- A run that reaches Craigslist but finds nothing new sends no notification. If
  Craigslist is unreachable the previous file is left untouched.
- First run is the slow one (~5 min, a few hundred bodies to fetch). Later runs
  only see what is new, so they are much shorter.
- If notifications do not appear, check System Settings → Notifications → Script
  Editor.
