-- SA18 forward repair PROPOSAL ONLY; 0051 needs fresh Harness confirmation.
-- Never rewrite 0047/0050. This grants metadata RECEIPT only while the original
-- verified Run awaits review; it does not change sa_async_current, Coding,
-- checkpoint/apply, model-payload or Job-start guards. No new tables or routes.

CREATE FUNCTION app.sa_source_review_intake_current(identifier uuid) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (
        SELECT 1 FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id AND c.project_id=b.project_id
        JOIN app.runs r ON r.run_id=b.run_id AND r.run_id=c.run_id
        JOIN app.tickets t ON t.ticket_id=r.ticket_id
        JOIN app.sa_source_reviews review ON review.coding_id=c.coding_id
            AND review.run_id=r.run_id AND review.project_id=c.project_id
        JOIN app.sa_coding_attempts attempt ON attempt.attempt_id=review.attempt_id
        JOIN app.sa_coding_verifications v ON v.verification_id=review.verification_id
        JOIN app.actions action ON action.action_id=review.action_id
        WHERE b.async_id=identifier AND b.state='active' AND b.expires_at>clock_timestamp()
        AND c.state='admitted' AND c.execution_mode='fixture'
        AND c.valid_from<=clock_timestamp() AND c.valid_until>clock_timestamp()
        AND r.status='waiting_approval' AND r.graph_version='source-coding-v1'
        AND r.execution_mode='fixture' AND r.execution_epoch=b.execution_epoch
        AND t.status='open' AND t.mandate_id=c.mandate_id
        AND c.mandate_sha256=b.mandate_sha256 AND c.revision=b.coding_revision
        AND attempt.coding_id=c.coding_id AND attempt.run_id=r.run_id
        AND attempt.assignment_id=b.assignment_id AND attempt.session_id=b.session_id
        AND attempt.workspace_id=b.workspace_id AND attempt.execution_epoch=r.execution_epoch
        AND attempt.state='result_recorded' AND attempt.fenced_at IS NULL
        AND v.attempt_id=attempt.attempt_id AND v.coding_id=c.coding_id
        AND v.result='passed' AND v.tests_run>0 AND v.tests_failed=0
        AND v.mandate_sha256=c.mandate_sha256 AND v.execution_operation_id IS NOT NULL
        AND action.run_id=r.run_id AND action.action_type='draft_pr'
        AND action.payload->>'scope_kind'='source-review-v1'
        AND action.payload->>'review_id'=review.review_id::text
        AND action.execution_epoch=r.execution_epoch
    );
$$;
-- Deliberately do not require the old review to remain valid: the allocated
-- async revision invalidates it BEFORE this INSERT, in the same Engine TX.
-- Authority loss permanently fences b/c through the existing Source triggers;
-- received input cannot apply itself, open a new tool or approve publication.

CREATE OR REPLACE FUNCTION app.sa_async_input_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b app.sa_async_bindings%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'async input disposition is permanent'; END IF;
    SELECT * INTO b FROM app.sa_async_bindings WHERE async_id=NEW.async_id FOR UPDATE;
    IF NOT FOUND OR b.project_id<>NEW.project_id OR b.scope_sha256<>NEW.scope_sha256 THEN
        RAISE EXCEPTION 'async input cannot change target';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.sequence<>b.received_sequence OR NEW.input_revision<>b.input_revision
            OR NEW.state='applied' OR NEW.sequence<=0
            OR (NEW.origin<>'system' AND (SELECT count(*) FROM app.sa_async_inputs
                WHERE async_id=NEW.async_id)>=256)
            OR (NEW.state='received' AND NEW.origin<>'system'
                AND NOT app.sa_async_current(NEW.async_id)
                AND NOT app.sa_source_review_intake_current(NEW.async_id)) THEN
            RAISE EXCEPTION 'async input requires its allocated sequence and bounded current scope';
        END IF;
        IF NEW.origin='source' AND NOT EXISTS (
            SELECT 1 FROM app.source_ingress_inputs i
            WHERE i.input_id=NEW.source_input_id AND i.source_instance_id=NEW.source_instance_id
            AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint
            AND i.identity_id=NEW.identity_id AND i.identity_revision=NEW.identity_revision
            AND i.policy_id=NEW.policy_id AND i.policy_revision=NEW.policy_revision) THEN
            RAISE EXCEPTION 'async source reference must match its immutable canonical fact';
        END IF;
        IF NEW.origin='source' AND NEW.kind='restriction' AND NEW.state<>'blocked' AND NOT EXISTS (
            SELECT 1 FROM app.source_role_bindings role
            JOIN app.source_task_controllers controller ON controller.binding_id=role.binding_id
            JOIN app.source_identities identity ON identity.identity_id=role.identity_id
            JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
            JOIN app.source_ingress_inputs i ON i.input_id=NEW.source_input_id
            WHERE role.binding_id=NEW.role_binding_id AND role.identity_id=NEW.identity_id
            AND role.project_id=NEW.project_id AND role.revision=NEW.role_revision
            AND role.active AND role.roles @> '["task_controller"]'::jsonb
            AND controller.active AND controller.ticket_id=c.ticket_id AND controller.thread_id=c.thread_id
            AND identity.revoked_at IS NULL AND identity.actor_kind='human' AND NOT i.context_only) THEN
            RAISE EXCEPTION 'Source restriction requires current human exact Ticket control';
        END IF;
    ELSE
        IF OLD.state<>'received' OR NEW.state NOT IN ('applied','blocked')
            OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['state','applied_checkpoint_id','reason_code','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['state','applied_checkpoint_id','reason_code','updated_at']) THEN
            RAISE EXCEPTION 'async input is consumed once and never rewritten';
        END IF;
        IF NEW.state='applied' AND (NOT app.sa_async_current(NEW.async_id) OR NOT EXISTS (
            SELECT 1 FROM app.sa_async_checkpoints c WHERE c.checkpoint_id=NEW.applied_checkpoint_id
            AND c.async_id=NEW.async_id AND c.project_id=NEW.project_id
            AND c.through_sequence>=NEW.sequence AND c.input_revision=b.input_revision
            AND c.expires_at<=NEW.expires_at)) THEN
            RAISE EXCEPTION 'applied requires a committed current bounded checkpoint';
        END IF;
    END IF;
    RETURN NEW;
END $$;
