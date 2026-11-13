-- SA11 first-run repair proposal; new0043 requires fixed numbering confirmation.
-- Preserve published0042/checksums. Only split record-field evaluation by table.
CREATE OR REPLACE FUNCTION app.sa_source_subscription_canonical_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier uuid;
BEGIN
    IF TG_TABLE_NAME='sa_source_subscription_versions' THEN
        identifier := NEW.seed_input_id;
    ELSE
        identifier := NEW.input_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.source_ingress_inputs i WHERE i.input_id=identifier
        AND i.source_instance_id=NEW.source_instance_id AND i.project_id=NEW.project_id
        AND i.version_fingerprint=NEW.version_fingerprint) THEN
        RAISE EXCEPTION 'source subscription requires the exact permanent canonical input';
    END IF;
    RETURN NEW;
END $$;
