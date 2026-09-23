-- Collapse instance-scoped observations to the latest state per logical strategy.
CREATE TEMP TABLE notification_state_rekey ON COMMIT DROP AS
SELECT states.state_key AS old_key,
       'strategy-health:' || runtime.account_id || ':' || runtime.strategy_id
           AS new_key,
       ROW_NUMBER() OVER (
           PARTITION BY runtime.account_id, runtime.strategy_id
           ORDER BY states.updated_at DESC, states.state_key DESC
       ) AS position
FROM notification_source_states AS states
JOIN strategy_runtime_status AS runtime
  ON LEFT(
       states.state_key,
       LENGTH('strategy-health:' || runtime.account_id || ':' || runtime.strategy_id || ':')
     ) = 'strategy-health:' || runtime.account_id || ':' || runtime.strategy_id || ':';

DELETE FROM notification_source_states AS states
USING notification_state_rekey AS rekey
WHERE states.state_key = rekey.old_key
  AND rekey.position > 1;

UPDATE notification_source_states AS states
SET state_key = rekey.new_key
FROM notification_state_rekey AS rekey
WHERE states.state_key = rekey.old_key
  AND rekey.position = 1;

TRUNCATE notification_state_rekey;

INSERT INTO notification_state_rekey (old_key, new_key, position)
SELECT states.state_key AS old_key,
       'strategy-risk:' || runtime.account_id || ':' || runtime.strategy_id
           AS new_key,
       ROW_NUMBER() OVER (
           PARTITION BY runtime.account_id, runtime.strategy_id
           ORDER BY states.updated_at DESC, states.state_key DESC
       ) AS position
FROM notification_source_states AS states
JOIN strategy_runtime_status AS runtime
  ON LEFT(
       states.state_key,
       LENGTH('strategy-risk:' || runtime.account_id || ':' || runtime.strategy_id || ':')
     ) = 'strategy-risk:' || runtime.account_id || ':' || runtime.strategy_id || ':';

DELETE FROM notification_source_states AS states
USING notification_state_rekey AS rekey
WHERE states.state_key = rekey.old_key
  AND rekey.position > 1;

UPDATE notification_source_states AS states
SET state_key = rekey.new_key
FROM notification_state_rekey AS rekey
WHERE states.state_key = rekey.old_key
  AND rekey.position = 1;
