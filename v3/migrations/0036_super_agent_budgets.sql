-- SAV1-07: scope, attribution and quota facts on the original Engine connection.
-- Actual money/status remain in the existing usage_calls and router_calls ledgers.
CREATE TABLE app.sa_budget_policies (
    scope_type text COLLATE "C" NOT NULL CHECK (scope_type IN ('organization','project')),
    scope_id text COLLATE "C" NOT NULL CHECK (length(scope_id) BETWEEN 1 AND 200),
    organization_id text COLLATE "C" CHECK (length(organization_id) BETWEEN 1 AND 200),
    revision bigint NOT NULL CHECK (revision>0),
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    definition_sha256 char(64) NOT NULL CHECK (definition_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_ref text COLLATE "C" NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 200),
    signed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type,scope_id),
    CHECK ((scope_type='organization' AND organization_id IS NULL) OR
           (scope_type='project' AND organization_id IS NOT NULL)),
    CHECK (definition->>'organization_id' IS NOT DISTINCT FROM organization_id)
);
CREATE TABLE app.sa_budget_versions (
    scope_type text COLLATE "C" NOT NULL,
    scope_id text COLLATE "C" NOT NULL,
    organization_id text COLLATE "C",
    revision bigint NOT NULL CHECK (revision>0),
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    definition_sha256 char(64) NOT NULL CHECK (definition_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_ref text COLLATE "C" NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 200),
    signed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type,scope_id,revision),
    FOREIGN KEY (scope_type,scope_id) REFERENCES app.sa_budget_policies(scope_type,scope_id)
        DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE app.sa_budget_policies ADD CONSTRAINT sa_budget_current_version
    FOREIGN KEY (scope_type,scope_id,revision)
    REFERENCES app.sa_budget_versions(scope_type,scope_id,revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.sa_budget_calls (
    ledger_kind text COLLATE "C" NOT NULL CHECK (ledger_kind IN ('usage','router')),
    call_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    organization_id text COLLATE "C" NOT NULL CHECK (length(organization_id) BETWEEN 1 AND 200),
    component text COLLATE "C" NOT NULL CHECK
        (component IN ('router','worker','verification','recall','subtask')),
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens>0),
    price jsonb NOT NULL CHECK (jsonb_typeof(price)='object'),
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ledger_kind,call_id)
);
CREATE INDEX sa_budget_calls_project ON app.sa_budget_calls(project_id,observed_at);
CREATE INDEX sa_budget_calls_organization ON app.sa_budget_calls(organization_id,observed_at);

CREATE TABLE app.sa_budget_quota (
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    kind text COLLATE "C" NOT NULL CHECK
        (kind IN ('candidate','queue','slot','source','notification')),
    operation_hash char(64) NOT NULL CHECK (operation_hash ~ '^[a-f0-9]{64}$'),
    organization_id text COLLATE "C" NOT NULL CHECK (length(organization_id) BETWEEN 1 AND 200),
    source_hash char(64) CHECK (source_hash ~ '^[a-f0-9]{64}$'),
    units bigint NOT NULL CHECK (units>0),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
    observed_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    PRIMARY KEY (project_id,kind,operation_hash),
    CHECK (released_at IS NULL OR (kind IN ('queue','slot') AND released_at>=observed_at)),
    CHECK (kind<>'source' OR source_hash IS NOT NULL)
);
CREATE INDEX sa_budget_quota_organization ON app.sa_budget_quota(organization_id,kind,observed_at);
CREATE INDEX sa_budget_quota_source ON app.sa_budget_quota(project_id,source_hash,observed_at)
    WHERE kind='source';

CREATE TABLE app.sa_budget_audit (
    audit_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    event text COLLATE "C" NOT NULL CHECK (length(event) BETWEEN 1 AND 100),
    details jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details)='object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sa_budget_audit_project ON app.sa_budget_audit(project_id,audit_id DESC);

CREATE FUNCTION app.sa_budget_immutable_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Budget attribution and signed history are immutable'; END $$;
CREATE TRIGGER sa_budget_versions_immutable BEFORE UPDATE OR DELETE ON app.sa_budget_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_budget_immutable_fact();
CREATE TRIGGER sa_budget_calls_immutable BEFORE UPDATE OR DELETE ON app.sa_budget_calls
    FOR EACH ROW EXECUTE FUNCTION app.sa_budget_immutable_fact();
CREATE TRIGGER sa_budget_audit_immutable BEFORE UPDATE OR DELETE ON app.sa_budget_audit
    FOR EACH ROW EXECUTE FUNCTION app.sa_budget_immutable_fact();

CREATE FUNCTION app.protect_sa_budget_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Budget scopes cannot be deleted'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 THEN RAISE EXCEPTION 'Budget scope starts at revision one'; END IF;
    ELSE
        IF ROW(NEW.scope_type,NEW.scope_id,NEW.organization_id)
           IS DISTINCT FROM ROW(OLD.scope_type,OLD.scope_id,OLD.organization_id)
           OR NEW.revision<>OLD.revision+1 THEN
            RAISE EXCEPTION 'Budget identity is immutable; revision must advance';
        END IF;
    END IF;
    IF NEW.scope_type='project' AND NOT EXISTS (
        SELECT 1 FROM app.sa_budget_policies
        WHERE scope_type='organization' AND scope_id=NEW.organization_id
    ) THEN RAISE EXCEPTION 'Project budget requires its existing organization'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_sa_budget_policy BEFORE INSERT OR UPDATE OR DELETE ON app.sa_budget_policies
    FOR EACH ROW EXECUTE FUNCTION app.protect_sa_budget_policy();

CREATE FUNCTION app.check_sa_budget_signed_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM app.sa_budget_versions v
        WHERE v.scope_type=NEW.scope_type AND v.scope_id=NEW.scope_id AND v.revision=NEW.revision
          AND ROW(v.organization_id,v.definition,v.definition_sha256,v.signed_by,v.review_ref,v.signed_at)
              IS NOT DISTINCT FROM
              ROW(NEW.organization_id,NEW.definition,NEW.definition_sha256,NEW.signed_by,NEW.review_ref,NEW.signed_at)
    ) THEN RAISE EXCEPTION 'Budget head must match its immutable signed version'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_budget_signed_head AFTER INSERT OR UPDATE ON app.sa_budget_policies
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_sa_budget_signed_head();

CREATE FUNCTION app.protect_sa_budget_quota() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Budget quota receipts cannot be deleted'; END IF;
    IF ROW(NEW.project_id,NEW.kind,NEW.operation_hash,NEW.organization_id,NEW.source_hash,
           NEW.units,NEW.fingerprint,NEW.observed_at)
       IS DISTINCT FROM ROW(OLD.project_id,OLD.kind,OLD.operation_hash,OLD.organization_id,
                            OLD.source_hash,OLD.units,OLD.fingerprint,OLD.observed_at)
       OR OLD.released_at IS NOT NULL OR NEW.released_at IS NULL OR OLD.kind NOT IN ('queue','slot') THEN
        RAISE EXCEPTION 'Quota facts are immutable and can only release occupancy once';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_sa_budget_quota BEFORE UPDATE OR DELETE ON app.sa_budget_quota
    FOR EACH ROW EXECUTE FUNCTION app.protect_sa_budget_quota();
