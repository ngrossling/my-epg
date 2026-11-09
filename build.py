#!/usr/bin/env python3
"""Personal mirror of the Ganja Release playlist + EPG.

* Pulls the upstream playlist and EPG (so new categories/channels flow in automatically).
* Drops the EPGenius category.
* Rewrites the NFL / College Football / NBA slot channels with stable tvg-ids and builds
  their guide data ourselves from the provider's live channel names + ESPN schedules.
* Prunes the EPG to the channels the playlist actually uses.

Standard library only. Configuration comes from environment variables (see README.md).
"""
import gzip
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
UA = "Mozilla/5.0 (personal-epg-mirror)"

UPSTREAM_M3U = os.environ.get("UPSTREAM_M3U", "")
UPSTREAM_EPG = os.environ.get(
    "UPSTREAM_EPG", "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg6.xml.gz")
XTREAM_HOST = os.environ.get("XTREAM_HOST", "").rstrip("/")
XTREAM_USER = os.environ.get("XTREAM_USER", "")
XTREAM_PASS = os.environ.get("XTREAM_PASS", "")
EPG_URL = os.environ.get("EPG_URL", "")
OUT_DIR = os.environ.get("OUT_DIR", "out")
SPOILERS = os.environ.get("SPOILERS", "0") == "1"

# Playlist groups to drop entirely (compared after normalising away emoji/punctuation).
DROP_GROUPS = {"epgenius"}

