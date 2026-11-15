-- SAV1-19 proposal: Owner console credential custody and metadata-only audit.
-- No new Task/Run/Action/usage ledger, listener, runtime activation or budget.
CREATE TABLE app.sa_console_credentials (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('github','feishu')),
    scope_sha256 text NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL CHECK (revision>0),
    ciphertext bytea,
    ciphertext_sha256 text,
    field_names text[] NOT NULL,
    valid_until timestamptz NOT NULL,
    updated_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_ref text NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz,
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    CHECK ((ciphertext IS NULL)=(revoked_at IS NOT NULL)),
    CHECK ((ciphertext IS NULL)=(ciphertext_sha256 IS NULL)),
    CHECK (ciphertext IS NULL OR (octet_length(ciphertext) BETWEEN 1 AND 65536
        AND ciphertext_sha256=encode(sha256(ciphertext),'hex'))),
    CHECK ((source_kind='github' AND field_names=ARRAY['app_private_key','webhook_secret'])
        OR (source_kind='feishu' AND field_names=ARRAY['app_secret','encrypt_key','verification_token']))
);
CREATE FUNCTION app.sa_console_credential_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'console credential metadata is permanent'; END IF;
    IF NOT EXISTS (SELECT 1 FROM app.source_instances s
        WHERE s.source_instance_id=NEW.source_instance_id AND s.project_id=NEW.project_id
        AND s.source_kind=NEW.source_kind) THEN
        RAISE EXCEPTION 'console credentials require the exact source installation';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.ciphertext IS NULL THEN
            RAISE EXCEPTION 'console credentials start at revision one with encrypted content';
        END IF;
    ELSE
        IF ROW(NEW.source_instance_id,NEW.project_id,NEW.source_kind,NEW.scope_sha256,NEW.created_at)
            IS DISTINCT FROM
           ROW(OLD.source_instance_id,OLD.project_id,OLD.source_kind,OLD.scope_sha256,OLD.created_at)
            OR NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at THEN
            RAISE EXCEPTION 'console credentials use immutable installation and monotonic revisions';
        END IF;
    END IF;
    IF NEW.ciphertext IS NOT NULL AND (NEW.valid_until<=clock_timestamp()
        OR NEW.valid_until>clock_timestamp()+interval '365 days') THEN
        RAISE EXCEPTION 'console credentials require a bounded future expiry';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_console_credential_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_console_credentials FOR EACH ROW EXECUTE FUNCTION app.sa_console_credential_guard();

CREATE TABLE app.sa_console_audit (
    event_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    actor_user_id uuid NOT NULL REFERENCES app.web_accounts(user_id),
    event text NOT NULL CHECK (event IN ('source_saved','credentials_saved','source_revoked',
        'all_sources_disabled','policy_saved','ingress_saved','task_stopped')),
    target_id uuid,
    review_ref text NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 256),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details)='object'
        AND details-ARRAY['source_revision','settings_revision','credential_revision',
            'policy_revision','generation','execution_epoch','source_count','run_count',
            'pending_jobs','unknown_actions']='{}'::jsonb),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX sa_console_audit_project ON app.sa_console_audit(project_id,created_at,event_id);
CREATE FUNCTION app.sa_console_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'console audit is append only'; END $$;
CREATE TRIGGER sa_console_audit_append_only BEFORE UPDATE OR DELETE ON app.sa_console_audit
    FOR EACH ROW EXECUTE FUNCTION app.sa_console_audit_append_only();
