-- Poem server schema — Design Doc Rev 3 (§14)
-- All law edits live on the server; time-of-day is audit-only.

CREATE TABLE IF NOT EXISTS law_revisions (
    id UUID PRIMARY KEY,
    law_id UUID NOT NULL,
    civilization_id UUID NOT NULL,
    action TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    canonical_change JSONB NOT NULL,
    sequence_number BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,   -- AUDIT ONLY: never a gameplay modifier
    simulation_turn_id UUID,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_law_revisions_pending
    ON law_revisions (law_id, status, sequence_number);

CREATE TABLE IF NOT EXISTS law_drafts (
    id UUID PRIMARY KEY,
    civilization_id UUID NOT NULL,
    target_law_id UUID,
    raw_text TEXT NOT NULL,
    canonical_draft JSONB,
    status TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS world_chronologies (
    world_id UUID PRIMARY KEY,
    game_speed TEXT NOT NULL,
    era_source TEXT NOT NULL,
    current_turn INTEGER NOT NULL,
    current_year DOUBLE PRECISION NOT NULL,
    current_season TEXT NOT NULL,
    current_era TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_interval_rules (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    era TEXT NOT NULL,
    years_per_turn DOUBLE PRECISION NOT NULL,
    speed TEXT NOT NULL
);

-- Treaties / transitions / travel: remaining time in GAME units, not turns.
CREATE TABLE IF NOT EXISTS timed_effects (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    civilization_id UUID,
    kind TEXT NOT NULL,
    remaining_years DOUBLE PRECISION NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    number INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    simulation_cutoff_at TIMESTAMPTZ NOT NULL,
    finalized_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    seed BIGINT NOT NULL,
    turn_span_years DOUBLE PRECISION NOT NULL,
    world_year_before DOUBLE PRECISION NOT NULL,
    world_year_after DOUBLE PRECISION NOT NULL,
    era_before TEXT NOT NULL,
    era_after TEXT NOT NULL
);

-- Folklore corpus (§11.5): spread & mutation scale with GAME time only.
CREATE TABLE IF NOT EXISTS folklore_items (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    civilization_id UUID NOT NULL,
    title TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    spread DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    mutation DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    oral_strength DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    distance_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    conflict_factor DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

-- Prejudices (§13 step 15): long-lived social biases; growth/decay are
-- annual rates applied over the turn's GAME-time span (§20.9), never a
-- function of real-world time of day (§20.4).
CREATE TABLE IF NOT EXISTS prejudices (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    civilization_id UUID NOT NULL,
    title TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    intensity DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    annual_growth DOUBLE PRECISION NOT NULL DEFAULT 0.004,
    annual_decay DOUBLE PRECISION NOT NULL DEFAULT 0.002,
    resolved BOOLEAN NOT NULL DEFAULT FALSE
);

-- Wars (§13 step 17): conflicts measured in GAME time. Fatigue accumulates
-- per game-year (with diminishing gains, §19.1) and is NOT a function of
-- real-world time of day (§20.4). A war that exhausts both sides ends and
-- produces a peace treaty stored in timed_effects with remaining_years.
CREATE TABLE IF NOT EXISTS wars (
    id UUID PRIMARY KEY,
    world_id UUID NOT NULL,
    attacker_id UUID NOT NULL,
    defender_id UUID NOT NULL,
    started_year DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    fatigue_attacker DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    fatigue_defender DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    annual_fatigue_rate DOUBLE PRECISION NOT NULL DEFAULT 0.01
);
