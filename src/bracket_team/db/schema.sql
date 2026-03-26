-- bracket_team database schema

CREATE TABLE IF NOT EXISTS brackets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    tournament_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matchups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id INTEGER NOT NULL REFERENCES brackets(id),
    run_id INTEGER REFERENCES runs(id),              -- NULL for Round 1 (shared); set for Round 2+ (run-specific)
    round_num INTEGER NOT NULL,          -- 1=First Round, 6=Championship
    region TEXT NOT NULL,
    favorite_name TEXT NOT NULL,
    favorite_seed INTEGER NOT NULL,
    underdog_name TEXT NOT NULL,
    underdog_seed INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id INTEGER NOT NULL REFERENCES brackets(id),
    name TEXT NOT NULL,
    risk_appetite TEXT NOT NULL DEFAULT 'neutral',  -- conservative, neutral, aggressive
    analyst_weights TEXT NOT NULL,       -- JSON blob
    user_preferences TEXT,                    -- free-form user input for manager
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, running, completed, error
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    error_message TEXT,
    progress_info TEXT                       -- JSON: {"teams":"A vs B","phase":"research"} while running
);

CREATE TABLE IF NOT EXISTS analyst_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    matchup_id INTEGER NOT NULL REFERENCES matchups(id),
    analyst_role TEXT NOT NULL,          -- sports_analyst, odds_analyst, etc.
    pick TEXT NOT NULL,                  -- favorite, underdog
    score INTEGER NOT NULL,              -- -5 to +5
    relevance TEXT NOT NULL,             -- low, medium, high
    thesis TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS discussion_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    matchup_id INTEGER NOT NULL REFERENCES matchups(id),
    phase TEXT NOT NULL,                 -- challenge, rebuttal
    author_role TEXT NOT NULL,
    target_role TEXT,                    -- NULL for rebuttals
    steelman TEXT,                       -- steelman_against_own_pick (challenges only)
    content TEXT NOT NULL,               -- challenge text or rebuttal text
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    matchup_id INTEGER NOT NULL REFERENCES matchups(id),
    predicted_winner TEXT NOT NULL,
    outcome_type TEXT NOT NULL,          -- expected, upset
    weighted_score REAL NOT NULL,
    confidence TEXT NOT NULL,            -- high, medium, low
    synthesis TEXT NOT NULL,
    manager_model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',  -- completed, error
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    matchup_id INTEGER,                  -- NULL for bracket-level calls
    agent_role TEXT NOT NULL,
    model TEXT NOT NULL,
    phase TEXT NOT NULL,                 -- research, challenge, rebuttal, decision
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS team_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id INTEGER NOT NULL REFERENCES brackets(id),
    team_name TEXT NOT NULL,
    season_wins INTEGER, season_losses INTEGER,
    conf_wins INTEGER, conf_losses INTEGER, conference TEXT,
    sos_rank INTEGER, net_rank INTEGER, srs REAL,
    adj_off_eff REAL, adj_def_eff REAL, pace REAL,
    fg_pct REAL, three_pt_pct REAL,
    conf_tourney_wins INTEGER DEFAULT 0, conf_tourney_losses INTEGER DEFAULT 0,
    head_coach TEXT, coach_tourney_appearances INTEGER, coach_tourney_record TEXT,
    freshmen_count INTEGER, senior_count INTEGER, transfer_count INTEGER,
    -- Additional Sports Reference stats (same page fetches, extra columns)
    ft_rate REAL,                                     -- free throw attempt rate (FTA/FGA)
    ft_pct REAL,                                      -- free throw shooting accuracy
    tov_pct REAL,                                     -- turnover percentage
    ppg REAL,                                         -- points per game
    opp_ppg REAL,                                     -- opponent points per game (points allowed)
    orb_per_g REAL,                                   -- offensive rebounds per game
    drb_per_g REAL,                                   -- defensive rebounds per game
    ast_per_g REAL,                                   -- assists per game
    stl_per_g REAL,                                   -- steals per game
    blk_per_g REAL,                                   -- blocks per game
    opp_fg_pct REAL,                                  -- opponent field goal percentage
    opp_three_pt_pct REAL,                            -- opponent 3-point percentage
    opp_tov_per_g REAL,                               -- opponent turnovers forced per game
    ap_rank INTEGER,                                  -- final AP poll ranking (NULL if unranked)
    neutral_wins INTEGER DEFAULT 0,                   -- neutral site record
    neutral_losses INTEGER DEFAULT 0,
    last10_wins INTEGER,                              -- last 10 regular season games
    last10_losses INTEGER,
    -- BartTorvik / T-Rank metrics
    bart_adj_oe REAL,                                 -- BartTorvik adj. offensive efficiency
    bart_adj_de REAL,                                 -- BartTorvik adj. defensive efficiency
    barthag REAL,                                     -- Pythagorean win probability
    bart_tempo REAL,                                  -- adjusted tempo
    bart_luck REAL,                                   -- luck rating (record vs Pythagorean expectation)
    bart_wab REAL,                                    -- wins above bubble
    quad1_wins INTEGER DEFAULT 0,                     -- Quad 1 record
    quad1_losses INTEGER DEFAULT 0,
    quad2_wins INTEGER DEFAULT 0,                     -- Quad 2 record
    quad2_losses INTEGER DEFAULT 0,
    quad3_wins INTEGER DEFAULT 0,                     -- Quad 3 record
    quad3_losses INTEGER DEFAULT 0,
    quad4_wins INTEGER DEFAULT 0,                     -- Quad 4 record
    quad4_losses INTEGER DEFAULT 0,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(bracket_id, team_name)
);

