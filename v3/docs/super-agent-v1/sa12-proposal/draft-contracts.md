# SA12 scheduler contracts — review draft

Status: DRAFT, migration number 0044 is proposed and unallocated. No SQL has
been installed. This document neither grants execution nor certifies Harness
H03/H04 availability. The owning Engine remains the sole business writer, using
its existing mutex, advisory-lock session and held PostgreSQL connection.

## Authority and scheduling unit

A queue row names an existing original Run, Ticket, Thread and Mandate. An
Assignment records exclusive resource occupancy for that Run; it does not
replace Run status, complete a Ticket or create an attempt. `claim` checks a
queued original Run and invokes the original `begin_run` exactly once inside
the same outer Engine transaction. The resulting original execution epoch is
the Assignment fence. A failed claim rolls back the start, quota, queue update
and Assignment together. Repeating the same claim returns the existing lease
only after rechecking its owner, request identity and current authority.

`target_sha256` is a stable authorization snapshot over the original project,
repository, Thread/Ticket/Run identities, Mandate identity/content hash, goal
hash, input revision and Thread revision. Status and execution epoch are
excluded: status is checked live and epoch has its own explicit expected/current
fence. Queue and Assignment retain the same stable target hash across the
single authorized `begin_run` transition. Hashing the entire SA10 TargetRef,
which includes the old epoch, is incompatible with this protocol.

Queue origin is either an authenticated owner request or a permanent canonical
Source input reference. Source/project/fingerprint, current policy/identity,
exact original target and Mandate are checked when enqueuing and again before
claim. No body, question, model proposal, actor display name or worker-supplied
priority becomes authority. Source TTL, edits or revocation can block a queue
entry without restoring old content or creating a replacement Task.

## Proposed persistent facts

| Table | Identity and scope | Mutable resource facts and guards |
| --- | --- | --- |
| `sa_scheduler_configs` | `project_id` | Current signed revision; disabled by default; only consecutive revision CAS. |
| `sa_scheduler_config_versions` | `(project_id,revision)` | Immutable explicit resource limits, expiry, signer and review hash; previous-revision chain. |
| `sa_scheduler_queue` | `queue_id`, `(run_id,generation)` and project/Thread/Ticket/Mandate | Current `queued/claimed/closed` resource disposition, consecutive revision; immutable origin, target hash, queue quota identity and trusted fairness classification. One open generation per Run. |
| `sa_scheduler_sessions` | `session_id`, project, immutable bearer SHA-256 and worker profile hash | Current revision, heartbeat, expiry and `registered/revoked`; no plaintext bearer. Revocation is irreversible. |
| `sa_scheduler_assignments` | `assignment_id`, queue, session, original Run/epoch and workspace | Current consecutive revision, `preparing/active/draining/released`, lease expiry, bounded CPU/memory and process-quiescence evidence; immutable binding. One occupying Assignment per project/Run, project/Ticket, project/session and project/workspace. |
| `sa_scheduler_operations` | `operation_id`, assignment/session/Run/epoch, fixed request hash | `requested/running/unknown/known_terminal` resource-operation facts with CAS. Unknown remains occupied and is reconciled, never automatically resent. Terminal result metadata has no Task completion effect. |
| `sa_scheduler_events` | Increasing `event_id`, project, optional scoped queue/session/assignment/operation | Immutable typed event/reason and metadata-only fact hash, binding each state transition to its revision. |

All original target references use the existing composite chain:
`(thread_id,project_id)`, `(ticket_id,thread_id)`, `(run_id,ticket_id)`, plus a
table-specific guard checking that the Ticket owns the referenced Mandate.
Source origins use `(input_id,source_instance_id,project_id)` plus an exact
fingerprint guard. Assignment and operation composite FKs retain their full
project/Run/session/epoch binding. Parent identity and created timestamps cannot
be edited. Fact deletion is rejected. Guards are specific to one row layout;
no CASE expression accesses columns that do not exist in another table.

## Occupancy, expiry and recovery

Execution authority and resource occupancy are separate. A lease permits work
only while configuration, registered session, original Run epoch/status,
policy/Mandate and the exact current Assignment are valid. `preparing`,
`active` and `draining` all occupy resources and retain the partial unique
Run/workspace locks. Lease expiry, session loss, config disable, Source revoke
or an epoch mismatch fences further work and enters `draining`; it does not
prove that a process has stopped and does not refund a slot.

Preparation concurrency counts both `preparing` and all `draining` Assignments
until release. This conservatively retains preparation capacity for unknown
processes, including when the previous execution stage is uncertain. An active
lease extension never extends the fixed `prepare_until` deadline. Preparing
cannot continue after that deadline even if its short lease is still valid.

