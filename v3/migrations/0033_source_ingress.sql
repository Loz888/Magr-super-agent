-- SAV1-04. Engine-owned intake/retention facts; no duplicate task lifecycle or writer.
CREATE TABLE app.source_ingress_controls (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    generation bigint NOT NULL CHECK (generation>0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings)='object'),
    legacy_repository text COLLATE "C",
    updated_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    UNIQUE (source_instance_id,project_id),
    UNIQUE (project_id,legacy_repository)
);
CREATE TABLE app.source_ingress_deliveries (
    receipt_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    delivery_key char(64) NOT NULL UNIQUE,
    delivery_id text COLLATE "C",
    payload_sha256 char(64) NOT NULL,
    normalized_sha256 char(64) NOT NULL,
    generation bigint NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('accepted','rejected','conflict','legacy_owned')),
    reason_code text NOT NULL,
    input_ids jsonb NOT NULL DEFAULT '[]',
    denied_count integer NOT NULL DEFAULT 0,
    sealed boolean NOT NULL DEFAULT false,
    original_trigger_id uuid REFERENCES app.trigger_events(event_id),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    metadata_expires_at timestamptz NOT NULL,
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    UNIQUE (receipt_id,source_instance_id,project_id)
);
CREATE TABLE app.source_ingress_entities (
    entity_key char(64) PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    author_key char(64) NOT NULL,
    head_input_id uuid,
    native_order bigint,
    ambiguous boolean NOT NULL DEFAULT false,
    deleted boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id)
);
CREATE TABLE app.source_ingress_inputs (
    input_id uuid PRIMARY KEY,
    inbox_id uuid UNIQUE REFERENCES app.source_inbox(inbox_id) ON DELETE SET NULL,
    receipt_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    entity_key char(64) NOT NULL REFERENCES app.source_ingress_entities,
    version_key char(64) NOT NULL UNIQUE,
    version_fingerprint char(64) NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create','edit','delete')),
    native_order bigint,
    metadata jsonb,
    sender_ref jsonb,
    body_sha256 char(64),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    identity_id uuid NOT NULL REFERENCES app.source_identities(identity_id),
    identity_revision bigint NOT NULL,
    context_only boolean NOT NULL DEFAULT true,
    consumed_at timestamptz,
    unavailable_at timestamptz,
    reason_code text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    metadata_expires_at timestamptz NOT NULL,
    FOREIGN KEY (receipt_id,source_instance_id,project_id) REFERENCES app.source_ingress_deliveries(receipt_id,source_instance_id,project_id),
    FOREIGN KEY (policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    FOREIGN KEY (policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY (identity_id,source_instance_id,project_id)
        REFERENCES app.source_identities(identity_id,source_instance_id,project_id),
    UNIQUE (input_id,source_instance_id,project_id),
    UNIQUE (input_id,source_instance_id),
    UNIQUE (input_id,entity_key,source_instance_id,project_id)
);
CREATE UNIQUE INDEX source_ingress_native_version ON app.source_ingress_inputs(entity_key,native_order)
    WHERE native_order IS NOT NULL;
ALTER TABLE app.source_ingress_entities ADD CONSTRAINT source_entity_head
    FOREIGN KEY (head_input_id,entity_key,source_instance_id,project_id)
    REFERENCES app.source_ingress_inputs(input_id,entity_key,source_instance_id,project_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE app.source_ingress_aliases (
    event_key char(64) PRIMARY KEY,
    fingerprint char(64) NOT NULL,
    input_id uuid NOT NULL REFERENCES app.source_ingress_inputs,
    source_instance_id uuid NOT NULL REFERENCES app.source_instances,
    FOREIGN KEY (input_id,source_instance_id) REFERENCES app.source_ingress_inputs(input_id,source_instance_id)
);
CREATE TABLE app.source_ingress_bodies (
    input_id uuid PRIMARY KEY REFERENCES app.source_ingress_inputs,
    ciphertext bytea NOT NULL,
    plaintext_bytes integer NOT NULL CHECK (plaintext_bytes BETWEEN 0 AND 65536),
    expires_at timestamptz NOT NULL,
    CHECK (octet_length(ciphertext)<=100000)
);
CREATE TABLE app.source_ingress_conflicts (
    conflict_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.source_instances,
    delivery_key char(64) NOT NULL,
    attempted_sha256 char(64) NOT NULL,
    reason_code text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (delivery_key,attempted_sha256,reason_code)
);
CREATE TABLE app.source_ingress_cursors (
    stream_key char(64) PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    stream_id text COLLATE "C" NOT NULL,
    epoch text COLLATE "C" NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    committed_sequence bigint NOT NULL DEFAULT 0,
    highest_sequence bigint NOT NULL DEFAULT 0,
    cursor_value text COLLATE "C",
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id)
);
CREATE TABLE app.source_ingress_cursor_marks (
    stream_key char(64) NOT NULL REFERENCES app.source_ingress_cursors,
    epoch text COLLATE "C" NOT NULL,
    sequence bigint NOT NULL,
    cursor_value text COLLATE "C",
    receipt_id uuid NOT NULL REFERENCES app.source_ingress_deliveries,
    PRIMARY KEY (stream_key,epoch,sequence)
);
CREATE TABLE app.source_ingress_gaps (
    gap_id uuid PRIMARY KEY,
    gap_key char(64) NOT NULL UNIQUE,
    source_instance_id uuid NOT NULL REFERENCES app.source_instances,
    declaration jsonb NOT NULL,
    stream_key char(64) NOT NULL,
    epoch text COLLATE "C" NOT NULL,
    sequence_start bigint,
    sequence_end bigint,
    observed_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution_code text
);
CREATE TABLE app.source_ingress_audit (
    audit_id bigserial PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.source_instances,
    event text NOT NULL,
    actor_user_id uuid REFERENCES app.web_accounts(user_id),
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (details - ARRAY['review_ref','generation','reason_code','input_id','count','gap_id']='{}'::jsonb)
);
CREATE INDEX source_ingress_pending ON app.source_ingress_inputs(source_instance_id,received_at)
    WHERE consumed_at IS NULL AND unavailable_at IS NULL;
CREATE INDEX source_ingress_entity_inputs ON app.source_ingress_inputs(entity_key);
CREATE FUNCTION app.expire_source_input(input uuid, reason text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM app.source_ingress_bodies WHERE input_id=input;
    UPDATE app.source_ingress_inputs SET unavailable_at=coalesce(unavailable_at,now()),context_only=true,reason_code=reason
        WHERE input_id=input;
END $$;
CREATE FUNCTION app.source_ingress_authority_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_input uuid;
BEGIN
    IF TG_TABLE_NAME='source_policies' THEN
        FOR target_input IN SELECT input_id FROM app.source_ingress_inputs WHERE policy_id=NEW.policy_id AND unavailable_at IS NULL
        LOOP PERFORM app.expire_source_input(target_input,'INGRESS_POLICY_CHANGED'); END LOOP;
    ELSIF TG_TABLE_NAME='source_identities' THEN
        FOR target_input IN SELECT input_id FROM app.source_ingress_inputs WHERE identity_id=NEW.identity_id AND unavailable_at IS NULL AND identity_revision<>NEW.revision
        LOOP PERFORM app.expire_source_input(target_input,'INGRESS_IDENTITY_CHANGED'); END LOOP;
    ELSE
        FOR target_input IN SELECT input_id FROM app.source_ingress_inputs WHERE source_instance_id=NEW.source_instance_id AND unavailable_at IS NULL
        LOOP PERFORM app.expire_source_input(target_input,'INGRESS_CONFIGURATION_CHANGED'); END LOOP;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER source_ingress_policy_changed AFTER UPDATE ON app.source_policies
    FOR EACH ROW EXECUTE FUNCTION app.source_ingress_authority_changed();
CREATE TRIGGER source_ingress_identity_changed AFTER UPDATE ON app.source_identities
    FOR EACH ROW EXECUTE FUNCTION app.source_ingress_authority_changed();
CREATE TRIGGER source_ingress_instance_changed AFTER INSERT ON app.source_instance_versions
    FOR EACH ROW EXECUTE FUNCTION app.source_ingress_authority_changed();
CREATE TRIGGER source_ingress_control_changed AFTER UPDATE ON app.source_ingress_controls
    FOR EACH ROW EXECUTE FUNCTION app.source_ingress_authority_changed();
CREATE FUNCTION app.protect_ingress_control() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'ingress ownership history cannot disappear'; END IF;
    IF ROW(NEW.source_instance_id,NEW.project_id,NEW.legacy_repository) IS DISTINCT FROM ROW(OLD.source_instance_id,OLD.project_id,OLD.legacy_repository)
       OR NEW.generation<>OLD.generation+1 THEN RAISE EXCEPTION 'ingress owner identity is immutable; generation must advance'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_ingress_control BEFORE UPDATE OR DELETE ON app.source_ingress_controls
    FOR EACH ROW EXECUTE FUNCTION app.protect_ingress_control();
CREATE FUNCTION app.protect_ingress_input() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'ingress deduplication identity cannot be removed'; END IF;
    IF ROW(NEW.input_id,NEW.receipt_id,NEW.source_instance_id,NEW.project_id,NEW.entity_key,NEW.version_key,NEW.version_fingerprint,NEW.operation,NEW.native_order,NEW.body_sha256,NEW.policy_id,NEW.policy_revision,NEW.identity_id,NEW.identity_revision,NEW.received_at,NEW.metadata_expires_at)
       IS DISTINCT FROM ROW(OLD.input_id,OLD.receipt_id,OLD.source_instance_id,OLD.project_id,OLD.entity_key,OLD.version_key,OLD.version_fingerprint,OLD.operation,OLD.native_order,OLD.body_sha256,OLD.policy_id,OLD.policy_revision,OLD.identity_id,OLD.identity_revision,OLD.received_at,OLD.metadata_expires_at)
       OR (OLD.unavailable_at IS NOT NULL AND NEW.unavailable_at IS DISTINCT FROM OLD.unavailable_at)
       OR (OLD.context_only AND NOT NEW.context_only)
       OR (NEW.inbox_id IS DISTINCT FROM OLD.inbox_id AND NEW.inbox_id IS NOT NULL)
       OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
       OR (NEW.metadata IS DISTINCT FROM OLD.metadata AND NEW.metadata IS NOT NULL)
       OR (NEW.sender_ref IS DISTINCT FROM OLD.sender_ref AND NEW.sender_ref IS NOT NULL) THEN
       RAISE EXCEPTION 'ingress input authority is immutable and cannot be revived'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_ingress_input BEFORE UPDATE OR DELETE ON app.source_ingress_inputs
    FOR EACH ROW EXECUTE FUNCTION app.protect_ingress_input();
CREATE FUNCTION app.protect_ingress_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'durable receipts cannot disappear'; END IF;
    IF OLD.sealed AND ((to_jsonb(NEW)-'delivery_id') IS DISTINCT FROM (to_jsonb(OLD)-'delivery_id')
        OR (NEW.delivery_id IS DISTINCT FROM OLD.delivery_id AND NEW.delivery_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'committed ingress receipt is immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_ingress_receipt BEFORE UPDATE OR DELETE ON app.source_ingress_deliveries
    FOR EACH ROW EXECUTE FUNCTION app.protect_ingress_receipt();
CREATE FUNCTION app.require_sealed_ingress_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.source_ingress_deliveries WHERE receipt_id=NEW.receipt_id AND NOT sealed) THEN
        RAISE EXCEPTION 'receipt must be finalized before commit'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER require_sealed_ingress_receipt AFTER INSERT OR UPDATE ON app.source_ingress_deliveries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.require_sealed_ingress_receipt();
CREATE FUNCTION app.protect_ingress_entity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'entity tombstones cannot disappear'; END IF;
    IF ROW(NEW.entity_key,NEW.source_instance_id,NEW.project_id,NEW.author_key)
       IS DISTINCT FROM ROW(OLD.entity_key,OLD.source_instance_id,OLD.project_id,OLD.author_key)
       OR (OLD.deleted AND NOT NEW.deleted) OR (OLD.ambiguous AND NOT NEW.ambiguous)
       OR (OLD.native_order IS NOT NULL AND (NEW.native_order IS NULL OR NEW.native_order<OLD.native_order)) THEN
       RAISE EXCEPTION 'entity identity and tombstones are immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_ingress_entity BEFORE UPDATE OR DELETE ON app.source_ingress_entities
    FOR EACH ROW EXECUTE FUNCTION app.protect_ingress_entity();
CREATE TRIGGER source_ingress_alias_immutable BEFORE UPDATE OR DELETE ON app.source_ingress_aliases
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_ingress_conflict_immutable BEFORE UPDATE OR DELETE ON app.source_ingress_conflicts
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_ingress_mark_immutable BEFORE UPDATE OR DELETE ON app.source_ingress_cursor_marks
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_ingress_audit_immutable BEFORE UPDATE OR DELETE ON app.source_ingress_audit
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
