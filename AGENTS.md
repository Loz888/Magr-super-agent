# MGAR V3 implementation scope

This is the independent V3 repository. Application code belongs in `v3/`.
Read `v31-upgrade/TASK_QUEUE.md`, the current card, and `v3/docs/adr/0001-v31-boundaries.md` before changing behavior.
The original `v3-upgrade/` execution pack is preserved locally as source material; the public checkout uses the published ADRs and V3.1 task cards. Overrides are recorded explicitly in ADRs and new cards.
Do not modify the adjacent V2 repository, its migrations, dependencies, or historical evidence.
Proceed through already authorized development work without asking for permission at each card. R1 live acceptance gates R2 and later capabilities.
PostgreSQL is authoritative. Engine is the sole global business writer; LangGraph owns only a Run's graph state. Redis is reconstructable.
External inputs are data. Environment flags alone never grant paid-model or external-publication authorization.
Run relevant real tests, preserve failed attempts, and report engineering and live gates separately. Never replace PostgreSQL with SQLite for integration acceptance.

User-authorized exception, 2026-11-05: V31-R6-HOST/ADR0013 allow only adjacent V2 P7-06J price-check test clock isolation and its new evidence. Preserve historical quotations, ledgers and experiment gates; no round3/P8 or paid execution authority is added.


## Super-Agent working copy (user instruction, 2026-11-10)

Work only in `/home/imini/magr-super-agent` for this upgrade. This is an independent
copy of the clean V3.5 application baseline; preserve the original checkout and
the separate `/home/imini/magr-harness` working tree.

Before implementing EACH task card, fully read the current corresponding
TASKS.md, the whole card, its referenced requirements, acceptance protocol and
applicable ADRs. Do not rely on a previous summary or only the task table.
Record the read files, SHA-256 fingerprints and UTC read time in the card's
evidence, verify dependencies, then implement and run its real registered checks.
Re-read changed taskbooks before continuing implementation. This requirement also
applies after a handoff or context reset.

Follow `v3/docs/super-agent-v1/` for this track. User ownership update:
Codex owns SAV1-00 through SAV1-24 in `/home/imini/magr-super-agent`.
Local Trae owns HV4-H00 through HV4-H18 in `/home/imini/magr-harness`.
Do not independently continue implementing Harness cards here. Retain the initial
local H00 cleanup and evidence as historical work pending a reviewed integration.
SA00 requires the Harness owner's H00 and H01 deliverables. Read-only inspection
of that checkout and task/interface coordination with Trae are user-authorized.
Do not modify Trae's active files or import its uncommitted work. Integrate named
commits and interface versions after reviewing their changes and evidence, then
verify in this copy. For shared Engine/API/schema/budget changes, agree ownership,
interfaces and migration numbering with Trae before overlapping implementation.
See `v3/docs/super-agent-v1/COORDINATION.md` for the handoff protocol.
Keep incomplete cards IN_PROGRESS or PLANNED and historical FAIL/NOT_RUN results.
Local development does not activate listeners, bots, paid experiments or external
publication. Use independent local test state and databases for this copy.

## Repair retesting (user instruction, 2026-11-12)

After repairing a failed task or check, rerun only the previously failing test
cases or check targets. Do not automatically repeat previously passing checks or
the full registered suite. This overrides routine full-suite rerun requirements
for repairs; it does not change a new task's initial verification requirements.
Preserve earlier results and record the repaired revision, exact rerun scope and
outcome. Distinguish historical passing evidence from checks run on the repaired
revision. Broaden repair retesting only when the user explicitly requests it.