`released` is permitted only with a trusted known quiescence receipt and no
pending original read Job or Scheduler operation. Pending includes requested,
running and unknown work; stop/inspection receipts cannot hide a still-live
Job. Waiting for user input may release resources after those same checks while
the original Run and its context remain available. A later execution uses an
explicit newly authorized queue generation, never automatic resurrection.
The stored quiescence time must exactly match its immutable operation receipt,
must follow the Assignment's immutable `fenced_at`, and cannot be replaced with
the current time to reuse an older observation.

Operations freeze a trusted provider kind and exact request hash before any
process/workspace call. The call runs outside the Engine transaction. Known
receipts are idempotent; unknown operations can only be inspected/reconciled.
A late receipt is stored as a resource fact, and cannot revive its expired
lease or mark the original Task successful. Worker self-report alone cannot
prove process-group quiescence or grant a new workspace.

SA07 remains the single quota authority: `queue` uses
`sa12:queue:<queue_id>:<generation>` and `slot` uses
`sa12:slot:<assignment_id>`. Reserve/release calls share the original Engine
transaction with the corresponding resource fact. A new generation or
Assignment gets a new key; a released quota key is never reused as fresh
capacity. Scheduler contains no parallel money, token or quota ledger. CPU,
memory and preparation totals are bounded additional constraints, not credits.

Fair ordering uses durable enqueue age and trusted `human/automation` class,
with bounded age promotion and a project/source fairness key derived from
verified origin. Source/CI floods cannot choose their own class or reset older
human age. Selection remains a deterministic bounded PG query; Redis may wake
the scheduler but its absence does not erase or claim work.

## Proposed methods and request shapes

All owner methods reuse current console cookie and, for HTTP writes,
Origin/CSRF. Every mutation rechecks its authority inside the held transaction.
Worker methods use a random server-issued bearer whose hash is scoped to one
project/session. The bearer is not an execution grant by itself.

| Method | Request identity | Result |
| --- | --- | --- |
| `configure` | expected config revision, explicit limits/validity, review reference | Immutable new config and current head. |
| `get_config`, `capacity` | current project | Metadata, occupied resources and blocking reasons. |
| `enqueue` | existing Run, expected epoch, current target hash, optional canonical origin, owner review | Durable queue generation and SA07 quota identity. |
| `queue`, `cancel_queue` | scoped queue ID and expected revision for cancel | Bounded queue metadata; cancellation never stops an unknown process implicitly. |
| `register_session`, `revoke_session` | owner-selected provider/profile and bounded advertised resources; expected revision for revoke | Session metadata; fresh bearer returned once at registration only. |
| `claim` | session bearer, exact idempotency key, expected config revision | At most one Assignment with original Run epoch and isolated workspace identity. |
| `heartbeat` | session bearer, Assignment ID, original epoch, expected Assignment revision | A bounded lease extension after full current checks. |
| `ready` | same fence and trusted preparation operation receipt | Preparing becomes active; does not report Task success. |
| `drain` | owner request or current worker fence, fixed reason | Revoke new execution and retain occupancy. |
| `reconcile` | scoped exact operation ID/request hash and trusted provider observation | Record known or still-unknown resource outcome without resend. |
| `release` | Assignment CAS, immutable quiescence receipt, pending-Job check | Release occupancy once and release its SA07 slot key. |
| `assignments`, `get_assignment` | current project, bounded limit | Metadata-only resource state and exact fencing epoch. |

Worker DTOs contain `assignment_id`, `session_id`, `epoch`,
`expected_revision` and an exact operation identity when applicable. They accept
no arbitrary command, path, source body, Task outcome, priority, config limits
or new target. JSON contracts use strict bounded integers, UUIDs, UTC times,
fixed enums and SHA-256 values. Session secrets are excluded from repr, logs,
events, ordinary reads and persisted JSON.

Root reviewed hard bounds: at most two occupied Assignments; preparing slots
cannot exceed active slots; aggregate CPU at most 4,000 millicores and memory at
most 4,096 MiB. Each Assignment requests 100–2,000 millicores and 64–2,048 MiB.
The durable queue is at most 256 entries; per-source slots are at most two.
Lease duration defaults to 30 seconds and is at most 120 seconds; a registered
session lasts at most 86,400 seconds. Preparation has a separate whole-operation
deadline, default 120 seconds and at most 600 seconds. Heartbeat only extends an unexpired lease,
and cannot cross session/config expiry or revive an old epoch. Disabled is the
initial state; positive SA07 quotas and registered provider capabilities are
independently required. H03/H04 resource operations are reused through fixed
capabilities, not inferred from installed packages.

