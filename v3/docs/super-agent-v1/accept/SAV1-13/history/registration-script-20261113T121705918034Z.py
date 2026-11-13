"""SA13 documentation/schema registration. No tests, SQL, runtimes or model calls.

Run each explicit phase only after the supervising agent freezes its inputs.
The schema phase imports only the strict Coding domain to export model schemas.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys

APP = Path('/home/imini/magr-super-agent/v3')
REPO = APP.parent
HISTORY = APP / 'docs/super-agent-v1/accept/SAV1-13/history'
OWNERSHIP = '617e14a52d65c5276bcd3ef87dbd0f05b4a3c08d'
SQL = '0046_source_coding.sql'
SQL_SHA = '379e9de4a018c7f38cc3615eae31b9c9d9e44ebdf16cbcf3c101c81d9874f212'
NEW_TESTS = [
    'tests/test_super_agent_sa13.py',
    'tests/test_super_agent_sa13_api.py',
    'tests/test_super_agent_sa13_migration.py',
    'tests/test_super_agent_sa13_compatibility.py',
    'tests/test_super_agent_sa13_runtime.py',
    'tests/test_super_agent_sa13_recovery.py',
    'tests/test_super_agent_sa13_verification.py',
    'tests/test_super_agent_sa13_lifecycle.py',
    'tests/test_super_agent_sa13_initial_tree.py',
]
DIRECT_DEPENDENCIES = [
    'tests/test_harness_v4_h01.py::test_reference_loop_reads_edits_and_runs_real_tests',
    'tests/test_harness_v4_h01.py::test_executor_is_replaceable_without_changing_the_runtime',
    'tests/test_harness_v4_h02.py::test_two_adapters_share_the_tool_contract',
    'tests/test_harness_v4_h02.py::test_tool_roundtrip_preserves_native_history',
    'tests/test_harness_v4_h02.py::test_cache_hit_is_not_billed_twice',
    'tests/test_super_agent_sa12_migration.py::test_formal0044_exact_source_catalogue_and_original_engine_connection',
    'tests/test_super_agent_sa12_migration.py::test_controller_transactions_roll_back_claim_start_and_quota_together',
    'tests/test_super_agent_sa12_source.py::test_current_source_change_fences_already_claimed_worker_before_new_operation',
    'tests/test_super_agent_sa12_runtime.py::test_actual_create_then_revocation_or_expiry_never_reaches_actual_start',
]
OLD_CATALOGUES = [
    'tests/test_super_agent_sa05.py',
    'tests/test_super_agent_sa06_migration.py',
    'tests/test_super_agent_sa07_migration.py',
    'tests/test_super_agent_sa08_migration.py',
    'tests/test_super_agent_sa09_migration.py',
    'tests/test_super_agent_sa10_migration.py',
    'tests/test_super_agent_sa11_migration.py',
    'tests/test_super_agent_sa12_migration.py',
]
SCHEMAS = {
    'source-coding-mandate-v1.schema.json': 'CodingMandate',
    'source-coding-admit-v1.schema.json': 'CodingAdmitRequest',
    'source-coding-run-v1.schema.json': 'CodingRunRequest',
    'source-coding-inspect-v1.schema.json': 'CodingInspectRequest',
    'source-coding-cancel-v1.schema.json': 'CodingCancelRequest',
}


def now():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def archive_originals():
    """Preserve pre-registration bytes once; never replace an existing archive."""
    manifest_path = HISTORY / 'pre-sa13-snapshot.json'
    if manifest_path.exists():
        return
    old_configs = [
        path for path in sorted((APP / 'config').glob('*.json'))
        if path.name != 'source-coding-v1.json'
        and ('artifacts' in json.loads(path.read_text()) or path.name == 'verification.json')
    ]
    paths = [*old_configs, *(APP / path for path in OLD_CATALOGUES)]
    archived = []
    for path in paths:
        relative = path.relative_to(APP)
        target = HISTORY / 'pre-sa13' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise RuntimeError('Existing archive differs: ' + str(relative))
        target.write_bytes(path.read_bytes())
        archived.append({'path': str(relative), 'sha256': sha(path),
                         'preserved_path': str(target.relative_to(APP))})
    dump(manifest_path, {
        'schema': 'sa13-pre-registration-bytes-v1',
        'recorded_utc': now(),
        'source_commit': subprocess.check_output(
            ['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
        'purpose': 'Preserve original config and mechanical catalogue test bytes before SA13 registration. Historical source-bound PASS/FAIL evidence is not edited or reinterpreted.',
        'files': archived,
    })


def register():
    archive_originals()
    path = APP / 'config/verification.json'
    value = json.loads(path.read_text())
    value['SAV1-13'] = {
        'tests': [*NEW_TESTS, *DIRECT_DEPENDENCIES],
        'timeout_seconds': 2400,
        'components': {
            'scope': 'Initial formal SA13 integration registration: all new Core, API, migration, fixed-kernel compatibility, actual isolated Runtime and recovery checks, plus directly affected H01/H02 and SA12 guards selected by node ID. This registration is not a test outcome and is not the full repository suite.',
            'postgres': 'Real disposable PostgreSQL; original Engine mutex/advisory lock and same held connection; original Thread/Ticket/Run/Mandate and usage_calls remain authoritative.',
            'ownership': OWNERSHIP + ' section38; exact0046 and four-file Engine/API/Scheduler/contracts integration. No0045 import.',
            'http': 'Six exact metadata routes on original create_app; Owner current cookie/Origin/CSRF and transaction recheck, independent current Assignment-bound Worker bearer, strict raw JSON and no-store.',
            'runtime': 'Explicit controlled fixture only; fixed H11 scoped kernel through actual H03 and bound H06. Default runtime/provider/Worker/listener/thread remain None.',
            'authority': 'Fresh Source/head/identity/roles/policy/context/budget/profile, original target/session/Assignment/lease/epoch and actual tree/environment at dispatch and verification.',
            'recovery': 'Durable original intent, encrypted bounded payloads, existing-file pre/postimages, actual owned process inspection and no unknown resend/refund; failures and raw receipts remain immutable.',
            'compatibility': 'Public H01 two-argument reference registry retained; fixed H11 strict authority namespace; full-content-only edits reject every supplied replacements key including general Mapping/null/empty; legacy receipts load with provenance=None.',
            'test_policy': 'Original 139 contract PASS and 13 workspace PASS are historical preparation evidence and excluded from this run. After failures rerun only failed cases or affected failed targets; never relabel a mixed-version aggregate as one full PASS.',
            'independent_review': 'NOT_RUN',
            'semantic_evaluation': 'NOT_RUN',
            'live_source_channel': 'NOT_RUN',
            'paid_model': 'NOT_RUN',
            'live_coding': 'NOT_RUN',
            'live_publication': 'NOT_RUN',
            'continuous_operation': 'NOT_RUN',
            'harness_h12_live_agent': 'FAIL',
            'harness_h12_mvp': 'FAIL',
        },
    }
    dump(path, value)
    config_path = APP / 'config/source-coding-v1.json'
    current = json.loads(config_path.read_text()) if config_path.exists() else {}
    current.update({
        'contract_version': 'source-coding-mandate-v1',
        'runtime_version': 'source-coding-fixture-runtime-v1',
        'status': 'FORMAL_IMPLEMENTATION_IN_PROGRESS',
        'ownership_status': 'ACTUALLY_CONFIRMED_FIXED_COMMIT',
        'ownership_commit': OWNERSHIP,
        'ownership_section': 38,
        'migration': SQL,
        'migration_sha256': SQL_SHA,
        'migration_count_after_install': 43,
        'source_fact_tables': 6,
        'management_prefix': '/api/v3/source-coding',
        'owner_routes': 4,
        'worker_routes': 2,
        'default_runtime': None,
        'default_provider': None,
        'default_cipher': None,
        'default_worker': None,
        'default_listener': None,
        'default_supervisor_thread': None,
        'default_profiles': [],
        'authority': 'Original Engine same held PG connection, mutex and advisory lock; original Thread/Ticket/Run/Mandate/Assignment/session/epoch and original shared financial ledgers. The Coding namespace never upgrades legacy grants.',
        'repository_support': 'Only explicit public fixture_git bugfix of existing pure-function .py files approved by the bounded AST fixture gate; private/live GitHub snapshots, other writable file types, new files, arbitrary feature/refactor and run_app are unavailable.',
        'edit_protocol': 'Required full-content edits only. Any supplied replacements key, including null/empty and general Mapping input, is rejected; original R5 worktree remains unchanged.',
        'financial_truth': 'Original usage_calls and Engine settlement with SA07 organization/project reserve. Unknown continues occupying the original reservation; no new monetary ledger or automatic paid activation.',
        'payload_retention': 'Bounded encrypted Source-dependent model responses and checkpoints expire within the current Source origin/Mandate and a server-only retention of1..1800seconds, default1800. Payload reads first commit maintenance outside the access transaction; expired or withdrawn ciphertext is irreversibly erased before a denied read can roll back. Minimal bodyless watermark remains.',
        'payload_retention_seconds_default': 1800,
        'payload_retention_seconds_min': 1,
        'payload_retention_seconds_max': 1800,
        'maintenance': {
            'entrypoint': 'SourceCoding.maintain',
            'activation': 'Explicit trusted Python server port; no HTTP route, default maintenance thread or platform listener.',
            'batch_limit_default': 128,
            'batch_limit_max': 256,
            'cursor': 'coding_id ordered after a bounded UUID cursor',
            'transaction': 'Requires the original held connection to be idle before its own short transaction. Committed erasure cannot be rolled back by the later access transaction.',
            'read_path': 'SourceCodingJournal.get_payload performs scoped maintenance before current authority and decryption.',
            'effects': 'Fence invalid attempts and erase expired or withdrawn ciphertext while preserving minimal facts; no resource/model IO, financial settlement, resource release, resend or business completion.',
        },
        'initial_tree_binding': 'Before first Coding execution, recheck actual H03 manifest, registration and immutable reviewed initial tree; a storage-owner edit is not an approved replacement. Subsequent changes require the original trusted pre/postimage journal and current authority.',
        'trusted_verification': 'Exact original Task/Run/Assignment/session/epoch/resource operation and actual H03 tree/environment/profile/acceptance plus trusted Broker journal; bare Worker/model claims cannot establish verification or complete a business Task.',
        'harness_h11_commit': 'd3f3457b3d9dfab68bc73a40add299e4e633fbeb',
        'harness_h11_engineering': 'PASS_INDEPENDENT_SCOPED',
        'harness_h11_recovery': 'PASS_INDEPENDENT_SCOPED',
        'harness_h12_commit': '88c75b8dac75da7e7d0c283a451c0ff5a3a3c9b0',
        'harness_h12_live_agent': 'FAIL',
        'harness_h12_mvp': 'FAIL',
        'engineering_gate': 'NOT_RUN',
        'policy_security_gate': 'NOT_RUN',
        'independent_review': 'NOT_RUN',
        'semantic_evaluation': 'NOT_RUN',
        'live_source_channel': 'NOT_RUN',
        'paid_model': 'NOT_RUN',
        'live_coding': 'NOT_RUN',
        'live_publication': 'NOT_RUN',
        'continuous_operation': 'NOT_RUN',
        'SA14': 'PLANNED_NOT_STARTED',
        'verification_registration': 'SAV1-13; source-bound outcomes are separate and no PASS is implied by registration or artifact hashes.',
        'historical_preparation': {'contract_passed': 139, 'workspace_passed': 13,
                                   'rerun': False, 'formal_integration_inferred': False},
    })
    dump(config_path, current)


def catalogue():
    archive_originals()
    changes = []
    for relative in OLD_CATALOGUES:
        path = APP / relative
        before = path.read_text()
        lines = before.splitlines(keepends=True)
        after = []
        for index, line in enumerate(lines):
            after.append(line)
            if (re.fullmatch(r'\s*"0044_source_scheduler\.sql",\s*\n', line)
                and (index + 1 >= len(lines) or '"0046_source_coding.sql"' not in lines[index + 1])):
                prefix = re.match(r'\s*', line).group(0)
                after.append(prefix + '"0046_source_coding.sql",\n')
        value = ''.join(after)
        for variable in ('checksums', 'sources', 'source', 'expected'):
            value = value.replace('len(' + variable + ') == 42', 'len(' + variable + ') == 43')
        if value != before:
            path.write_text(value)
            changes.append({'path': relative, 'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                            'after_sha256': sha(path)})
    dump(HISTORY / 'mechanical-catalogue-update.json', {
        'schema': 'sa13-mechanical-current-catalogue-update-v1',
        'recorded_utc': now(), 'formal_migration': SQL,
        'scope': 'Only current directory size42→43 and expected forward migration0046 appended. Historical baseline counts/checksums and all old SQL remain unchanged. These source edits do not rerun or replace historical PASS evidence.',
        'files': changes,
    })


def export_schemas():
    sys.path.insert(0, str(APP / 'src'))
    domain = importlib.import_module('mgar_v3.super_agent.source_coding_domain')
    for name, model_name in SCHEMAS.items():
        model = getattr(domain, model_name)
        dump(APP / 'schemas/super-agent-v1' / name, model.model_json_schema())


def lifecycle_metadata():
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    directory = HISTORY / ('before-lifecycle-metadata-' + stamp)
    directory.mkdir(parents=True)
    preserved = []
    for name in ('verification.json', 'source-coding-v1.json'):
        source = APP / 'config' / name
        target = directory / name
        target.write_bytes(source.read_bytes())
        preserved.append({'path': 'config/' + name, 'sha256': sha(source),
                          'preserved_path': str(target.relative_to(APP))})
    dump(directory / 'snapshot.json', {
        'schema': 'sa13-before-lifecycle-metadata-bytes-v1', 'recorded_utc': now(),
        'scope': 'Original bytes preserved before adding two new first-run registrations and current lifecycle support metadata. No historical outcome is edited or reinterpreted.',
        'files': preserved,
    })
    registry_path = APP / 'config/verification.json'
    registry = json.loads(registry_path.read_text())
    entry = registry['SAV1-13']
    for target in ('tests/test_super_agent_sa13_lifecycle.py',
                   'tests/test_super_agent_sa13_initial_tree.py'):
        if target not in entry['tests']:
            entry['tests'].append(target)
    entry['components']['lifecycle'] = 'New first-run checks for exact1..1800second server retention, committed expiry/authority-loss erasure before decrypt, attempt binding, idempotent explicit metadata maintenance and no rollback resurrection. Added after the initial185 results; run separately without repeating passed cases.'
    entry['components']['initial_tree'] = 'New first-run guard for actual H03 manifest/registration/reviewed immutable initial tree: a legitimate storage-owner edit cannot substitute for the original Coding tree.'
    entry['components']['engineering_gate'] = 'NOT_RUN'
    dump(registry_path, registry)
    path = APP / 'config/source-coding-v1.json'
    value = json.loads(path.read_text())
    value.update({
        'status': 'FORMAL_IMPLEMENTATION_IN_PROGRESS',
        'repository_support': 'Only explicit public fixture_git bugfix of existing pure-function .py files approved by the bounded AST fixture gate; private/live GitHub snapshots, other writable file types, new files, arbitrary feature/refactor and run_app are unavailable.',
        'payload_retention': 'Bounded encrypted Source-dependent model responses and checkpoints expire within the current Source origin/Mandate and a server-only retention of1..1800seconds, default1800. Payload reads first commit maintenance outside the access transaction; expired or withdrawn ciphertext is irreversibly erased before a denied read can roll back. Minimal bodyless watermark remains.',
        'payload_retention_seconds_default': 1800,
        'payload_retention_seconds_min': 1,
        'payload_retention_seconds_max': 1800,
        'maintenance': {
            'entrypoint': 'SourceCoding.maintain',
            'activation': 'Explicit trusted Python server port; no HTTP route, default maintenance thread or platform listener.',
            'batch_limit_default': 128,
            'batch_limit_max': 256,
            'cursor': 'coding_id ordered after a bounded UUID cursor',
            'transaction': 'Requires the original held connection to be idle before its own short transaction. Committed erasure cannot be rolled back by the later access transaction.',
            'read_path': 'SourceCodingJournal.get_payload performs scoped maintenance before current authority and decryption.',
            'effects': 'Fence invalid attempts and erase expired or withdrawn ciphertext while preserving minimal facts; no resource/model IO, financial settlement, resource release, resend or business completion.',
        },
        'initial_tree_binding': 'Before first Coding execution, recheck actual H03 manifest, registration and immutable reviewed initial tree; a storage-owner edit is not an approved replacement. Subsequent changes require the original trusted pre/postimage journal and current authority.',
    })
    dump(path, value)


def refresh_artifacts():
    archive_originals()
    snapshot = now()
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    saved_script = HISTORY / ('registration-script-' + stamp + '.py')
    saved_script.write_bytes(Path(__file__).read_bytes())
    configs = {}
    for path in sorted((APP / 'config').glob('*.json')):
        value = json.loads(path.read_text())
        if (path.name != 'source-coding-v1.json' and isinstance(value, dict)
            and isinstance(value.get('artifacts'), list)):
            configs[str(path.relative_to(APP))] = value
    ordered, done, active = [], set(), set()

    def visit(relative):
        if relative in done:
            return
        if relative in active:
            raise RuntimeError('Circular config artifact dependency: ' + relative)
        active.add(relative)
        for artifact in configs[relative]['artifacts']:
            if artifact['path'] in configs:
                visit(artifact['path'])
        active.remove(relative)
        done.add(relative)
        ordered.append(relative)

    for relative in configs:
        visit(relative)
    for relative in ordered:
        path, value = APP / relative, configs[relative]
        changed = False
        for artifact in value['artifacts']:
            actual = sha(APP / artifact['path'])
            if actual != artifact['sha256']:
                artifact['sha256'] = actual
                changed = True
        if changed:
            value['artifact_snapshot_utc'] = snapshot
            value['artifact_snapshot_scope'] = 'Current formal per-file hashes refreshed for authorized SA13 composition/catalogue. Historical source-bound PASS/FAIL records remain unchanged; pre-SA13 original bytes are preserved in docs/super-agent-v1/accept/SAV1-13/history/pre-sa13/. This refresh is not acceptance evidence.'
            value['artifact_status'] = 'CURRENT_FORMAL_HASHES'
            dump(path, value)
    coding_path = APP / 'config/source-coding-v1.json'
    coding = json.loads(coding_path.read_text())
    paths = {
        'config/verification.json', 'config/source-scheduler-v1.json',
        'scripts/verify.py', 'migrations/' + SQL,
        'src/mgar_v3/api.py', 'src/mgar_v3/coordination/engine.py',
        'src/mgar_v3/super_agent/source_scheduler.py', 'src/mgar_v3/harness/contracts.py',
        'src/mgar_v3/harness/coding_loop.py', 'src/mgar_v3/harness/long_context.py',
        'src/mgar_v3/harness/source_coding_adapters.py', 'src/mgar_v3/harness/incremental.py',
        *NEW_TESTS, *(target.split('::', 1)[0] for target in DIRECT_DEPENDENCIES),
        *OLD_CATALOGUES, 'tests/sa13_support.py', 'tests/sa13_runtime_support.py',
        'docs/super-agent-v1/accept/SAV1-13/history/registration-script.py',
        str(saved_script.relative_to(APP)),
        'docs/super-agent-v1/accept/SAV1-13/history/api-import-order-followup.json',
        'docs/super-agent-v1/accept/SAV1-13/history/api-import-order-followup.patch',
    }
    paths.update(artifact['path'] for artifact in coding.get('artifacts', []))
    for pattern in ('src/mgar_v3/super_agent/source_coding*.py',
                    'docs/super-agent-v1/sa13-proposal/*',
                    'schemas/super-agent-v1/source-coding-*.json',
                    'tests/sa13*actor.py'):
        paths.update(str(path.relative_to(APP)) for path in APP.glob(pattern) if path.is_file())
    coding['artifact_snapshot_utc'] = snapshot
    coding['artifact_snapshot_scope'] = 'Current formal source/schema/test hashes; these hashes neither rerun historical PASS nor establish a new engineering/independent/live gate.'
    coding['artifacts'] = [{'path': relative, 'sha256': sha(APP / relative)} for relative in sorted(paths)]
    dump(coding_path, coding)
    dump(HISTORY / ('current-artifact-refresh-' + stamp + '.json'), {
        'schema': 'sa13-current-artifact-refresh-v1', 'recorded_utc': snapshot,
        'source_coding_config_sha256': sha(coding_path),
        'verification_sha256': sha(APP / 'config/verification.json'),
        'scope': 'Mechanical current snapshots only. Historical raw outcomes and acceptance files are not modified.',
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--register', action='store_true')
    parser.add_argument('--catalogue', action='store_true')
    parser.add_argument('--export-schemas', action='store_true')
    parser.add_argument('--lifecycle-metadata', action='store_true')
    parser.add_argument('--refresh-artifacts', action='store_true')
    args = parser.parse_args()
    for enabled, action in ((args.register, register), (args.catalogue, catalogue),
                            (args.export_schemas, export_schemas),
                            (args.lifecycle_metadata, lifecycle_metadata),
                            (args.refresh_artifacts, refresh_artifacts)):
        if enabled:
            action()
            print(action.__name__ + ': written; no tests/SQL/runtime executed')


if __name__ == '__main__':
    main()
