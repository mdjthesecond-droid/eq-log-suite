-- EverQuest Log Suite schema. Run once against the `eqlogs` database:
--   mariadb -u eqlogs -p eqlogs < eq_log_suite/schema.sql

CREATE TABLE IF NOT EXISTS games (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(16) NOT NULL UNIQUE,   -- 'eql', 'eq', ...
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

-- User-curated NPC type (log text can strongly suggest "vendor" via
-- vendor_buy/vendor_sell events, or "mob" via classify_actor's
-- article-prefix heuristic, but can't say "class trainer" or otherwise
-- distinguish a unique-named NPC from a unique-named mob) -- /npcs shows
-- an auto-suggested type alongside this override.
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

-- Zone correlation (used by /loot, /quests) works by finding the
-- most recent zone_change logged before an event's own ts -- but a
-- character's very first bit of activity, before their first zone_change
-- was ever logged (log file started mid-session, or /log was toggled on
-- after already zoning in), has no such row to find, and shows up as
-- "(unknown zone)". The log itself has no way to recover that -- this is a
-- one-time, per-character, user-confirmed answer to "where was I before the
-- log starts". Only ever fills in that one leading gap; once a character
-- has any real zone_change logged, everything after it resolves normally
-- without needing an entry here.
CREATE TABLE IF NOT EXISTS zone_start_overrides (
    character_id INT NOT NULL PRIMARY KEY,
    zone VARCHAR(255) NOT NULL,
    note VARCHAR(255) NULL,
    confirmed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (character_id) REFERENCES characters(id)
) ENGINE=InnoDB;

