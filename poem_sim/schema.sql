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
