"""Compose original Source/Policy/Ingress/Run commands for the Owner console."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ChannelCapabilities, SourceInstance, SourceScope
from mgar_v3.sources.feishu import CAPABILITIES as FEISHU_CAPABILITIES
from mgar_v3.sources.feishu_domain import FeishuConfigure, FeishuSettings
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.sources.github import CAPABILITIES as GITHUB_CAPABILITIES
from mgar_v3.sources.github_domain import GitHubConfigure, GitHubSettings
from mgar_v3.sources.github_store import GitHubSources
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.super_agent.policy_domain import PolicyPreview, PolicyRevoke, PolicySave
from mgar_v3.super_agent.source_console_domain import (
    ConsoleCredentialSave,
    ConsoleDisableSources,
    ConsoleSourceRevoke,
    ConsoleSourceSave,
    ConsoleTaskStop,
    credential_binding,
    credential_values,
    expiry_valid,
)
from mgar_v3.super_agent.source_console_projection import ConsoleProjection
from mgar_v3.super_agent.source_console_store import Row

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection


class SourceConsole(ConsoleProjection):
    @staticmethod
    def _cas(actual: int, expected: int) -> None:
        if actual != expected:
            raise DomainError("CONSOLE_REVISION_CONFLICT", "设置已变化，请刷新后重新确认。", 409)

    def _source_for_save(
        self, conn: Connection, identifier: UUID, request: ConsoleSourceSave
    ) -> SourceInstance | None:
        row = conn.execute(
            "SELECT project_id FROM app.source_instances WHERE source_instance_id=%s", (identifier,)
        ).fetchone()
        if row is None:
            self._cas(0, request.expected_source_revision)
            return None
        instance = self.identities.registry._instance(conn, identifier)
        self._cas(instance.revision, request.expected_source_revision)
        if instance.scope != self._scope(identifier, request):
            raise DomainError("CONSOLE_SOURCE_IDENTITY", "安装身份不可更换，请创建新的来源。", 409)
        return instance

    @staticmethod
    def _scope(identifier: UUID, request: ConsoleSourceSave) -> SourceScope:
        value = request.configuration.settings
        return SourceScope(
            source_kind=request.source_kind,
            source_instance_id=identifier,
            tenant_id=str(value.account_id)
            if isinstance(value, GitHubSettings)
            else value.tenant_key,
            installation_id=str(value.installation_id)
            if isinstance(value, GitHubSettings)
            else value.app_id,
        )

    @staticmethod
    def _platform_scope(request: ConsoleSourceSave) -> None:
        if isinstance(request.configuration.settings, FeishuSettings):
            if len(request.configuration.settings.chat_ids) != 1:
                raise DomainError("CONSOLE_SINGLE_CHAT_SCOPE", "本版请为来源选择一个研发群。", 422)

    def _save_credential(
        self,
        conn: Connection,
        identifier: UUID,
        scope: SourceScope,
        expected: int,
        encrypted: tuple[bytes, str],
        fields: list[str],
        valid_until: Any,
        user: UUID,
        review_ref: str,
    ) -> None:
        ciphertext, fingerprint = encrypted
        old = self._credential(conn, identifier)
        self._cas(old["revision"] if old else 0, expected)
        if not expiry_valid(valid_until, self._now(conn)):
            raise DomainError("CONSOLE_CREDENTIAL_EXPIRED", "凭据期限应在未来365天以内。", 422)
        if old is None:
            conn.execute(
                "INSERT INTO app.sa_console_credentials(source_instance_id,project_id,source_kind,"
                "scope_sha256,revision,ciphertext,ciphertext_sha256,field_names,valid_until,"
                "updated_by,review_ref) VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)",
                (
                    identifier,
                    self.project,
                    scope.source_kind,
                    scope.key,
                    ciphertext,
                    fingerprint,
                    fields,
                    valid_until,
                    user,
                    review_ref,
                ),
            )
        else:
            conn.execute(
                "UPDATE app.sa_console_credentials SET revision=revision+1,ciphertext=%s,"
                "ciphertext_sha256=%s,field_names=%s,valid_until=%s,updated_by=%s,review_ref=%s,"
                "revoked_at=NULL,updated_at=clock_timestamp() WHERE source_instance_id=%s "
                "AND project_id=%s AND revision=%s",
                (
                    ciphertext,
                    fingerprint,
                    fields,
                    valid_until,
                    user,
                    review_ref,
                    identifier,
                    self.project,
                    expected,
                ),
            )
        self._audit(
            conn,
            user,
            "credentials_saved",
            identifier,
            review_ref,
            credential_revision=expected + 1,
        )

    def save_source(self, cookie: str, identifier: UUID, request: ConsoleSourceSave) -> Row:
        self._platform_scope(request)
        scope = self._scope(identifier, request)
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                self._source_for_save(conn, identifier, request)
                credential = self._credential(conn, identifier)
                self._cas(
                    credential["revision"] if credential else 0,
                    request.expected_credential_revision,
                )
                if request.credentials is not None:
                    assert request.credential_valid_until is not None
                    if not expiry_valid(request.credential_valid_until, self._now(conn)):
                        raise DomainError(
                            "CONSOLE_CREDENTIAL_EXPIRED", "凭据期限应在未来365天以内。", 422
                        )
            encrypted = None
            if request.credentials is not None:
                encrypted = self.vault.encrypt(
                    credential_values(request.credentials),
                    credential_binding(
                        self.project,
                        str(identifier),
                        scope.key,
                        request.source_kind,
                        request.expected_credential_revision + 1,
                    ),
                )
            # Key files/crypto may invoke a callback; recheck Owner and all CAS facts afterwards.
            with self.engine.transaction() as conn:
                user = self._owner(conn, cookie)
                instance = self._source_for_save(conn, identifier, request)
                credential = self._credential(conn, identifier)
                self._cas(
                    credential["revision"] if credential else 0,
                    request.expected_credential_revision,
                )
                reference = "vault://source-console/" + identifier.hex
                if instance is None:
                    instance = SourceInstance(
                        scope=scope,
                        project_id=self.project,
                        display_name=request.display_name,
                        adapter_version="github-app-v1"
                        if request.source_kind == "github"
                        else "feishu-bot-v1",
                        capabilities=GITHUB_CAPABILITIES
                        if request.source_kind == "github"
                        else FEISHU_CAPABILITIES,
                        channel_capabilities=ChannelCapabilities(
                            send="unsupported",
                            idempotent_send="unsupported",
                            query_delivery="unsupported",
                            delete_message="unsupported",
                        ),
                        credential_ref=reference,
                    )
                    self.identities.registry.register_instance(instance)
                elif instance.display_name != request.display_name or (
                    instance.credential_ref is None
                    or (encrypted is not None and instance.credential_ref != reference)
                ):
                    instance = instance.model_copy(
                        update={
                            "revision": instance.revision + 1,
                            "display_name": request.display_name,
                            "credential_ref": reference
                            if encrypted is not None or instance.credential_ref is None
                            else instance.credential_ref,
                        }
                    )
                    self.identities.registry.register_instance(instance)
                if isinstance(request.configuration, GitHubConfigure):
                    saved = GitHubSources(self.engine).configure(
                        cookie, identifier, request.configuration
                    )
                else:
                    saved = FeishuSources(self.engine).configure(
                        cookie, identifier, request.configuration
                    )
                if encrypted is not None:
                    assert request.credentials is not None
                    self._save_credential(
                        conn,
                        identifier,
                        scope,
                        request.expected_credential_revision,
                        encrypted,
                        sorted(credential_values(request.credentials)),
                        request.credential_valid_until,
                        user,
                        request.configuration.review_ref,
                    )
                self._audit(
                    conn,
                    user,
                    "source_saved",
                    identifier,
                    request.configuration.review_ref,
                    source_revision=saved["source_revision"],
                    settings_revision=saved["revision"],
                )
                result = self._source_view(
                    conn, self.identities.registry._instance(conn, identifier)
                )
                return {**result, "runtime": "NOT_ACTIVATED_BY_SETTINGS"}

    def save_credentials(
        self, cookie: str, identifier: UUID, request: ConsoleCredentialSave
    ) -> Row:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                instance = self.identities.registry._instance(conn, identifier)
                self._cas(instance.revision, request.expected_source_revision)
                row = self._config(conn, instance)
                if row is None or instance.scope.source_kind not in {"github", "feishu"}:
                    raise DomainError(
                        "CONSOLE_SOURCE_NOT_CONFIGURED", "请先保存来源安装范围。", 409
                    )
                config_type = (
                    GitHubConfigure if instance.scope.source_kind == "github" else FeishuConfigure
                )
                config = config_type.model_validate(
                    {
                        "expected_revision": row["revision"],
                        "settings": row["definition"],
                        "review_ref": request.review_ref,
                    }
                )
            return self.save_source(
                cookie,
                identifier,
                ConsoleSourceSave(
                    source_kind="github" if instance.scope.source_kind == "github" else "feishu",
                    expected_source_revision=instance.revision,
                    expected_credential_revision=request.expected_credential_revision,
                    display_name=instance.display_name,
                    configuration=config,
                    credentials=request.credentials,
                    credential_valid_until=request.valid_until,
                ),
            )

    def _source_runs(self, conn: Connection, identifier: UUID) -> list[UUID]:
        rows = conn.execute(
            "SELECT b.run_id FROM app.source_run_policies b JOIN app.source_policies p USING(policy_id) "
            "WHERE b.project_id=%s AND p.source_instance_id=%s UNION "
            "SELECT run_id FROM app.sa_coding_bindings WHERE project_id=%s AND source_instance_id=%s UNION "
            "SELECT run_id FROM app.sa_autonomy_attempts WHERE project_id=%s AND source_instance_id=%s LIMIT 1001",
            (self.project, identifier, self.project, identifier, self.project, identifier),
        ).fetchall()
        if len(rows) > 1000:
            raise DomainError("CONSOLE_SCOPE_BOUND", "任务数量超出本次原子停用上限。", 409)
        return [row["run_id"] for row in rows]

    def _revoke_source(
        self, conn: Connection, cookie: str, identifier: UUID, review_ref: str, user: UUID
    ) -> Row:
        instance = self.identities.registry._instance(conn, identifier)
        runs = self._source_runs(conn, identifier)
        for policy in self._policies(conn):
            if (
                policy["definition"]["selector"]["scope"]["source_instance_id"] == str(identifier)
                and not policy["revoked"]
            ):
                self.engine.source_policies.revoke(
                    cookie,
                    policy["policy_id"],
                    PolicyRevoke(
                        expected_revision=policy["current_revision"], review_ref=review_ref
                    ),
                )
        config = self._config(conn, instance)
        if config is not None:
            body = {
                "expected_revision": config["revision"],
                "settings": {**config["definition"], "enabled": False},
                "review_ref": review_ref,
            }
            if instance.scope.source_kind == "github":
                GitHubSources(self.engine).configure(
                    cookie, identifier, GitHubConfigure.model_validate(body)
                )
            else:
                FeishuSources(self.engine).configure(
                    cookie, identifier, FeishuConfigure.model_validate(body)
                )
        else:
            self.identities.registry.register_instance(
                instance.model_copy(update={"revision": instance.revision + 1})
            )
        old = conn.execute(
            "SELECT generation,settings FROM app.source_ingress_controls "
            "WHERE source_instance_id=%s AND project_id=%s",
            (identifier, self.project),
        ).fetchone()
        if old:
            settings = IngressSettings.model_validate(old["settings"]).model_copy(
                update={"owner": "disabled", "policy_id": None, "policy_revision": None}
            )
            self.engine.source_ingress.configure(
                cookie,
                identifier,
                IngressConfigure(
                    expected_generation=old["generation"], settings=settings, review_ref=review_ref
                ),
            )
        credential = self._credential(conn, identifier)
        if credential and credential["ciphertext"] is not None:
            conn.execute(
                "UPDATE app.sa_console_credentials SET revision=revision+1,ciphertext=NULL,"
                "ciphertext_sha256=NULL,revoked_at=clock_timestamp(),updated_at=clock_timestamp(),"
                "updated_by=%s,review_ref=%s WHERE source_instance_id=%s AND project_id=%s",
                (user, review_ref, identifier, self.project),
            )
        for run_id in runs:
            self._run_context(conn, run_id)
            self.engine.cancel(run_id)
        current = self.identities.registry._instance(conn, identifier)
        self._audit(
            conn,
            user,
            "source_revoked",
            identifier,
            review_ref,
            source_revision=current.revision,
            run_count=len(runs),
        )
        return self._source_view(conn, current)

    def revoke_source(self, cookie: str, identifier: UUID, request: ConsoleSourceRevoke) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            instance = self.identities.registry._instance(conn, identifier)
            self._cas(instance.revision, request.expected_source_revision)
            return self._revoke_source(conn, cookie, identifier, request.review_ref, user)

    def disable_all(self, cookie: str, request: ConsoleDisableSources) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            if request.expected_snapshot_sha256 != self._snapshot_hash(conn):
                raise DomainError(
                    "CONSOLE_SNAPSHOT_STALE", "来源或规则已改变，请刷新后重新确认停用。", 409
                )
            identifiers = [i.scope.source_instance_id for i in self._instances(conn)]
            for identifier in identifiers:
                self._revoke_source(conn, cookie, identifier, request.review_ref, user)
            self._audit(
                conn,
                user,
                "all_sources_disabled",
                None,
                request.review_ref,
                source_count=len(identifiers),
            )
            return {
                "disabled_source_count": len(identifiers),
                "new_work_blocked": True,
                "in_flight_effects": "RECONCILE_ONLY",
                "snapshot_sha256": self._snapshot_hash(conn),
            }

    def _policy_ceiling(self, conn: Connection, request: PolicySave | PolicyPreview) -> None:
        value = request.definition
        if value.mode == "auto_repair" or set(value.execution.capabilities) & {
            "edit_files",
            "run_tests",
        }:
            raise DomainError(
                "CONSOLE_WORKER_GATE", "自动修复尚未通过能力验收，无法保存修复授权。", 409
            )
        instance = self.identities.registry._instance(conn, value.selector.scope.source_instance_id)
        self.engine.source_policies._validate_source(conn, value)
        row = self._config(conn, instance)
        if row is None:
            raise DomainError("CONSOLE_SOURCE_NOT_CONFIGURED", "请先保存来源安装范围。", 409)
        if row["source_revision"] != instance.revision:
            raise DomainError("CONSOLE_SOURCE_STALE", "来源范围已变化，请先重新确认来源设置。", 409)
        if instance.scope.source_kind == "feishu":
            settings = FeishuSettings.model_validate(row["definition"])
            conversations = set(settings.chat_ids)
            humans, bots = set(settings.sender_open_ids), set(settings.context_bot_open_ids)
            events: set[str] = set(settings.events)
            selected_events = set(value.selector.event_kinds)
        elif instance.scope.source_kind == "github":
            github = GitHubSettings.model_validate(row["definition"])
            conversations = {"repository:" + str(github.repository.repository_id)}
            humans, bots = (
                {str(v) for v in github.sender_ids},
                {str(v) for v in github.context_bot_ids},
            )
            events = set(github.events)
            selected_events = {event.split(".", 1)[0] for event in value.selector.event_kinds}
        else:
            raise DomainError("CONSOLE_SOURCE_KIND", "控制台仅支持已配置的GitHub或飞书来源。", 422)
        if (
            not set(value.selector.conversations).issubset(conversations)
            or not selected_events.issubset(events)
            or any(
                a.actor_id
                not in (
                    humans if a.actor_kind == "human" else bots if a.actor_kind == "bot" else set()
                )
                for a in value.selector.actors
            )
        ):
            raise DomainError("CONSOLE_POLICY_SCOPE", "规则超出了已选择的群、用户或事件范围。", 403)

    def save_policy(self, cookie: str, identifier: UUID, request: PolicySave) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            self._policy_ceiling(conn, request)
            result = self.engine.source_policies.save(cookie, identifier, request)
            self._audit(
                conn,
                user,
                "policy_saved",
                identifier,
                request.review_ref,
                policy_revision=result["current_revision"],
            )
            return result

    def preview_policy(self, cookie: str, identifier: UUID, request: PolicyPreview) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            result = self.engine.source_policies.preview(cookie, identifier, request)
            blocked = None
            try:
                self._policy_ceiling(conn, request)
            except DomainError as exc:
                blocked = exc.code
            return {
                **result,
                "console_capabilities": self.capabilities(),
                "save_eligible": blocked is None,
                "save_block_reason_code": blocked,
                "creates_task": False,
                "semantic_evaluation": "NOT_RUN",
            }

    def configure_ingress(self, cookie: str, identifier: UUID, request: IngressConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            if request.settings.policy_id is not None:
                row = self.engine.source_policies._current(conn, request.settings.policy_id)
                policy = PolicySave.model_validate(
                    {
                        "expected_revision": row["current_revision"],
                        "definition": row["definition"],
                        "review_ref": request.review_ref,
                    }
                )
                self._policy_ceiling(conn, policy)
                if policy.definition.selector.scope.source_instance_id != identifier:
                    raise DomainError("CONSOLE_POLICY_SCOPE", "接收规则属于另一个来源。", 403)
            result = self.engine.source_ingress.configure(cookie, identifier, request)
            self._audit(
                conn,
                user,
                "ingress_saved",
                identifier,
                request.review_ref,
                generation=result["generation"],
            )
            return result

    def stop_task(self, cookie: str, identifier: UUID, request: ConsoleTaskStop) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            run = self._run_context(conn, identifier)
            self._cas(run["execution_epoch"], request.expected_epoch)
            self.engine.cancel(identifier)
            run = self._run_context(conn, identifier)
            jobs = conn.execute(
                "SELECT count(*) AS n FROM app.sa_async_jobs WHERE project_id=%s "
                "AND run_id=%s AND state<>'known'",
                (self.project, identifier),
            ).fetchone()
            unknown = conn.execute(
                "SELECT count(*) AS n FROM app.actions WHERE run_id=%s "
                "AND status IN ('dispatching','outcome_unknown')",
                (identifier,),
            ).fetchone()
            resources = conn.execute(
                "SELECT count(*) AS n FROM app.sa_scheduler_assignments "
                "WHERE project_id=%s AND run_id=%s AND state<>'released'",
                (self.project, identifier),
            ).fetchone()
            assert jobs and unknown and resources
            self._audit(
                conn,
                user,
                "task_stopped",
                identifier,
                request.review_ref,
                execution_epoch=run["execution_epoch"],
                pending_jobs=jobs["n"],
                unknown_actions=unknown["n"],
            )
            return {
                "run": run,
                "stop_committed": True,
                "pending_jobs": jobs["n"],
                "pending_resources": resources["n"],
                "unknown_actions": unknown["n"],
                "quiescence_confirmed": jobs["n"] == 0 and resources["n"] == 0,
            }
