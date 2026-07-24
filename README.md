# EQ Log Suite

[![GitHub repo](https://img.shields.io/badge/GitHub-eq--log--suite-blue?logo=github)](https://github.com/mdjthesecond-droid/eq-log-suite)

Personal EverQuest-family log parser: full combat breakdown into MySQL/MariaDB,
browsable/filterable through a web UI (raw SQL or point-and-click), a live
in-game overlay, and user-editable alert rules. Multi-game by design -- one
small parser plugin per game feeds a common schema, so the UI never changes.

Currently supports:
- **eql** -- EverQuest Legends (classic EQ log format)
- **eq2** -- EverQuest II

## One-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/local.example.yaml config/local.yaml
# edit config/local.yaml with the real eqlogs MySQL password

# needs MariaDB initialized + running first, see below
.venv/bin/mariadb -u eqlogs -p eqlogs < eq_log_suite/schema.sql
```

If MariaDB isn't set up yet on this machine:

```bash
sudo mariadb-install-db --user=mysql --datadir=/var/lib/mysql
sudo systemctl enable --now mariadb
sudo mariadb -e "CREATE DATABASE eqlogs; CREATE USER 'eqlogs'@'localhost' IDENTIFIED BY 'yourpassword'; GRANT ALL PRIVILEGES ON eqlogs.* TO 'eqlogs'@'localhost'; FLUSH PRIVILEGES;"
```

## Everyday use -- desktop launchers

Once set up, you don't need a terminal or this venv activated by hand. Four
entries are installed in your application launcher (search "EQ Log Suite" in
the KDE app menu, or use the icons directly):

- **EQ Log Suite - Tailer** -- starts just the live tailer. This is the
  normal one to click before you play.
- **EQ Log Suite - Web UI** -- starts the web server (if not already running)
  and opens it in your browser at http://localhost:8000. This is where you
  actually watch things live (`/live`, `/zones`, `/gathering`) -- see below
  for why.
- **EQ Log Suite - Overlay** -- starts the tailer plus the in-game overlay
  window. Not part of normal play right now: KWin gives a focused fullscreen
  game its own stacking layer above ordinary "always on top" windows, so the
  overlay only reliably renders when the game isn't fullscreen-focused (menus,
  loading screens, or if you ever switch a game to true windowed mode).
  Confirmed this isn't a simple KWin setting -- `WindowsBlockCompositing` is
  already `false` here, so the compositor isn't fully suspending for
  fullscreen; it's KWin's fullscreen stacking layer specifically. The real
  fix (below) is graphics-API-level overlay injection, same as MangoHud/
  Steam's own overlay.
- **EQ Log Suite - Stop** -- stops the tailer, overlay, MangoHud alert
  writer, and web UI, whichever of them are running.

They're backed by plain shell scripts in `bin/` (`start-tailer.sh`,
`start-overlay.sh`, `start-mangohud-alerts.sh`, `start-web.sh`, `stop-all.sh`)
if you'd rather run them from a terminal, or want to see what they're doing.
All are safe to re-run -- they check what's already running before starting
anything new. Logs from each go to `logs/tailer.log`, `logs/overlay.log`,
`logs/mangohud_alerts.log`, `logs/web.log`.

The desktop entries live in `~/.local/share/applications/eq-log-suite-*.desktop`
and point at the scripts in this project's `bin/` directory by absolute path,
so they'll keep working as long as this project stays at
`/var/home/myself/claudes/eq-log-suite/`.

## Alerts over fullscreen (MangoHud)

The GTK overlay above doesn't reliably render over a focused fullscreen
game (see its note above); MangoHud does, because it draws inside the
game's own Vulkan/GL swapchain rather than being a window KWin has to
stack. Confirmed working on this machine against EQ2 running through Proton
via a Steam non-Steam-game shortcut.

Setup (one-time):

1. Bazzite already ships MangoHud -- nothing to install.
2. Right-click the EQ2 shortcut in Steam -> Properties -> Launch Options:
   ```
   MANGOHUD_CONFIGFILE=/home/myself/.config/MangoHud/eq_log_suite.conf mangohud %command%
   ```
   Using a dedicated config file (not `~/.config/MangoHud/MangoHud.conf`)
   keeps this from touching whatever MangoHud settings you use for other
   games.
3. Create that config file with two `exec`/`custom_text` pairs -- one for a
   live DPS readout, one for alerts:
   ```
   position=top-left
   exec=cat /var/home/myself/claudes/eq-log-suite/logs/mangohud_dps.txt
   custom_text=DPS
   exec=cat /var/home/myself/claudes/eq-log-suite/logs/mangohud_alert.txt
   custom_text=Alert
   ```
   `exec`'s output is the value shown; `custom_text` is just its label --
   the two must be paired like this, `custom_text` alone doesn't render
   anything (learned the hard way -- a bare `custom_text` with no `exec`
   next to it never appears on screen, regardless of position or preset).
   `custom_text_center` was tried for the alert slot (hoping for a
   screen-centered alert, separate from the DPS line) but it doesn't pair
   with a preceding `exec` the way plain `custom_text` does -- it only ever
   renders its own literal configured string, centered and disconnected
   from any exec output next to it. So both DPS and Alert use plain
   `custom_text` and sit in the same row for now.

   MangoHud polls each `exec` command on its own and re-renders on change,
   no reload keypress needed -- confirmed by editing the file live and
   watching it update in-game. Also confirmed safe to add MangoHud's own
   layout/color/font options (`horizontal`, `table_columns`, `*_color`,
   `font_size`, etc., e.g. via the MangoJuice GUI) into this same file
   alongside the two `exec`/`custom_text` pairs above -- just watch out
   that GUI config tools tend to rewrite the *entire* file when you save
   from them, silently dropping the `exec=` lines since they're not exposed
   in the GUI. Re-add the two pairs above after any GUI-driven edit.

Everyday use: **EQ Log Suite - MangoHud Alerts** (or `bin/start-mangohud-alerts.sh`)
instead of the regular Overlay launcher. It starts the tailer (if not
already running) plus `eq_log_suite/overlay/mangohud_writer.py`, which
listens on the same alert-broadcast socket the GTK overlay uses and writes:
- the live DPS/hit%/crit% snapshot to `logs/mangohud_dps.txt`, refreshed
  every 0.5s while active and frozen (not cleared) once combat pauses, same
  behavior as the GTK overlay's own DPS meter;
- whichever alert most recently fired to `logs/mangohud_alert.txt`,
  clearing it again once that alert's configured duration has passed. This
  includes a built-in in-combat/out-of-combat alert (`tailer.py`'s
  `CombatTracker`) showing how many distinct mob *names* you're currently
  trading hits with -- note "names," not instances: the log format has no
  per-mob ID, so two simultaneously-engaged mobs sharing an identical
  generic name (e.g. two "a lowland viper") collapse into one count.

Known limitation: MangoHud only gives you a couple of fixed exec/custom_text
slots, not a real queue -- so unlike the GTK overlay's stacked,
per-message-colored annotations, this shows one line per slot: whatever
fired most recently. Fine for "something just happened," not a substitute
for the richer overlay if you're ever in windowed mode.

MariaDB itself is a systemd service (`sudo systemctl enable --now mariadb`
was run during setup), so it starts automatically on boot -- nothing to
launch for that.

## New/rotated log files (new character, new server, EQ2 `/log` toggles)

This is automatic -- nothing to do manually. The tailer scans the folders
listed under `log_roots` in `config/local.yaml` (defaults: the EQL `Logs/`
folder, and the EQ2 `logs/` folder recursively, since EQ2 organizes it by
server-named subfolder) once at startup and every 5 minutes while it runs.
Any file not already tracked gets imported and considered for live-tailing
automatically (see `eq_log_suite/discovery.py`).

Only one file is kept "live" per character at a time, resolved by which of
that character's known files has the most recent modification time -- i.e.
whichever one the game is actually writing to right now. This handles EQ2
creating a fresh file on a `/log` toggle (its default un-named log file is
`eq2log_<Character>.txt`, same idea as EQL's `eqlog_<Character>_<Server>.txt`)
without ending up tailing a stale abandoned file. Different servers are
always separate characters (and so separately live) even under the same
character name, since server-specific rulesets can affect both combat and
gathering.

To import a file without waiting for the next scan (or to bulk-backfill
history without tailing it), the manual path still works:

```bash
.venv/bin/python -m eq_log_suite.importer <path-to-log-file> --live   # omit --live to backfill only
```

Both auto-detect which game a file is from.

## Re-baselining gathering data (e.g. after a patch)

Drop rates/yields can change with a game patch, which would otherwise make
old gathering stats and new ones misleading if mixed together. Nothing
gathering-related ever needs to be deleted to handle this -- `/gathering`
uses "eras" (`gather_eras` table): a timestamp boundary, not a data
partition.

- **Start a new era** (closes the current one, starts a fresh one) --
  either the "Start a new era" form on `/gathering` itself, or:
  ```bash
  .venv/bin/python -m eq_log_suite.gather_eras new "Post Patch 7.2" --note "patch notes mentioned mining rates"
  .venv/bin/python -m eq_log_suite.gather_eras list
  ```
  `/gathering` defaults to showing only the active era; pick a past era (or
  "all eras") from its dropdown to see/compare older data. Since a gather
  event is bucketed by its own in-game timestamp (not by file or import
  time), this works correctly even for a log file that straddles the patch
  moment -- no need to import a specific "post-patch" file separately.

- **Hard reset instead** (actually wipes gather data, only if you really
  want a from-scratch rebuild rather than a new era): null out the
  `raw_lines` back-references, delete the events, then re-derive them from
  the same raw lines already on disk in the DB -- no need to re-read the
  actual log files:
  ```bash
  mariadb -u eqlogs -p eqlogs -e "
    UPDATE raw_lines SET event_id = NULL WHERE event_id IN (SELECT id FROM (SELECT id FROM events WHERE event_type IN ('gather','rare_found')) x);
    DELETE FROM events WHERE event_type IN ('gather','rare_found');
  "
  .venv/bin/python -m eq_log_suite.backfill --all
  ```
  This is what building the current `gather_eras` baseline started with.

## Extending to a new game

1. Get a real log sample (enable in-game logging, play a bit).
2. Add `eq_log_suite/parsers/<game>.py`: a `GameParser` subclass with
   `game_code`, optionally a custom `TIMESTAMP_RE`/`TIMESTAMP_FMT`, and
   `PATTERNS` -- an ordered list of `(regex, handler)` pairs. Look at
   `eq_legends.py` and `eq2.py` for the pattern: scan the real log for
   distinct line shapes, write a regex + handler per shape, and don't worry
   about 100% coverage -- unmatched lines still land in `raw_lines`.
3. Register it in `eq_log_suite/parsers/registry.py`.
4. Everything else (schema, importer, tailer, web UI, overlay, alerts) works
   unchanged.