# Slot groups we generate ourselves. Key = normalised group-title.
LEAGUES = {
    "nfl sunday ticket": dict(
        key="nfl", label="NFL", espn="football/nfl", params=[""], hours=3.5,
        emoji="\U0001F3C8", sport="Football", logo="https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png"),
    "college football": dict(
        key="ncaaf", label="NCAAF", espn="football/college-football", params=["&groups=80", "&groups=81"],
        hours=3.75, emoji="\U0001F3C8", sport="Football",
        logo="https://a.espncdn.com/i/teamlogos/ncaa_conf/500/ncaa.png"),
    "nba league pass": dict(
        key="nba", label="NBA", espn="basketball/nba", params=[""], hours=2.75,
        emoji="\U0001F3C0", sport="Basketball", logo="https://a.espncdn.com/i/teamlogos/leagues/500/nba.png"),
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def log(*a):
    print(*a, flush=True)


SECRETS = [s for s in (XTREAM_USER, XTREAM_PASS, UPSTREAM_M3U) if s]


def add_secret(s):
    """Hide a value we discovered at runtime, both in our own messages and in the Actions log."""
    if s and s not in SECRETS:
        SECRETS.append(s)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{s}", flush=True)


def secret_free(msg):
    """Strip credentials from anything we print (Actions logs are public on public repos)."""
    msg = str(msg)
    for s in SECRETS:
        if s:
            msg = msg.replace(s, "***")
    return msg


def fetch(src, timeout=120):
    if not re.match(r"https?://", src):
        with open(src, "rb") as f:
            return f.read()
    req = urllib.request.Request(src, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        raise RuntimeError(secret_free(f"download failed: {e}")) from None


# ---------------------------------------------------------------- time helpers

def eastern_offset(dt):
    """US Eastern UTC offset (hours) for an aware UTC datetime, without needing tzdata."""
    def nth_sunday(y, m, n):
        d = datetime(y, m, 1, tzinfo=UTC)
        return d + timedelta(days=(6 - d.weekday()) % 7, weeks=n - 1)
    start = nth_sunday(dt.year, 3, 2).replace(hour=7)
    end = nth_sunday(dt.year, 11, 1).replace(hour=6)
    return -4 if start <= dt < end else -5


def to_et(dt):
    return dt.astimezone(timezone(timedelta(hours=eastern_offset(dt))))


def et_midnight_utc(dt):
    e = to_et(dt).replace(hour=0, minute=0, second=0, microsecond=0)
    return e.astimezone(UTC)


def fmt_et(dt):
    e = to_et(dt)
    return f"{e:%a} {e.hour % 12 or 12}:{e.minute:02d} {'AM' if e.hour < 12 else 'PM'} ET"


def xmltv_time(dt):
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")


# ---------------------------------------------------------------- playlist

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
EXTINF_RE = re.compile(r'^(#EXTINF:\S*)((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$')
STREAM_RE = re.compile(r"^(https?://[^/]+)/(live/)?([^/]+)/([^/]+)/(\d+)(\.\w+)?$")


def norm_group(g):
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", g).lower()).strip()


def parse_m3u(text):
    lines = text.splitlines()
    header, entries, pending = lines[0] if lines else "#EXTM3U", [], []
    for line in lines[1:]:
        if not line.strip():
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        extinf = next((p for p in pending if p.startswith("#EXTINF")), None)
        if extinf:
            m = EXTINF_RE.match(extinf)
            if m:
                entries.append(dict(
                    prefix=m.group(1), attrs=dict(ATTR_RE.findall(m.group(2))), name=m.group(3),
                    extra=[p for p in pending if p is not extinf], url=line.strip()))
            else:
                log("warn: could not parse", extinf[:120])
        pending = []
    return header, entries


def entry_to_text(e):
    attrs = "".join(f' {k}="{v}"' for k, v in e["attrs"].items())
    return "\n".join([f"{e['prefix']}{attrs},{e['name']}", *e["extra"], e["url"]])


def upstream_credentials(header_lines):
    for line in header_lines:
        if line.startswith("#EXT-X-CREDENTIALS:"):
            try:
                c = json.loads(line.split(":", 1)[1])[0]
                return c["dns"].rstrip("/"), c["username"], c["password"]
            except Exception:
                pass
    return None


# ---------------------------------------------------------------- ESPN

def espn_events(league, now):
    days = [(to_et(now) + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1, 2)]
    events = {}
    for day, extra in [(d, x) for d in days for x in league["params"]]:
        url = (f"https://site.api.espn.com/apis/site/v2/sports/{league['espn']}/scoreboard"
               f"?dates={day}&limit=300{extra}")
        try:
            data = json.loads(fetch(url, timeout=60))
        except Exception as e:
            log(f"warn: ESPN {league['key']} {day}{extra}: {e}")
            continue
        for ev in data.get("events", []):
            parsed = parse_event(ev)
            if parsed:
                events[parsed["id"]] = parsed
    return list(events.values())


def parse_event(ev):
    try:
        comp = ev["competitions"][0]
        side = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = side["home"], side["away"]
    except (KeyError, IndexError):
        return None

    def team(c):
        t = c.get("team", {})
        names = {t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("location")}
        if len(t.get("abbreviation") or "") >= 3:
            names.add(t["abbreviation"])
        aliases = set()
        for n in filter(None, names):
            a = norm_text(re.sub(r"[()]", " ", n)).strip()
            if a:
                aliases.update({a, a.replace(" state", " st")})
        return dict(
            full=t.get("displayName") or t.get("name") or "?",
            short=t.get("shortDisplayName") or t.get("name") or "?",
            logo=t.get("logo"), aliases=aliases, score=c.get("score"),
            record=(c.get("records") or [{}])[0].get("summary"),
            rank=(c.get("curatedRank") or {}).get("current"))

    venue = comp.get("venue") or {}
    addr = venue.get("address") or {}
    broadcasts = []
    for b in comp.get("broadcasts") or []:
        broadcasts += b.get("names") or []
    return dict(
        id=ev["id"],
        start=datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(UTC),
        state=(ev.get("status") or {}).get("type", {}).get("state", "pre"),
        home=team(home), away=team(away), neutral=comp.get("neutralSite", False),
        venue=", ".join(filter(None, [venue.get("fullName"), addr.get("city"), addr.get("state")])),
        broadcasts=", ".join(dict.fromkeys(broadcasts)),
        odds=((comp.get("odds") or [{}])[0] or {}).get("details"),
        note=((comp.get("notes") or [{}])[0] or {}).get("headline"),
        headline=((comp.get("headlines") or [{}])[0] or {}).get("description"),
    )


# ---------------------------------------------------------------- slot name matching

NOISE = [
    r"start:.*$", r"\(\s*\d{4}-\d\d-\d\d[^)]*\)", r"-\s*no event streaming\s*-", r"8k exclusive",
    r"\bus:\s*nba pass ppv \d+", r"\((?:us)\)", r"^\s*(?:live|next)\s*\|", r"^\s*us\s*\(\w+\s*\d+\)\s*\|",
    r":\s*(?:nfl|ncaaf|nba|ncaab)\s+\d+\s*$", r"^\s*(?:nfl|ncaaf|nba)\s*\|?\s*\d*\s*[-:]?",
    r"^\s*(?:nfl|ncaaf|nba)\s+\d+\s*:", r"^\s*\d+k?\s*-", r"\|",
]


def clean_slot_name(raw):
    s = raw
    for pat in NOISE:
        s = re.sub(pat, " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -:|")


def norm_text(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " " + re.sub(r"[^a-z0-9]+", " ", s).strip() + " "


def explicit_date(raw, now):
    """Date written in the provider name, if any (used to reject stale names)."""
    m = re.search(r"(\d{4})-(\d\d)-(\d\d)", raw)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\b", raw) or re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})\b", raw)
        if not m:
            return None
        a, b = m.groups()
        mon, day = (a, b) if a.isalpha() else (b, a)
        if mon.lower() not in MONTHS:
            return None
        y, mo, d = to_et(now).year, MONTHS[mon.lower()], int(day)
    try:
        return datetime(y, mo, d).date()
    except ValueError:
        return None


def alias_spans(text, aliases):
    out = []
    for a in aliases:
        i = text.find(f" {a} ")
        while i >= 0:
            out.append((i + 1, i + 1 + len(a)))
            i = text.find(f" {a} ", i + 1)
    return out


def match_event(raw, events, now):
    text = norm_text(clean_slot_name(raw))
    when = explicit_date(raw, now)
    best, best_key = None, None
    for ev in events:
        # Both teams must match, on different parts of the name. A match sitting inside a longer match of the
        # other team belongs to that team ("Michigan" inside "Michigan State" is not Michigan).
        hs = alias_spans(text, ev["home"]["aliases"])
        aws = alias_spans(text, ev["away"]["aliases"])
        hs, aws = ([s for s in mine if not any(o[0] <= s[0] and s[1] <= o[1] and o[1] - o[0] > s[1] - s[0]
                                               for o in other)]
                   for mine, other in ((hs, aws), (aws, hs)))
        score = max((h[1] - h[0] + a[1] - a[0] for h in hs for a in aws
                     if h[1] <= a[0] or a[1] <= h[0]), default=0)
        if not score:
            continue
        bonus = 5 if when and abs((to_et(ev["start"]).date() - when).days) <= 1 else 0
        key = (-(score + bonus), ev["state"] == "post", abs((ev["start"] - now).total_seconds()))
        if best_key is None or key < best_key:
            best, best_key = ev, key
    return best


def slot_status(raw, now):
    """'empty' / 'stale' / 'named' for an unmatched provider slot name."""
    cleaned = clean_slot_name(raw)
    if len(re.sub(r"[^A-Za-z]", "", cleaned)) < 3:
        return "empty", cleaned
    d = explicit_date(raw, now)
    if d and abs((d - to_et(now).date()).days) > 2:
        return "stale", cleaned
    return "named", cleaned


# ---------------------------------------------------------------- EPG generation

def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def chunks(a, b, hours=4):
    out, step = [], timedelta(hours=hours)
    while a < b:
        out.append((a, min(a + step, b)))
        a += step
    return out


def programme(cid, start, stop, title, desc=None, subtitle=None, cats=(), icon=None, live=False):
    p = ET.Element("programme", {"start": xmltv_time(start), "stop": xmltv_time(stop), "channel": cid})
    sub(p, "title", title, lang="en")
    if subtitle:
        sub(p, "sub-title", subtitle, lang="en")
    if desc:
        sub(p, "desc", desc, lang="en")
    for c in cats:
        sub(p, "category", c, lang="en")
    if icon:
        sub(p, "icon", src=icon)
    if live:
        sub(p, "live")
    return p


def team_label(t):
    return f"#{t['rank']} {t['short']}" if t.get("rank") and t["rank"] <= 25 else t["short"]


def event_programmes(cid, ev, league, win_start, win_end):
    sep = "vs" if ev["neutral"] else "@"
    short = f"{team_label(ev['away'])} {sep} {team_label(ev['home'])}"
    full = f"{ev['away']['full']} {'vs' if ev['neutral'] else 'at'} {ev['home']['full']}"
    start, end = ev["start"], ev["start"] + timedelta(hours=league["hours"])
    bits = [ev["headline"] or ev["note"]]
    if ev["venue"]:
        bits.append(f"\U0001F4CD {ev['venue']}")
    if ev["broadcasts"]:
        bits.append(f"\U0001F4FA TV: {ev['broadcasts']}")
    if ev["away"]["record"] or ev["home"]["record"]:
        bits.append(f"\U0001F4CA Records: {ev['away']['short']} {ev['away']['record'] or '-'}"
                    f" • {ev['home']['short']} {ev['home']['record'] or '-'}")
    if ev["odds"]:
        bits.append(f"\U0001F3B2 Odds: {ev['odds']}")
    desc = " ".join(filter(None, bits))
    cats = ["Sports", league["sport"], league["label"]]
    icon = ev["home"]["logo"] or league["logo"]
    out = []
    for a, b in chunks(win_start, min(start, win_end)):
        out.append(programme(cid, a, b, f"{fmt_et(start)}: {short}", desc, full, cats, icon))
    if start < win_end:
        out.append(programme(cid, max(start, win_start), min(end, win_end),
                             f"{league['emoji']} {short}", desc, full, cats, icon, live=True))
    final = ""
    if SPOILERS and ev["state"] == "post":
        final = f" — Final: {ev['away']['short']} {ev['away']['score']}, {ev['home']['short']} {ev['home']['score']}"
    for a, b in chunks(max(end, win_start), win_end):
        out.append(programme(cid, a, b, f"Ended: {short}{final}", desc, full, cats, icon))
    return out


def filler_programmes(cid, title, desc, league, win_start, win_end):
    return [programme(cid, a, b, title, desc, None, ["Sports"], league["logo"])
            for a, b in chunks(win_start, win_end)]


# ---------------------------------------------------------------- main build

def main():
    if not UPSTREAM_M3U:
        sys.exit("UPSTREAM_M3U is not set")
    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(UTC)

    log("Downloading upstream playlist ...")
    raw_m3u = fetch(UPSTREAM_M3U).decode("utf-8-sig", errors="replace")
    if not raw_m3u.lstrip().startswith("#EXTM3U"):
        sys.exit("Upstream playlist does not look like an M3U (Drive link/permissions?)")
    header, entries = parse_m3u(raw_m3u)
    header_lines = [l for l in raw_m3u.splitlines()[:5]]
    log(f"  {len(entries)} upstream entries")

    host, user, pw = XTREAM_HOST, XTREAM_USER, XTREAM_PASS
    if not (host and user and pw):
        creds = upstream_credentials(header_lines)
        if not creds:
            sys.exit("No Xtream credentials (set XTREAM_HOST/USER/PASS)")
        host, user, pw = creds
    for s in (user, pw):
        add_secret(s)

    # --- playlist rewrite
    kept, slots = [], []
    for e in entries:
        group = e["attrs"].get("group-title", "")
        ng = norm_group(group)
        if ng in DROP_GROUPS:
            continue
        m = STREAM_RE.match(e["url"])
        if m:
            e["url"] = f"{host}/{m.group(2) or ''}{user}/{pw}/{m.group(5)}{m.group(6) or ''}"
        league = LEAGUES.get(ng)
        if league and "dsx-slot" in e["attrs"] and m:
            slot = e["attrs"]["dsx-slot"]
            backup = "(Backup)" in e["name"]
            for k in [k for k in e["attrs"] if k.startswith("dsx-")]:
                del e["attrs"][k]
            cid = f"slot.{league['key']}.{m.group(5)}"
            e["attrs"]["tvg-id"] = cid
            e["attrs"]["tvg-logo"] = league["logo"]
            slots.append(dict(entry=e, league=league, slot=slot, stream_id=m.group(5), cid=cid, backup=backup))
        kept.append(e)
    log(f"  kept {len(kept)} entries, {len(slots)} sports slots")

    # --- provider slot names
    log("Reading provider channel names ...")
    api = f"{host}/player_api.php?" + urllib.parse.urlencode(
        {"username": user, "password": pw, "action": "get_live_streams"})
    names = {str(s["stream_id"]): s.get("name") or "" for s in json.loads(fetch(api, timeout=180))}

    # --- ESPN schedules
    events = {}
    for league in LEAGUES.values():
        events[league["key"]] = espn_events(league, now)
        log(f"  ESPN {league['key']}: {len(events[league['key']])} events")

    win_start = et_midnight_utc(now) - timedelta(days=1)
    win_end = et_midnight_utc(now) + timedelta(days=3)
    gen_channels, gen_programmes, report = [], [], []
    for s in slots:
        league, cid, e = s["league"], s["cid"], s["entry"]
        raw = names.get(s["stream_id"], "")
        ev = match_event(raw, events[league["key"]], now) if raw else None
        label = f"{league['label']} {s['slot']}"
        suffix = " (Backup)" if s["backup"] else ""
        ch = ET.Element("channel", {"id": cid})
        sub(ch, "display-name", label + suffix, lang="en")
        sub(ch, "icon", src=league["logo"])
        gen_channels.append(ch)
        if ev:
            sep = "vs" if ev["neutral"] else "@"
            e["name"] = f"{label}: {ev['away']['short']} {sep} {ev['home']['short']}{suffix}"
            gen_programmes += event_programmes(cid, ev, league, win_start, win_end)
            status = f"{ev['away']['short']} {sep} {ev['home']['short']} ({fmt_et(ev['start'])})"
        else:
            kind, cleaned = slot_status(raw, now)
            if kind == "named":
                e["name"] = f"{label}: {cleaned}{suffix}"
                gen_programmes += filler_programmes(cid, cleaned, raw, league, win_start, win_end)
                status = f"unmatched -> '{cleaned}'"
            else:
                e["name"] = f"{label}: No Game Today{suffix}"
                gen_programmes += filler_programmes(
                    cid, "No Game Today", "No game is assigned to this channel right now.", league, win_start, win_end)
                status = kind
        e["attrs"]["tvg-name"] = e["name"]
        report.append(dict(league=league["key"], slot=s["slot"], provider_name=raw, result=status))
        log(f"  {label:>10}{suffix:<9} | {raw[:60]:<60} | {status}")

    # --- write playlist
    head = "#EXTM3U"
    if EPG_URL:
        head += f' url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'
    m3u_text = "\n".join([head] + [entry_to_text(e) for e in kept]) + "\n"
    with open(os.path.join(OUT_DIR, "playlist.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u_text)

    # --- merge EPG
    wanted = {e["attrs"].get("tvg-id") for e in kept} - {s["cid"] for s in slots}
    log("Downloading upstream EPG ...")
    epg_raw = fetch(UPSTREAM_EPG, timeout=300)
    if epg_raw[:2] == b"\x1f\x8b":
        epg_raw = gzip.decompress(epg_raw)
    tmp = os.path.join(OUT_DIR, "epg.xml.gz.tmp")
    n_ch = n_pr = 0
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="personal-epg-mirror">\n')
        wrote_ours = False

        def write_ours_channels():
            for ch in gen_channels:
                ch.tail = "\n"
                out.write(ET.tostring(ch, encoding="unicode"))

        root = None
        for event, el in ET.iterparse(io.BytesIO(epg_raw), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                continue
            if el.tag == "channel" and el.get("id") in wanted:
                el.tail = "\n"
                out.write(ET.tostring(el, encoding="unicode"))
                n_ch += 1
            elif el.tag == "programme":
                if not wrote_ours:
                    write_ours_channels()
                    wrote_ours = True
                if el.get("channel") in wanted:
                    el.tail = "\n"
                    out.write(ET.tostring(el, encoding="unicode"))
                    n_pr += 1
            else:
                continue
            root.clear()
        if not wrote_ours:
            write_ours_channels()
        for p in gen_programmes:
            p.tail = "\n"
            out.write(ET.tostring(p, encoding="unicode"))
        out.write("</tv>\n")
    os.replace(tmp, os.path.join(OUT_DIR, "epg.xml.gz"))
    log(f"EPG: {n_ch} upstream channels, {n_pr} upstream programmes, "
        f"{len(gen_channels)} sports slots, {len(gen_programmes)} generated programmes")

    with open(os.path.join(OUT_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(dict(built=now.isoformat(timespec="seconds"), entries=len(kept),
                       playlist_sha1=hashlib.sha1(m3u_text.encode()).hexdigest(), slots=report),
                  f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(secret_free(f"build failed: {e!r}"))
