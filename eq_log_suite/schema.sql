-- EverQuest Log Suite schema. Run once against the `eqlogs` database:
--   mariadb -u eqlogs -p eqlogs < eq_log_suite/schema.sql

CREATE TABLE IF NOT EXISTS games (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(16) NOT NULL UNIQUE,   -- 'eql', 'eq2', ...
    name VARCHAR(64) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS characters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    game_id INT NOT NULL,
    name VARCHAR(64) NOT NULL,
    server VARCHAR(64) NULL,
    FOREIGN KEY (game_id) REFERENCES games(id),
    UNIQUE KEY uniq_char (game_id, name, server)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS log_sources (
    id INT AUTO_INCREMENT PRIMARY KEY,
    game_id INT NOT NULL,
    character_id INT NOT NULL,
    file_path VARCHAR(1024) NOT NULL,
    last_byte_offset BIGINT NOT NULL DEFAULT 0,
    last_parsed_at DATETIME NULL,
    live BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    UNIQUE KEY uniq_path (file_path(768))
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    game_id INT NOT NULL,
    character_id INT NOT NULL,
    log_source_id INT NOT NULL,
    ts DATETIME(3) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    source_name VARCHAR(128) NULL,
    source_type VARCHAR(16) NULL,
    target_name VARCHAR(128) NULL,
    target_type VARCHAR(16) NULL,
    verb VARCHAR(128) NULL,
    amount INT NULL,
    outcome VARCHAR(16) NULL,
    extra JSON NULL,
    raw_line TEXT NOT NULL,
    line_no INT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    FOREIGN KEY (log_source_id) REFERENCES log_sources(id),
    INDEX idx_game_ts (game_id, ts),
    INDEX idx_char_ts (character_id, ts),
    INDEX idx_event_type (event_type),
    INDEX idx_source_name (source_name),
    INDEX idx_target_name (target_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS raw_lines (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_source_id INT NOT NULL,
    line_no INT NOT NULL,
    ts DATETIME(3) NULL,
    raw_text TEXT NOT NULL,
    event_id BIGINT NULL,
    FOREIGN KEY (log_source_id) REFERENCES log_sources(id),
    FOREIGN KEY (event_id) REFERENCES events(id),
    INDEX idx_log_source_line (log_source_id, line_no)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS alert_rules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    game_id INT NULL,                          -- NULL = applies to all games
    name VARCHAR(128) NOT NULL,
    description VARCHAR(512) NULL,
    match_type ENUM('regex', 'field_condition') NOT NULL,
    pattern TEXT NOT NULL,
    reaction_types SET('notify', 'sound', 'overlay', 'log') NOT NULL DEFAULT 'log',
    reaction_config JSON NULL,                 -- e.g. {"sound_file": "/path/to.wav"}
    cooldown_seconds INT NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    FOREIGN KEY (game_id) REFERENCES games(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS alert_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    rule_id INT NOT NULL,
    event_id BIGINT NULL,
    ts DATETIME(3) NOT NULL,
    matched_text TEXT NOT NULL,
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id),
    FOREIGN KEY (event_id) REFERENCES events(id),
    INDEX idx_rule_ts (rule_id, ts)
) ENGINE=InnoDB;

-- User-curated node -> tradeskill tier mapping (tier isn't stated anywhere
-- in the log text itself). Assigning a node's tier here is a one-time
-- interaction -- every future pull from that node, and every past one
-- already in `events`, picks it up automatically via a join on node name.
-- /gathering flags any node missing from this table so nothing goes
-- unnoticed. Also incidentally answers "which tier(s) show up in which
-- zone" by joining this against the zone inferred per gather event.
CREATE TABLE IF NOT EXISTS node_tiers (
    node VARCHAR(255) NOT NULL PRIMARY KEY,
    tier VARCHAR(32) NOT NULL,
    note VARCHAR(255) NULL,
    identified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- User-curated NPC type (log text can strongly suggest "vendor" via
-- vendor_buy/vendor_sell events, or "mob" via classify_actor's
-- article-prefix heuristic, but can't say "class trainer" or otherwise
-- distinguish a unique-named NPC from a unique-named mob) -- /npcs shows
-- an auto-suggested type alongside this override, same UX as node_tiers.
CREATE TABLE IF NOT EXISTS npc_info (
    npc VARCHAR(255) NOT NULL PRIMARY KEY,
    npc_type VARCHAR(32) NULL,
    note VARCHAR(255) NULL,
    confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- User-curated zone level-range override, keyed by base_zone (see
-- parse_zone_tier() in eq_legends.py) -- fills in/corrects the level range
-- /zoneinfo otherwise derives from `con` events, for zones with no con data
-- yet or where the user wants to override the observed range.
CREATE TABLE IF NOT EXISTS zone_info (
    zone VARCHAR(255) NOT NULL PRIMARY KEY,
    level_min_override INT NULL,
    level_max_override INT NULL,
    note VARCHAR(255) NULL,
    confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- Zone correlation (used by /loot, /gathering, /quests) works by finding the
-- most recent zone_change logged before an event's own ts -- but a
-- character's very first bit of activity, before their first zone_change
-- was ever logged (log file started mid-session, or /log was toggled on
-- after already zoning in), has no such row to find, and shows up as
-- "(unknown zone)". The log itself has no way to recover that -- this is a
-- one-time, per-character, user-confirmed answer to "where was I before the
-- log starts", same role node_tiers plays for gathering. Only ever fills in
-- that one leading gap; once a character has any real zone_change logged,
-- everything after it resolves normally without needing an entry here.
CREATE TABLE IF NOT EXISTS zone_start_overrides (
    character_id INT NOT NULL PRIMARY KEY,
    zone VARCHAR(255) NOT NULL,
    note VARCHAR(255) NULL,
    confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (character_id) REFERENCES characters(id)
) ENGINE=InnoDB;

-- Gathering "eras": a timestamp boundary, not a data partition. Nothing
-- gathering-related is ever deleted to start a new baseline (e.g. after a
-- patch changes drop rates/yields) -- ending the current era and starting a
-- new one just changes what /gathering shows by default. A gather event
-- "belongs" to whichever era's [started_at, ended_at) window contains its
-- own ts, so this works correctly even for a file that straddles the patch
-- boundary -- old lines fall in the old era, new lines in the new one,
-- based on in-game time, not import time. Exactly one row should have
-- ended_at IS NULL at a time (the active era).
CREATE TABLE IF NOT EXISTS gather_eras (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    started_at DATETIME(3) NOT NULL,
    ended_at DATETIME(3) NULL,
    note VARCHAR(512) NULL
) ENGINE=InnoDB;

-- Inbox for item-window screenshots dropped in the configured
-- item_capture.screenshot_dir (see config/local.yaml) by
-- eq_log_suite.item_capture_watcher: OCR runs automatically on arrival, but
-- the result just lands here as 'pending' -- EQ's stat-window font/layout
-- isn't clean enough to trust unreviewed, so nothing reaches item_info (the
-- actual catalog) without a human confirming/correcting it on /items/review.
CREATE TABLE IF NOT EXISTS item_captures (
    id INT AUTO_INCREMENT PRIMARY KEY,
    screenshot_path VARCHAR(1024) NOT NULL,
    captured_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_ocr_text TEXT NULL,
    parsed_name VARCHAR(255) NULL,
    status ENUM('pending', 'confirmed', 'rejected') NOT NULL DEFAULT 'pending',
    reviewed_at DATETIME NULL,
    UNIQUE KEY uniq_screenshot (screenshot_path(768))
) ENGINE=InnoDB;

-- Curated item catalog -- one row per *confirmed snapshot*, not one row per
-- item (deliberately not keyed/unique on `item`, unlike node_tiers/npc_info/
-- zone_info). Confirmed real (screenshot dated 2026-08-05): items have a
-- Tier field ("Tier3", or "Tier2 0/4" while still progressing toward the
-- next tier) that changes over time as the item is used, plus up to five
-- Exaltation slots (Ornamentation, Focus/Click/Worn/Proc Exaltation) that
-- start as a plain effect line and only become removable as their own
-- augmentation item once Tier is high enough. Overwriting on every confirm
-- (the original design) would silently destroy exactly the tier/exaltation
-- progression this exists to track -- every confirm is its own historical
-- row instead, so /items/history?item=... can show whether e.g. 1/8, 2/8,
-- 3/8 actually changed over real play before any dedup cleanup happens.
-- exaltations stays free text (5 known slots, but not confirmed exhaustive).
-- stats is JSON, not free text -- confirmed real (2026-08-05) that once the
-- capture is a clean single-item screenshot in a monospace font, OCR is
-- reliable enough to auto-extract "Label: Value" pairs (Size/AC/Weight/
-- Strength/SV. Magic/Combat Effect/etc. -- a stable, known label vocabulary
-- across every item shape seen) straight into structured, indexable fields
-- via JSON_EXTRACT instead of a blob the user would have to re-parse by
-- hand later to actually query anything.
CREATE TABLE IF NOT EXISTS item_info (
    id INT AUTO_INCREMENT PRIMARY KEY,
    item VARCHAR(255) NOT NULL,
    tier INT NULL,
    tier_progress VARCHAR(16) NULL,   -- e.g. "0/4", the X/Y next to Tier
    upgradeable BOOLEAN NULL,         -- "This item can/cannot be upgraded"
    exaltations TEXT NULL,            -- Ornamentation/Focus/Click/Worn/Proc Exaltation slots
    stats JSON NULL,                  -- {"Size": "SMALL", "AC": "12", "SV. Magic": "7", ...}
    note VARCHAR(255) NULL,
    screenshot_path VARCHAR(1024) NULL,
    source_capture_id INT NULL,
    confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_capture_id) REFERENCES item_captures(id),
    INDEX idx_item (item)
) ENGINE=InnoDB;