CREATE TABLE IF NOT EXISTS team_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id INTEGER NOT NULL REFERENCES brackets(id),
    matchup_id INTEGER NOT NULL REFERENCES matchups(id),
    favorite_name TEXT NOT NULL, underdog_name TEXT NOT NULL,
    spread REAL, favorite_ml INTEGER, underdog_ml INTEGER, over_under REAL,
    implied_fav_win_pct REAL, implied_dog_win_pct REAL,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(matchup_id)
);

CREATE TABLE IF NOT EXISTS seed_matchup_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    favorite_seed INTEGER NOT NULL, underdog_seed INTEGER NOT NULL,
    total_games INTEGER NOT NULL,
    favorite_wins INTEGER NOT NULL, underdog_wins INTEGER NOT NULL,
    favorite_win_pct REAL NOT NULL, upset_rate_pct REAL NOT NULL,
    notable_pattern TEXT,
    UNIQUE(favorite_seed, underdog_seed)
);

CREATE TABLE IF NOT EXISTS team_player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id INTEGER NOT NULL REFERENCES brackets(id),
    team_name TEXT NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT,
    class_year TEXT,
    ppg REAL,
    rpg REAL,
    apg REAL,
    mpg REAL,
    ft_pct REAL,
    three_pt_pct REAL,
    usage_rate REAL,
    injured BOOLEAN DEFAULT FALSE,
    injury_note TEXT,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(bracket_id, team_name, player_name)
);

CREATE TABLE IF NOT EXISTS config_overrides (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Migrations: new columns added to existing team_stats table.
-- These are executed individually with error handling in init_db().
-- ALTER TABLE team_stats ADD COLUMN ft_rate REAL;
-- ALTER TABLE team_stats ADD COLUMN tov_pct REAL;
-- ALTER TABLE team_stats ADD COLUMN neutral_wins INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN neutral_losses INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN last10_wins INTEGER;
-- ALTER TABLE team_stats ADD COLUMN last10_losses INTEGER;
-- ALTER TABLE team_stats ADD COLUMN bart_adj_oe REAL;
-- ALTER TABLE team_stats ADD COLUMN bart_adj_de REAL;
-- ALTER TABLE team_stats ADD COLUMN barthag REAL;
-- ALTER TABLE team_stats ADD COLUMN bart_tempo REAL;
-- ALTER TABLE team_stats ADD COLUMN bart_luck REAL;
-- ALTER TABLE team_stats ADD COLUMN bart_wab REAL;
-- ALTER TABLE team_stats ADD COLUMN quad1_wins INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad1_losses INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad2_wins INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad2_losses INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad3_wins INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad3_losses INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad4_wins INTEGER DEFAULT 0;
-- ALTER TABLE team_stats ADD COLUMN quad4_losses INTEGER DEFAULT 0;
-- ALTER TABLE team_player_stats ADD COLUMN ft_pct REAL;
-- ALTER TABLE team_player_stats ADD COLUMN three_pt_pct REAL;
-- ALTER TABLE team_player_stats ADD COLUMN usage_rate REAL;
