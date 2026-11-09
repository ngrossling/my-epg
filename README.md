# Personal EPG mirror (Ganja Release)

Every 30 minutes GitHub Actions:

1. Downloads the Ganja Release playlist (your EPGenius Google Drive link) and his EPG (`epg6.xml.gz`).
   New categories and channels he adds show up here automatically.
2. Drops the **EPGenius** category. Every other category is kept as-is.
3. Rebuilds **NFL Sunday Ticket**, **College Football**, **NBA League Pass** and **College Basketball**
   game slots itself: it reads what each provider slot is showing right now (Xtream API) and matches
   it to the ESPN schedule. Each slot gets a permanent guide ID (`slot.nfl.<stream id>`), so new games
   appear after a guide refresh with no playlist reload. College Basketball keeps Ganja's hand-picked
   games and adds all ~100 provider `NCAAB` slots (men's and women's).
4. Remembers which game each slot carried (`state.json`), so a finished game stays in the guide with
   its final score for 24 hours even after the provider clears the slot. Then the slot shows
   "No Game Today", or the slot's next game once it's within 12 hours.
5. Publishes:
   * `epg.xml.gz` → this repo's `output` branch (no credentials in it)
   * `playlist.m3u` → a **secret gist** (it contains your Xtream login)

If any download fails, the run fails and the last good files stay up.

## One-time setup

1. **Create the repo.** Make a new **public** repo on GitHub (for example `my-epg`) and upload
   `build.py`, `README.md`, `.gitignore` and `.github/workflows/update.yml`.
   It is public so Actions minutes are free and the EPG raw URL works without a login.
   No secrets are ever committed.
2. **Create a secret gist.** Go to <https://gist.github.com>, filename `playlist.m3u`, content `#EXTM3U`,
   then **Create secret gist**. Copy the ID from the URL: `gist.github.com/<you>/<GIST_ID>`.
3. **Create a token for the gist.** GitHub → Settings → Developer settings → Personal access tokens →
   **Tokens (classic)** → scope **gist** only, no expiration.
4. **Add repo secrets.** Repo → Settings → Secrets and variables → Actions → *New repository secret*:

   | Secret | Value |
   |---|---|
   | `UPSTREAM_M3U` | your EPGenius Drive link (`https://drive.usercontent.google.com/download?id=...&confirm=t`) |
   | `GIST_ID` | the gist ID from step 2 |
   | `GIST_TOKEN` | the token from step 3 |
   | `XTREAM_HOST` *(optional)* | your Strong8K server, e.g. `http://server.example:80` (everything before `/live/` in a stream link) |
   | `XTREAM_USER` *(optional)* | your Xtream username |
   | `XTREAM_PASS` *(optional)* | your Xtream password |

   The three `XTREAM_*` secrets are only needed if you want to override the login that is already
   embedded in your EPGenius playlist (for example after changing your password).

5. **First run.** Actions tab → *Update EPG* → **Run workflow**. Once it's green you're done.

## URLs for your player (TiviMate, IPTV Smarters, etc.)

* Playlist: `https://gist.githubusercontent.com/<you>/<GIST_ID>/raw/playlist.m3u`
* EPG: `https://raw.githubusercontent.com/<you>/my-epg/output/epg.xml.gz`

The playlist already points at the EPG, but set the EPG URL in your player too.
Set the EPG to refresh every 1–2 hours and the playlist every 12–24 hours.

Each run's slot matches are in `status.json` on the `output` branch.

## Options

* `SPOILERS: "1"` in the workflow (on): live scores while a game is on (as of the last run, so up to
  30 min old plus however often your player refreshes the EPG), and `Final: Away 24, Home 17` after.
  Set it to `"0"` for spoiler-free titles.
* `POST_HOURS` / `PRE_HOURS` in `build.py`: how long a final stays up (24) and how early the
  upcoming entry starts (12).
* To add another league later (NHL, MLB, WNBA), add an entry to `LEAGUES` in
  `build.py` with its playlist group name and ESPN path. Until then those groups use Ganja's EPG.
