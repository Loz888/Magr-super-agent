-- Proposal only: requires exact Harness migration ownership confirmation.
-- Preserve all old authorizations/ledgers; allow a newly signed USD10 trial.
ALTER TABLE app.sa_coding_trial_authorizations
    DROP CONSTRAINT sa_coding_trial_authorizations_maximum_project_micros_check;
ALTER TABLE app.sa_coding_trial_authorizations
    ADD CONSTRAINT sa_coding_trial_authorizations_maximum_project_micros_check
    CHECK(maximum_project_micros IN (2000000,10000000));

-- Change one exact predicate, preserving every other 0054 binding guard byte.
DO $migration$
DECLARE
    original text := pg_get_functiondef('app.sa_coding_binding_guard()'::regprocedure);
    old_predicate text := 'AND trial.maximum_project_micros=2000000';
BEGIN
    IF (length(original)-length(replace(original,old_predicate,'')))
        /length(old_predicate) <> 1 THEN
        RAISE EXCEPTION 'expected exactly one old SA22 trial ceiling predicate';
    END IF;
    EXECUTE replace(original,old_predicate,
        'AND trial.maximum_project_micros IN (2000000,10000000)');
END
$migration$;