Domain request names agreed with the contract owner are
`SchedulerConfigureRequest`, `SchedulerEnqueueRequest`,
`SchedulerSessionRegisterRequest`, `SchedulerSessionRevokeRequest`,
`SchedulerQueueCancelRequest`, `SchedulerClaimRequest`,
`SchedulerAssignmentRequest` and `SchedulerOperationRequest`, with named
heartbeat/ready/release/drain/report/reconcile variants. Limits remain a
nested strict `SchedulerLimits` DTO; SQL expands them into named columns.
`ResourceRequest` contains only bounded CPU/memory. `source_input` is an
optional `CorrelationInputRef`; classification is not a client field.

## SQL column mapping for the implementation owner

`SchedulerLimits` keys map to config version columns as follows:
`slots→active_slots`, `preparing→preparing_slots`,
`cpu_millis→total_cpu_millis`, `memory_mib→total_memory_mib`,
`queue→max_queue`, `source_slots→per_source_slots`;
`lease_seconds`, `preparation_seconds` and `session_ttl_seconds` keep their names.
All capacity defaults are zero, configuration is disabled, lease defaults to
30 seconds and preparation defaults to 120 seconds. The current-head row starts
at revision zero and must append version one and advance its head in the same
transaction, because its head FK is deferred and mandatory at commit.

Queue `request_id/request_sha256` bind owner idempotence; `generation` advances
once per new explicit admission. `expected_epoch` is checked before claim;
Assignment `execution_epoch` is exactly that value plus one after original
`begin_run`. Queue Source fields map to `CorrelationInputRef.input_id`,
`source_instance_id`, current row `project_id`, `version_fingerprint` and
`state_sha256→source_state_sha256`; all four nullable origin columns are either
present together or absent together. Queue `state` is `queued/claimed/closed`;
an immutable `assignment_id` and `claimed_at` appear at claim. Closing a queue
does not imply that its Assignment has released resources.

Session columns include registration `request_id/request_sha256`, immutable
`token_sha256`, `profile_sha256`, owner/review hashes, `registered_at`, fixed
`expires_at`, current `last_seen_at/revoked_at` and revision. Assignment contains
the full original target identity, fixed session/config/queue/workspace binding,
`request_id/request_sha256`, `target_sha256`, `execution_epoch`, revision,
`workspace_id:uuid`, `workspace_sha256`, bounded resource columns,
`lease_until`, immutable `prepare_until`, `last_heartbeat_at`, `fenced_at`,
`quiescence_operation_id/quiescence_sha256/quiescent_at`, `released_at`,
`slot_quota_key` and created/updated times. Workspace UUID is stringified when
calling the existing Harness string workspace contract; no arbitrary path is
accepted from a Worker.

Operation identity freezes original Run/session/Assignment/epoch, kind
`prepare/start/job/stop/inspect` and exact `request_sha256`. Result columns are
`provider_operation_id`, `result_code` (`succeeded/failed/cancelled/absent`),
`result_sha256`, optional `exit_code`, `quiescent` and `observed_at`. Only a
known terminal stop/inspect receipt may set `quiescent`; its backend proof must
include both stopped processes and a fenced workspace. Unknown and running
rows remain pending. Each operation update advances revision once; a terminal
result and a fixed provider identity cannot be edited. Operation success is
resource evidence, never an original Run completion.

Events contain scoped optional IDs, fixed `event_type/reason_code`, the entity
revision and bounded `details`. SQL limits details keys to state/epoch/resource
counts, lease time, operation/profile hashes, config revision, queue generation,
trusted class/fairness hash and quiescence. The service must emit strict typed
metadata, never arbitrary user/Worker dictionaries. It appends an event in the
same transaction as every resource transition, including rejected/fenced
outcomes where that transaction is intentionally committed.

The SQL draft intentionally does not call SA07 functions, mutate the original
Run, start a process or claim a real workspace. Those service actions require
the coordinated Engine/Harness integration and registered runtime evidence.

## Verification scope to register after implementation

Real PG concurrency/rollback and process boundaries must cover duplicate claim,
single epoch increment, expired session/lease and stale worker reports,
different workspace identities, retained slots for unknown or pending work,
safe waiting release, no Redis reconstruction loss, bounded preparation and
CPU/memory, SA07 shared queue/slot limits, Source revoke/edit and human fairness.
Schema and route presence do not replace those runtime checks. All live
platform, paid, outbound, publication and long-running gates remain NOT_RUN.
