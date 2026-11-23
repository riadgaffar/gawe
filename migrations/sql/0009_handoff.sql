-- M20: action classes the engine only prepares. A prepared step waits for a person, who marks it
-- done or not done; the run waits with it. `handoff` records the role and what the person said.
ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'created', 'planning', 'running', 'paused_for_approval', 'waiting_verification',
    'waiting_for_person', 'completed', 'failed', 'cancelled', 'denied', 'unknown'));
ALTER TABLE steps DROP CONSTRAINT steps_status_check;
ALTER TABLE steps ADD CONSTRAINT steps_status_check CHECK (status IN (
    'pending', 'ready', 'policy_checked', 'awaiting_approval', 'approved',
    'rejected', 'denied', 'executing', 'verifying', 'awaiting_readback',
    'verification_blocked', 'verification_unknown', 'succeeded', 'failed',
    'prepared', 'done_by_person', 'not_done_by_person'));
ALTER TABLE steps ADD COLUMN handoff jsonb
    CHECK (handoff IS NULL OR jsonb_typeof(handoff) = 'object');
-- A hand-off state needs its hand-off record, and only those states may carry one.
ALTER TABLE steps ADD CONSTRAINT steps_handoff_state_check CHECK (
    (status IN ('prepared', 'done_by_person', 'not_done_by_person')) = (handoff IS NOT NULL)
    OR (status = 'failed' AND handoff IS NOT NULL));
