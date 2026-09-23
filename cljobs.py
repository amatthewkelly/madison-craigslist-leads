#!/usr/bin/env python3
"""
cljobs.py - Madison Craigslist jobs + gigs lead finder.

Pulls 'all jobs' (jjj) and 'all gigs' (ggg) from Craigslist's own search API,
drops CDL postings, scams, MLM, surveys/research studies and corporate
recruiting, scores what's left, has Claude write a one-line summary of each
keeper, then appends them to a rolling text file on the Desktop and fires a
macOS notification.

Stdlib only. Run `./cljobs.py --help` for options.
"""

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CATCACHE_PATH = os.path.join(HERE, ".categories.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
SAPI = "https://sapi.craigslist.org/web/v8/postings/search/full"
CATEGORIES_REF = "https://reference.craigslist.org/Categories"

CAT_LABEL = {"jjj": "jobs", "ggg": "gigs"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg):
    sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))


def expand(p):
    return os.path.expanduser(os.path.expandvars(p))


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def get(url, headers=None, timeout=30):
    """GET a URL, following redirects, returning decoded text."""
    hdrs = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def get_with_retry(url, headers=None, timeout=30, tries=3, delay=2.0):
    last = None
    for attempt in range(tries):
        try:
            return get(url, headers=headers, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            # 403/429 mean we are being throttled; back off harder.
            if exc.code in (403, 429, 500, 502, 503):
                time.sleep(delay * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            time.sleep(delay * (2 ** attempt))
    raise last


# --------------------------------------------------------------------------
# craigslist fetch + decode
# --------------------------------------------------------------------------

def load_category_map():
    """CategoryID -> 3-letter abbreviation, cached on disk for a week."""
    try:
        st = os.stat(CATCACHE_PATH)
        if time.time() - st.st_mtime < 7 * 86400:
            with open(CATCACHE_PATH) as fh:
                return {int(k): v for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        pass
    try:
        cats = json.loads(get_with_retry(CATEGORIES_REF))
        mapping = {int(c["CategoryID"]): c["Abbreviation"]
                   for c in cats if c.get("CategoryID") and c.get("Abbreviation")}
        with open(CATCACHE_PATH, "w") as fh:
            json.dump(mapping, fh)
        return mapping
    except Exception as exc:                      # cache is an optimisation only
        log("category map unavailable (%s); falling back to generic URLs" % exc)
        return {}


def fetch_category(area_id, cat, host):
    url = ("%s?batch=%d-0-360-0-0&cc=US&lang=en&searchPath=%s"
           % (SAPI, area_id, cat))
    body = get_with_retry(url, headers={
        "Accept": "application/json",
        "Origin": "https://%s.craigslist.org" % host,
        "Referer": "https://%s.craigslist.org/" % host,
    })
    return json.loads(body)


def decode_items(payload, cat, host, catmap):
    """Turn Craigslist's positional/tagged array format into dicts."""
    data = payload.get("data", {})
    dec = data.get("decode", {})
    min_id = dec.get("minPostingId", 0)
    min_date = dec.get("minPostedDate", 0)
    locs = dec.get("locationDescriptions", [])
    out = []

    for item in data.get("items", []):
        scalars = [el for el in item if not isinstance(el, list)]
        tagged = {el[0]: el[1] for el in item
                  if isinstance(el, list) and len(el) == 2 and isinstance(el[0], int)}
        if len(scalars) < 3:
            continue

        try:
            pid = min_id + int(scalars[0])
            posted = min_date + int(scalars[1])
            cat_id = int(scalars[2])
        except (TypeError, ValueError):
            continue

        # Title is the trailing bare string; tag 12 is a shorter normalised form.
        title = ""
        if isinstance(scalars[-1], str) and not scalars[-1][:2].isdigit():
            title = scalars[-1]
        if not title:
            title = tagged.get(12, "") or ""
        title = title.strip()
        if not title:
            continue

        # scalars[4] looks like "1:<locIdx>~<lat>~<lon>"
        where = ""
        for sc in scalars:
            if isinstance(sc, str) and re.match(r"^\d+:\d+~", sc):
                try:
                    idx = int(sc.split(":", 1)[1].split("~", 1)[0])
                    if 0 < idx < len(locs) and isinstance(locs[idx], str):
                        where = locs[idx]
                except (ValueError, IndexError):
                    pass
                break

        abbr = catmap.get(cat_id, "")
        slug = tagged.get(6, "") or "d"
        if abbr:
            url = "https://%s.craigslist.org/%s/d/%s/%d.html" % (host, abbr, slug, pid)
        else:
            url = "https://%s.craigslist.org/d/%s/%d.html" % (host, slug, pid)

        out.append({
            "id": str(pid),
            "title": title,
            "url": url,
            "price": (tagged.get(7) or "").strip(),
            "company": (tagged.get(8) or "").strip(),
            "where": where,
            "posted": posted,
            "feed": cat,
            "cat_abbr": abbr,
            "body": "",
        })
    return out


BODY_RE = re.compile(r'id="postingbody".*?>(.*?)</section>', re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
QR_RE = re.compile(r"QR Code Link to This Post", re.I)


def fetch_body(url, timeout=25):
    try:
        page = get_with_retry(url, timeout=timeout, tries=2, delay=1.5)
    except Exception:
        return ""
    m = BODY_RE.search(page)
    if not m:
        return ""
    text = TAG_RE.sub(" ", m.group(1))
    text = html.unescape(text)
    text = QR_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# filtering + scoring
# --------------------------------------------------------------------------

class Rules(object):
    def __init__(self, cfg):
        self.blocks = []
        for group, pats in cfg["hard_block"].items():
            for p in pats:
                self.blocks.append((group, re.compile(p, re.I)))
        self.pos = [(e["w"], re.compile(e["re"], re.I), e["why"]) for e in cfg["positive"]]
        self.neg = [(e["w"], re.compile(e["re"], re.I), e["why"]) for e in cfg["negative"]]

    def blocked(self, text):
        """Return (group, matched_text) for the first hard block hit, else None."""
        for group, rx in self.blocks:
            m = rx.search(text)
            if m:
                return group, m.group(0).strip()
        return None

    def score(self, text):
        total, reasons = 0, []
        for w, rx, why in self.pos:
            if rx.search(text):
                total += w
                reasons.append("+%d %s" % (w, why))
        for w, rx, why in self.neg:
            if rx.search(text):
                total += w
                reasons.append("%d %s" % (w, why))
        return total, reasons


PHONE_RE = re.compile(r"\b(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]\d{4}\b")
# Singular only. "we/our" is what every corporate posting says ("we offer",
# "our team"), so counting it defeats the point of this heuristic.
FIRST_PERSON_RE = re.compile(r"\b(i|i'm|im|my|mine|me|myself)\b")
JUNK_TITLE_RE = re.compile(r"^[\W_]*\w+[\W_]*$")


def human_bonus(body):
    """Heuristics for 'a real person typed this', not a job board robot."""
    if not body:
        return 0, []
    bonus, why = 0, []
    # Case-sensitive on purpose: bare "I" is the signal, and lowercasing would
    # swallow every "i" inside acronyms and list markers.
    fp = len(FIRST_PERSON_RE.findall(body)) + len(
        re.findall(r"\b(I|I'm|My|Me)\b", body))
    if fp >= 6:
        bonus += 3
        why.append("+3 heavily first-person")
    elif fp >= 2:
        bonus += 1
        why.append("+1 first-person")
    if PHONE_RE.search(body):
        bonus += 2
        why.append("+2 phone number in post")
    n = len(body)
    if 80 <= n <= 900:
        bonus += 2
        why.append("+2 short human-length post")
    elif n > 3000:
        bonus -= 2
        why.append("-2 very long/boilerplate post")
    letters = [c for c in body if c.isalpha()]
    if len(letters) > 40:
        caps = sum(1 for c in letters if c.isupper()) / float(len(letters))
        if caps > 0.45:
            bonus -= 3
            why.append("-3 shouting in all caps")
    return bonus, why


DEDUPE_STRIP_RE = re.compile(r"[^a-z0-9 ]+")


def dedupe_key(post):
    """Craigslist reposts carry identical titles; collapse them to one lead."""
    t = DEDUPE_STRIP_RE.sub(" ", post["title"].lower())
    t = " ".join(t.split())
    return "%s|%s" % (t, (post.get("where") or "").lower().strip())


def dedupe(posts, already=()):
    """Keep the newest posting per key, dropping keys already reported."""
    seen = set(already)
    out = []
    for p in sorted(posts, key=lambda x: x["posted"], reverse=True):
        k = dedupe_key(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def evaluate(post, rules, min_score):
    """Attach verdict/score/reasons to a posting dict."""
    haystack = "\n".join([post["title"], post.get("company", ""),
                          post.get("price", ""), post.get("body", "")])
    hit = rules.blocked(haystack)
    if hit:
        post["verdict"] = "blocked"
        post["block_group"] = hit[0]
        post["block_match"] = hit[1]
        post["score"] = -999
        post["reasons"] = ["BLOCKED (%s): %r" % (hit[0], hit[1])]
        return post

    score, reasons = rules.score(haystack)
    hb, hwhy = human_bonus(post.get("body", ""))
    score += hb
    reasons.extend(hwhy)
    if not post.get("body"):
        score -= 1
        reasons.append("-1 body unavailable")
    if JUNK_TITLE_RE.match(post["title"]) or len(post["title"].strip()) < 8:
        score -= 3
        reasons.append("-3 contentless title")

    post["score"] = score
    post["reasons"] = reasons
    post["verdict"] = "keep" if score >= min_score else "low"
    return post


# --------------------------------------------------------------------------
# summarisation via the claude CLI
# --------------------------------------------------------------------------

PROMPT_HEAD = """You are triaging Craigslist job and gig postings for someone \
looking for real, hands-on work (cash jobs, short-term gigs, part-time and \
full-time roles) around Madison, Wisconsin.

For EACH numbered posting below, write one or two plain sentences (40 words max \
total) saying what the work actually is, what it pays, and any catch worth \
knowing. Be concrete and skip marketing language. If a posting looks like a scam, \
an MLM, a survey/research study, or a disguised recruiting ad, say so plainly in \
the sentence.

Output format: exactly one line per posting, as <number>|<sentence>
No preamble, no blank lines, no markdown, nothing else.

"""


def build_prompt(batch):
    parts = [PROMPT_HEAD]
    for i, p in enumerate(batch, 1):
        body = re.sub(r"\s+", " ", p.get("body", ""))[:1200]
        meta = " / ".join(x for x in (p.get("price"), p.get("where"),
                                      p.get("company")) if x)
        parts.append("[%d] %s%s\n%s\n" % (i, p["title"],
                                          (" (%s)" % meta) if meta else "",
                                          body or "(no body text available)"))
    return "\n".join(parts)


def fallback_summary(post):
    """Extractive first-sentences summary, used when Claude is unavailable."""
    body = re.sub(r"\s+", " ", post.get("body", "")).strip()
    if not body:
        bits = [x for x in (post.get("price"), post.get("where")) if x]
        return "%s%s" % (post["title"], (" - " + ", ".join(bits)) if bits else "")
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if len(out) + len(sent) > 200:
            break
        out += (" " if out else "") + sent
    return (out or body[:200]).strip()


def summarize(posts, cfg):
    """Return {post_id: sentence}. Falls back to extractive on any failure."""
    result = {}
    if not posts:
        return result
    if not cfg.get("summarize", True):
        return {p["id"]: fallback_summary(p) for p in posts}

    size = max(1, int(cfg.get("summarize_batch_size", 20)))
    batches = [posts[i:i + size] for i in range(0, len(posts), size)]

    for bi, batch in enumerate(batches, 1):
        got = {}
        try:
            log("summarising batch %d/%d (%d postings) with %s"
                % (bi, len(batches), len(batch), cfg.get("claude_model")))
            proc = subprocess.run(
                [cfg.get("claude_bin", "claude"), "-p",
                 "--model", cfg.get("claude_model", "claude-opus-5"),
                 build_prompt(batch)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=int(cfg.get("claude_timeout_sec", 240)),
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300]
                                   or "exit %d" % proc.returncode)
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                line = line.strip()
                m = re.match(r"^\[?(\d+)\]?\s*\|\s*(.+)$", line)
                if not m:
                    continue
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(batch):
                    got[batch[idx]["id"]] = m.group(2).strip()
        except Exception as exc:
            log("summariser failed (%s); using extractive fallback" % exc)

        for p in batch:
            result[p["id"]] = got.get(p["id"]) or fallback_summary(p)
    return result


# --------------------------------------------------------------------------
# state + rendering
# --------------------------------------------------------------------------

URL_IN_FILE_RE = re.compile(r"https?://[^\s]+craigslist\.org/[^\s]+")


def reconcile_dismissals(state, out_path, now):
    """Treat entries you deleted from the text file as dismissed.

    The text file is rendered from state on every run, so a hand-deleted entry
    would otherwise reappear. Anything missing from the file is removed from
    state and its title remembered, so a repost under a new posting ID does not
    bring it back either.

    If the file is absent we skip entirely - a deleted file means 'regenerate',
    not 'dismiss everything'.
    """
    if not state["entries"] or not os.path.isfile(out_path):
        return state, 0
    try:
        with open(out_path) as fh:
            present = set(URL_IN_FILE_RE.findall(fh.read()))
    except OSError:
        return state, 0

    keep, gone = [], []
    for e in state["entries"]:
        (keep if e.get("url") in present else gone).append(e)
    for e in gone:
        state.setdefault("dismissed", {})[dedupe_key(e)] = now
    state["entries"] = keep
    return state, len(gone)


def rotate_log(cfg, state, now):
    """Keep run.log bounded: weekly, or sooner if it gets large.

    launchd holds this file open in append mode, so we truncate in place
    rather than renaming - a rename would leave launchd writing to the
    detached inode and the live log would look empty forever.
    """
    path = expand(cfg.get("log_file", "~/.craigslistcash/run.log"))
    if not os.path.isfile(path):
        return state, None
    try:
        size = os.path.getsize(path)
    except OSError:
        return state, None

    max_b = int(cfg.get("log_max_bytes", 1048576))
    days = float(cfg.get("log_rotate_days", 7))
    last = state.get("last_log_rotate", 0)
    aged = (now - last) >= days * 86400 if last else False
    if not last:
        state["last_log_rotate"] = now
        return state, None
    if size <= max_b and not aged:
        return state, None

    reason = "weekly" if aged else "size %.1f KB" % (size / 1024.0)
    try:
        with open(path, "rb") as src:
            data = src.read()
        with open(path + ".1", "wb") as dst:    # keep one generation
            dst.write(data)
        os.truncate(path, 0)                    # same inode, launchd keeps writing
        state["last_log_rotate"] = now
        return state, "%s (%.1f KB -> run.log.1)" % (reason, size / 1024.0)
    except OSError as exc:
        log("log rotation failed: %s" % exc)
        return state, None


def load_state(path):
    try:
        with open(path) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("entries", [])
    st.setdefault("dismissed", {})
    return st


def save_state(path, state):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, path)


def prune(state, keep_days, seen_days, now):
    entry_cut = now - keep_days * 86400
    state["entries"] = [e for e in state["entries"]
                        if e.get("found_at", 0) >= entry_cut]
    seen_cut = now - seen_days * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= seen_cut}
    state["dismissed"] = {k: v for k, v in state.get("dismissed", {}).items()
                          if v >= seen_cut}
    return state


def wrap(text, width, indent):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    return ("\n" + indent).join(lines) if lines else ""


def render(entries, keep_days, now):
    W = 78
    stamp = datetime.fromtimestamp(now).strftime("%a %b %d %Y, %-I:%M %p")
    out = ["=" * W,
           " MADISON CRAIGSLIST LEADS".ljust(W - 1),
           (" updated %s" % stamp).ljust(W - 1),
           (" %d open leads - entries drop off after %d days"
            % (len(entries), keep_days)).ljust(W - 1),
           "=" * W, ""]

    if not entries:
        out.append("Nothing made it through the filter yet. The next run will add to this file.")
        out.append("")
        return "\n".join(out)

    # newest first, grouped by the day we found them
    entries = sorted(entries, key=lambda e: e.get("found_at", 0), reverse=True)
    last_day = None
    for e in entries:
        day = datetime.fromtimestamp(e.get("found_at", now)).strftime("%A, %B %-d")
        if day != last_day:
            out.append("")
            out.append("-" * W)
            out.append("  %s" % day.upper())
            out.append("-" * W)
            out.append("")
            last_day = day

        out.append("  " + wrap(e.get("summary", ""), W - 4, "  "))
        out.append("")
        meta = " · ".join(x for x in (e.get("price"), e.get("where"),
                                      CAT_LABEL.get(e.get("feed"), e.get("feed"))) if x)
        out.append("    " + wrap(e.get("title", ""), W - 6, "    "))
        if meta:
            out.append("    %s" % meta)
        out.append("    %s" % e.get("url", ""))
        out.append("")
    return "\n".join(out)


def write_out(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


TERMINAL_NOTIFIER = os.path.expanduser(
    "~/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier")


def notify(title, message, open_path=None):
    """Post a macOS notification. With terminal-notifier installed, clicking
    it opens open_path; otherwise fall back to osascript (click does nothing
    useful)."""
    if os.path.exists(TERMINAL_NOTIFIER):
        cmd = [TERMINAL_NOTIFIER, "-title", title[:80], "-message", message[:220],
               "-sound", "Glass", "-group", "craigslistcash"]
        if open_path:
            cmd += ["-execute", "/usr/bin/open %s" % shlex.quote(open_path)]
    else:
        def esc(s):
            return s.replace("\\", "\\\\").replace('"', '\\"')
        cmd = ["osascript", "-e",
               'display notification "%s" with title "%s" sound name "Glass"'
               % (esc(message[:220]), esc(title[:80]))]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
    except Exception as exc:
        log("notification failed: %s" % exc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Madison Craigslist jobs/gigs lead finder")
    ap.add_argument("--profile", choices=["strict", "balanced", "loose"],
                    help="override the filter profile in config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="print results, touch no state, write no files, send no notification")
    ap.add_argument("--no-notify", action="store_true", help="skip the macOS notification")
    ap.add_argument("--no-summarize", action="store_true", help="skip Claude, use extractive summaries")
    ap.add_argument("--reset-seen", action="store_true",
                    help="forget every posting seen before (next run treats all as new)")
    ap.add_argument("--explain", action="store_true",
                    help="print per-posting scoring decisions, including rejects")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-render the text file from saved state without fetching")
    ap.add_argument("--vacuum", action="store_true",
                    help="prune expired state and rotate the log now, then report "
                         "on-disk usage; fetches nothing")
    ap.add_argument("--repurge", action="store_true",
                    help="re-apply current hard_block rules to entries already on "
                         "file and drop any that now match, then re-render")
    args = ap.parse_args()

    cfg = load_config()
    if args.no_summarize:
        cfg["summarize"] = False

    profile_name = args.profile or cfg.get("profile", "balanced")
    profile = cfg["profiles"][profile_name]
    min_score = profile["min_score"]
    max_results = profile["max_results"]

    out_path = expand(cfg["output_file"])
    state_path = expand(cfg["state_file"])
    keep_days = int(cfg.get("keep_days", 4))
    now = int(time.time())

    state = load_state(state_path)

    state, rotated = rotate_log(cfg, state, now)
    if rotated:
        log("rotated run.log: %s" % rotated)

    if args.reset_seen:
        state["seen"] = {}
        log("cleared seen history")

    if args.vacuum:
        before = os.path.getsize(state_path) if os.path.isfile(state_path) else 0
        n_seen, n_ent = len(state["seen"]), len(state["entries"])
        state["last_log_rotate"] = 0                      # force rotation now
        state, rot = rotate_log(cfg, state, now)
        state = prune(state, keep_days, int(cfg.get("seen_retention_days", 21)), now)
        save_state(state_path, state)
        after = os.path.getsize(state_path)
        print("  seen ids   %5d -> %5d" % (n_seen, len(state["seen"])))
        print("  entries    %5d -> %5d" % (n_ent, len(state["entries"])))
        print("  dismissed  %5d" % len(state.get("dismissed", {})))
        print("  state.json %5.1f KB -> %5.1f KB" % (before / 1024.0, after / 1024.0))
        print("  log        %s" % (rot or "nothing to rotate"))
        return 0

    if args.repurge:
        rules = Rules(cfg)
        kept, dropped = [], []
        for e in state["entries"]:
            # Bodies are not stored, so re-check against what we did keep.
            hay = " ".join([e.get("title", ""), e.get("summary", ""),
                            e.get("price", ""), e.get("where", "")])
            hit = rules.blocked(hay)
            (dropped if hit else kept).append((e, hit))
        for e, hit in dropped:
            log("dropping (%s: %r) %s" % (hit[0], hit[1], e.get("title", "")[:60]))
        state["entries"] = [e for e, _ in kept]
        args.rebuild = True
        log("repurge: dropped %d, kept %d" % (len(dropped), len(kept)))

    if args.rebuild:
        state = prune(state, keep_days, int(cfg.get("seen_retention_days", 21)), now)
        write_out(out_path, render(state["entries"], keep_days, now))
        save_state(state_path, state)
        log("rebuilt %s from state (%d entries)" % (out_path, len(state["entries"])))
        return 0

    state, dismissed_n = reconcile_dismissals(state, out_path, now)
    if dismissed_n:
        log("%d entr%s deleted from the text file; dismissed for good"
            % (dismissed_n, "y was" if dismissed_n == 1 else "ies were"))

    rules = Rules(cfg)
    catmap = load_category_map()

    # 1. fetch both feeds
    posts = []
    for cat in cfg.get("categories", ["jjj", "ggg"]):
        try:
            payload = fetch_category(cfg["area_id"], cat, cfg["area_host"])
            got = decode_items(payload, cat, cfg["area_host"], catmap)
            log("%s: %d postings" % (cat, len(got)))
            posts.extend(got)
        except Exception as exc:
            log("ERROR fetching %s: %s" % (cat, exc))

    if not posts:
        log("no postings fetched; leaving previous results in place")
        if not args.dry_run:
            notify("Craigslist leads", "Could not reach Craigslist this run.")
        return 1

    # 2. de-dupe against what we have already reported
    max_age = int(cfg.get("max_age_days", 10)) * 86400
    fresh = [p for p in posts
             if p["id"] not in state["seen"] and (now - p["posted"]) <= max_age]
    log("%d total, %d new and recent enough" % (len(posts), len(fresh)))

    prior_keys = {dedupe_key(e) for e in state["entries"]}
    prior_keys |= set(state.get("dismissed", {}))
    before = len(fresh)
    fresh_unique = dedupe(fresh, prior_keys)
    if before != len(fresh_unique):
        log("collapsed %d repost/duplicate title(s)" % (before - len(fresh_unique)))

    # 3. cheap title-only pass so we do not fetch bodies for obvious junk
    survivors, rejected = [], []
    for p in fresh_unique:
        hit = rules.blocked(p["title"] + " " + p.get("company", ""))
        if hit:
            p["verdict"] = "blocked"
            p["block_group"], p["block_match"] = hit
            p["reasons"] = ["BLOCKED on title (%s): %r" % hit]
            rejected.append(p)
        else:
            survivors.append(p)
    log("%d survived the title pass, %d blocked outright" % (len(survivors), len(rejected)))

    # 4. fetch bodies (newest first, capped, politely spaced)
    survivors.sort(key=lambda p: p["posted"], reverse=True)
    cap = int(cfg.get("max_body_fetches", 80))
    delay = float(cfg.get("request_delay_sec", 0.7))
    for i, p in enumerate(survivors[:cap]):
        p["body"] = fetch_body(p["url"])
        if i + 1 < min(cap, len(survivors)):
            time.sleep(delay)
    if len(survivors) > cap:
        log("body fetch capped at %d (%d skipped)" % (cap, len(survivors) - cap))

    # 5. full evaluation
    keepers = []
    for p in survivors:
        evaluate(p, rules, min_score)
        if p["verdict"] == "keep":
            keepers.append(p)
        else:
            rejected.append(p)

    keepers.sort(key=lambda p: (p["score"], p["posted"]), reverse=True)
    keepers = keepers[:max_results]
    log("%d keepers at profile '%s' (min_score=%d)"
        % (len(keepers), profile_name, min_score))

    if args.explain:
        print("\n===== REJECTED (%d) =====" % len(rejected))
        for p in sorted(rejected, key=lambda x: x.get("score", -999), reverse=True):
            print("  [%5s] %s\n           %s" % (p.get("score", "block"),
                                                 p["title"][:70],
                                                 "; ".join(p.get("reasons", []))[:150]))
        print("\n===== KEPT (%d) =====" % len(keepers))
        for p in keepers:
            print("  [%5d] %s\n           %s" % (p["score"], p["title"][:70],
                                                 "; ".join(p["reasons"])[:150]))

    # 6. summarise
    summaries = summarize(keepers, cfg)

    new_entries = []
    for p in keepers:
        new_entries.append({
            "id": p["id"], "title": p["title"], "url": p["url"],
            "price": p.get("price", ""), "where": p.get("where", ""),
            "feed": p["feed"], "score": p["score"],
            "summary": summaries.get(p["id"], fallback_summary(p)),
            "posted": p["posted"], "found_at": now,
        })

    if args.dry_run:
        print(render(new_entries, keep_days, now))
        log("dry run: nothing written, state untouched")
        return 0

    # 7. persist: mark everything seen, add entries, prune, render
    for p in fresh:
        state["seen"][p["id"]] = now
    state["entries"] = new_entries + state["entries"]
    state = prune(state, keep_days, int(cfg.get("seen_retention_days", 21)), now)

    write_out(out_path, render(state["entries"], keep_days, now))
    save_state(state_path, state)
    log("wrote %d new (%d total on file) -> %s"
        % (len(new_entries), len(state["entries"]), out_path))

    # 8. notify
    if cfg.get("notify", True) and not args.no_notify:
        if new_entries:
            top = new_entries[0]["title"]
            msg = ("%d new lead%s - top: %s"
                   % (len(new_entries), "" if len(new_entries) == 1 else "s", top))
            notify("Craigslist leads (Madison)", msg, open_path=out_path)
        else:
            log("no new leads; skipping notification")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
