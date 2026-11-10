-- SAV1-02: verified source identities, explicit conversation roles and minimal audit.
-- All changes occur on the existing Engine transaction connection.
CREATE TABLE app.source_principals (
    principal_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    principal_kind text NOT NULL CHECK (principal_kind IN ('human','automation')),
    engine_actor text NOT NULL UNIQUE CHECK (engine_actor ~ '^sa_[a-f0-9]{32}$'),
    console_user_id uuid REFERENCES app.web_accounts(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (principal_id,project_id),
    UNIQUE (project_id,console_user_id),
    CHECK (console_user_id IS NULL OR principal_kind='human')
);
CREATE TABLE app.source_identities (
    identity_id uuid PRIMARY KEY,
    identity_key char(64) NOT NULL UNIQUE,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    id_type text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    actor_kind text NOT NULL CHECK (actor_kind IN ('human','bot','service')),
    actor_ref jsonb NOT NULL CHECK (jsonb_typeof(actor_ref)='object'),
    principal_id uuid,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY (principal_id,project_id) REFERENCES app.source_principals(principal_id,project_id),
    UNIQUE (source_instance_id,id_type,actor_id),
    UNIQUE (identity_id,project_id),
    UNIQUE (identity_id,source_instance_id,project_id)
);
CREATE TABLE app.source_identity_evidence (
    evidence_id uuid PRIMARY KEY,
    identity_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    source_revision bigint NOT NULL,
    auth_revision bigint NOT NULL CHECK (auth_revision > 0),
    remote_event_id text COLLATE "C" NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    credential_hash char(64) NOT NULL UNIQUE CHECK (credential_hash ~ '^[a-f0-9]{64}$'),
    conversation_key char(64) NOT NULL,
    source_thread jsonb NOT NULL,
    verification_method text NOT NULL CHECK (verification_method IN ('platform_signature','installation_proof')),
    verifier_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (identity_id,source_instance_id,project_id)
        REFERENCES app.source_identities(identity_id,source_instance_id,project_id),
    FOREIGN KEY (source_instance_id,source_revision)
        REFERENCES app.source_instance_versions(source_instance_id,revision),
    UNIQUE (source_instance_id,remote_event_id),
    CHECK (expires_at > verified_at)
);
CREATE TABLE app.source_role_bindings (
    binding_id uuid PRIMARY KEY,
    identity_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    conversation_key char(64) NOT NULL,
    roles jsonb NOT NULL CHECK (jsonb_typeof(roles)='array'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    active boolean NOT NULL DEFAULT true,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (identity_id,project_id) REFERENCES app.source_identities(identity_id,project_id),
    UNIQUE (identity_id,conversation_key),
    UNIQUE (binding_id,project_id),
    CHECK (jsonb_array_length(roles) BETWEEN 1 AND 4),
    CHECK ('["context_provider","requester","task_controller","project_administrator"]'::jsonb @> roles),
    CHECK (active=(revoked_at IS NULL))
);
CREATE TABLE app.source_task_controllers (
    binding_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    active boolean NOT NULL DEFAULT true,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    PRIMARY KEY (binding_id,ticket_id),
    FOREIGN KEY (binding_id,project_id) REFERENCES app.source_role_bindings(binding_id,project_id),
    FOREIGN KEY (ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY (thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    CHECK (active=(revoked_at IS NULL))
);
CREATE TABLE app.source_identity_audit (
    audit_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    actor_user_id uuid REFERENCES app.web_accounts(user_id),
    event text NOT NULL,
    target_id uuid,
    decision text NOT NULL CHECK (decision IN ('allow','deny')),
    reason_code text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (details - ARRAY['roles','principal_id','ticket_id','revision','review_ref','evidence_id']='{}'::jsonb)
);
CREATE INDEX source_identity_evidence_recent ON app.source_identity_evidence(project_id,verified_at DESC);
CREATE INDEX source_identity_audit_recent ON app.source_identity_audit(project_id,audit_id DESC);

CREATE FUNCTION app.protect_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.identity_id,NEW.identity_key,NEW.source_instance_id,NEW.project_id,
        NEW.id_type,NEW.actor_id,NEW.actor_kind,NEW.actor_ref,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.identity_id,OLD.identity_key,OLD.source_instance_id,OLD.project_id,
        OLD.id_type,OLD.actor_id,OLD.actor_kind,OLD.actor_ref,OLD.created_at)
       OR (OLD.principal_id IS NOT NULL AND NEW.principal_id IS DISTINCT FROM OLD.principal_id)
       OR NEW.revision <> OLD.revision+1 THEN
        RAISE EXCEPTION 'source identity is immutable; authorization revision must advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_source_identity BEFORE UPDATE ON app.source_identities
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_identity();
CREATE FUNCTION app.protect_source_role_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    kind text;
BEGIN
    SELECT actor_kind INTO kind FROM app.source_identities WHERE identity_id=NEW.identity_id;
    IF kind <> 'human' AND NEW.roles <> '["context_provider"]'::jsonb THEN
        RAISE EXCEPTION 'nonhuman source identities can only provide context';
    END IF;
    IF TG_OP='UPDATE' AND (ROW(NEW.binding_id,NEW.identity_id,NEW.project_id,NEW.conversation_key,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.binding_id,OLD.identity_id,OLD.project_id,OLD.conversation_key,OLD.created_at)
       OR NEW.revision <> OLD.revision+1) THEN
        RAISE EXCEPTION 'binding identity is immutable; binding revision must advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_source_role_binding BEFORE INSERT OR UPDATE ON app.source_role_bindings
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_role_binding();
CREATE FUNCTION app.protect_source_controller_target() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.binding_id,NEW.ticket_id,NEW.thread_id,NEW.project_id)
       IS DISTINCT FROM ROW(OLD.binding_id,OLD.ticket_id,OLD.thread_id,OLD.project_id) THEN
        RAISE EXCEPTION 'controller target is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_source_controller_target BEFORE UPDATE ON app.source_task_controllers
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_controller_target();
CREATE FUNCTION app.invalidate_source_credentials() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    identity uuid;
    binding uuid;
BEGIN
    IF TG_TABLE_NAME='source_role_bindings' THEN
        IF TG_OP='DELETE' THEN identity := OLD.identity_id; ELSE identity := NEW.identity_id; END IF;
    ELSE
        IF TG_OP='DELETE' THEN binding := OLD.binding_id; ELSE binding := NEW.binding_id; END IF;
        SELECT identity_id INTO identity FROM app.source_role_bindings WHERE binding_id=binding;
    END IF;
    UPDATE app.source_identities SET revision=revision+1 WHERE identity_id=identity;
    RETURN NULL;
END $$;
CREATE TRIGGER invalidate_binding_credentials AFTER INSERT OR UPDATE OR DELETE ON app.source_role_bindings
    FOR EACH ROW EXECUTE FUNCTION app.invalidate_source_credentials();
CREATE TRIGGER invalidate_controller_credentials AFTER INSERT OR UPDATE OR DELETE ON app.source_task_controllers
    FOR EACH ROW EXECUTE FUNCTION app.invalidate_source_credentials();
CREATE TRIGGER source_principals_immutable BEFORE UPDATE OR DELETE ON app.source_principals
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_identity_evidence_immutable BEFORE UPDATE OR DELETE ON app.source_identity_evidence
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_identity_audit_immutable BEFORE UPDATE OR DELETE ON app.source_identity_audit
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
