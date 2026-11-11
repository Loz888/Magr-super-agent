-- SAV1-05: installation configuration and explicit read-only snapshot authority.
-- Original Engine connection is the only writer. No second task or budget ledger.
CREATE TABLE app.github_source_settings (
    source_instance_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    source_revision bigint NOT NULL,
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT now(),
    review_ref text NOT NULL,
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY (source_instance_id,source_revision) REFERENCES app.source_instance_versions,
    UNIQUE (source_instance_id,revision),
    CHECK (definition->>'schema_version'='github-app-v1')
);
CREATE TABLE app.github_source_versions (
    source_instance_id uuid NOT NULL REFERENCES app.github_source_settings,
    revision bigint NOT NULL CHECK (revision > 0),
    source_revision bigint NOT NULL,
    definition jsonb NOT NULL,
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT now(),
    review_ref text NOT NULL,
    PRIMARY KEY (source_instance_id,revision)
);
CREATE TABLE app.github_snapshot_grants (
    grant_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.github_source_settings,
    config_revision bigint NOT NULL,
    source_revision bigint NOT NULL,
    owner_generation bigint NOT NULL CHECK (owner_generation > 0),
    revision bigint NOT NULL CHECK (revision > 0),
    revoked boolean NOT NULL DEFAULT false,
    definition jsonb NOT NULL,
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT now(),
    review_ref text NOT NULL,
    FOREIGN KEY (source_instance_id,config_revision) REFERENCES app.github_source_versions,
    FOREIGN KEY (source_instance_id,source_revision) REFERENCES app.source_instance_versions
);
CREATE TABLE app.github_source_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.github_source_settings,
    event text NOT NULL,
    reason_code text NOT NULL,
    actor_user_id uuid REFERENCES app.web_accounts(user_id),
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION app.github_immutable_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'GitHub settings history is immutable'; END $$;
CREATE TRIGGER github_version_immutable BEFORE UPDATE OR DELETE ON app.github_source_versions
FOR EACH ROW EXECUTE FUNCTION app.github_immutable_version();
CREATE FUNCTION app.github_grant_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.definition IS DISTINCT FROM OLD.definition OR NEW.source_instance_id<>OLD.source_instance_id
       OR NEW.config_revision<>OLD.config_revision OR NEW.source_revision<>OLD.source_revision
       OR NEW.owner_generation<>OLD.owner_generation OR NEW.signed_by<>OLD.signed_by
       OR NEW.signed_at<>OLD.signed_at OR NEW.review_ref<>OLD.review_ref OR OLD.revoked
       OR NOT NEW.revoked OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'Snapshot grant can only be revoked, never upgraded or revived';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER github_grant_immutable BEFORE UPDATE ON app.github_snapshot_grants
FOR EACH ROW EXECUTE FUNCTION app.github_grant_immutable();
