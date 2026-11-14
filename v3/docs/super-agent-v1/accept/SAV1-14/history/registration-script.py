from pathlib import Path
from datetime import datetime,UTC
import hashlib,importlib,json,re,subprocess,sys,xml.etree.ElementTree as ET
A=Path('/home/imini/magr-super-agent/v3');R=A.parent;B=A/'docs/super-agent-v1';D=B/'accept/SAV1-14';S=json.loads((A/'.local/sa14-state.json').read_text());E=Path(S['integration_evidence']);now=datetime.now(UTC).isoformat()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p):return json.loads(p.read_text())
def save(p,j):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n')
def rel(p):return str(p.relative_to(A))
def git(*args):return subprocess.check_output(['git',*args],cwd=R)
# All tests must have completed before any tracked metadata mutation.
last=load(E/'failed5-plus-atomic-first-4/summary.json');assert last['results']['registered']['exit_code']==0
assert all(last['cleanup'][k] for k in ['source_stable','main_unchanged','own_container_absent'])
assert git('rev-parse','HEAD').decode().strip()=='be489f7a12f1ffe0e263cfac07a5b1c2b83abb91'
# Freeze pre-SA14 metadata/catalogue bytes; historical acceptance records stay untouched.
H=D/'history/pre-sa14';snap=[]
oldcat=['tests/test_super_agent_sa05.py',*[f'tests/test_super_agent_sa{n:02}_migration.py' for n in range(6,14)]]
configs=sorted(p for p in (A/'config').glob('*.json') if isinstance(load(p),dict) and ('artifacts' in load(p) or p.name=='verification.json'))
for path in [*configs,*(A/p for p in oldcat)]:
 relative=rel(path);target=H/relative;target.parent.mkdir(parents=True,exist_ok=True)
 raw=git('show','c49cf1f:v3/'+relative);assert not target.exists();target.write_bytes(raw)
 snap.append({'path':relative,'sha256':hashlib.sha256(raw).hexdigest(),'preserved_path':rel(target),'baseline':'c49cf1f'})
save(D/'history/pre-sa14-snapshot.json',{'UTC':now,'files':snap,'scope':'Original pre-SA14 metadata and current-catalogue assertions; not historical outcome changes'})
# Mechanical catalogue append only; these prior PASS checks are not rerun.
changes=[]
for relative in oldcat:
 p=A/relative;before=p.read_text();lines=before.splitlines(keepends=True);out=[]
 for line in lines:
  out.append(line)
  if re.fullmatch(r'\s*"0046_source_coding\.sql",\s*\n',line):out.append(re.match(r'\s*',line).group(0)+'"0047_source_async.sql",\n')
 value=''.join(out)
 for name in ['checksums','sources','source','expected']:value=value.replace('len('+name+') == 43','len('+name+') == 44')
 if value!=before:
  p.write_text(value);changes.append({'path':relative,'before_sha256':hashlib.sha256(before.encode()).hexdigest(),'after_sha256':sha(p)})
save(D/'history/mechanical-catalogue-update.json',{'UTC':now,'scope':'Current catalogue43→44 and forward0047 only; historical43 SQL/old outcomes unchanged, no rerun of prior PASS cases','files':changes})
# Export current strict metadata contracts without running workers or providers.
sys.path.insert(0,str(A/'src'));domain=importlib.import_module('mgar_v3.super_agent.source_async_domain')
models={'admit':'AsyncAdmitRequest','restriction':'AsyncRestrictRequest','worker':'AsyncWorkerRequest','job-cancel':'AsyncJobCancelRequest','worker-job-cancel':'AsyncWorkerJobCancelRequest','checkpoint':'AsyncCheckpoint','input':'AsyncInputReceipt','job':'AsyncJob'}
for name,model in models.items():save(A/f'schemas/super-agent-v1/source-async-{name}-v1.schema.json',getattr(domain,model).model_json_schema())
ready='LOCAL_ENGINEERING_READY_PENDING_INDEPENDENT_REVIEW'
p=A/'config/source-async-v1.json';j=load(p);j.update({'status':ready,'installed_migration_count':44,'scope_gate':'PASS_VERSIONED_LOCAL_CONTROLLED_FIXTURE_ONLY','independent_review':'NOT_RUN','functional_candidate':'be489f7a12f1ffe0e263cfac07a5b1c2b83abb91','distinct_formal_cases_with_PASS':62,'single_final_full_suite_PASS_claimed':False,'passed_cases_repeated':0,'historical_preparation_repeated':False,'acceptance_summary':'docs/super-agent-v1/accept/SAV1-14/accept.json','versioned_static_note':'lint/mypy278 PASS at5758b31; new boundary lint first PASS atbe489f7. Later repair code does not repeat previously passing static targets per user instruction.'});j['six_gates']['engineering_gate']='PASS_VERSIONED_LOCAL_CONTROLLED_SCOPE';save(p,j)
p=A/'config/verification.json';j=load(p);j['SAV1-14']['components'].update({'engineering_gate':'PASS_VERSIONED_LOCAL_CONTROLLED_SCOPE','single_final_full_suite_PASS_claimed':False,'atomic_boundary':'New first check: Owner restriction commits after first physical file replace; remaining original batch finishes and original receipt becomes known; next incompatible write blocked.'});save(p,j)
p=B/'plan.json';j=load(p);t=next(t for t in j['tasks'] if t['id']=='SAV1-14');t.update({'status':'IN_PROGRESS','work_state':ready,'formal_integration':'COMPLETE_LOCAL_CONTROLLED_VERSIONED','ownership_commit':'c711d54504c4945625505f47e9df56e22bc1bdac','engineering_gate':'PASS_VERSIONED_LOCAL_CONTROLLED_SCOPE','independent_review':'NOT_RUN','formal_distinct_cases_PASS':62,'remaining_failed_cases':0,'SA15':'NOT_STARTED','acceptance':'accept/SAV1-14/accept.json'});save(p,j)
p=B/'coordination-state.json';j=load(p);j.update({'updated_at':now,'work_state':'SA14_'+ready,'card_status':'IN_PROGRESS','sa15':'NOT_STARTED'});j['SA14_shared_scope']={'status':ready,'commit':'c711d54504c4945625505f47e9df56e22bc1bdac','section':39,'migration':'0047_source_async.sql','migration_count':44,'sql_sha256':'9ce372e6a82cbf747c51624486ef7548faf056cc60912e0287279be8b569f041','three_file_patch_sha256':'c43e05c0d8aabe1bccf3484cbb7922c5f6f1897f6d2cd5ac0840b4454ff85b5e','shared_after_hashes':'EXACT','interfaces':12,'new_Harness_imports':[],'H12_acceptance_owner':'other CLI','engineering_gate':'PASS_VERSIONED_LOCAL_CONTROLLED_SCOPE','independent_review':'NOT_RUN','real_capabilities':'NOT_RUN','SA15':'NOT_STARTED'};save(p,j)
for name in ['TASKS.md','README.md']:
 p=B/name;s=p.read_text().replace('SA14 IN_PROGRESS（第39节已确认，正式接线与异步集成实现／首次检查中）','SA14 IN_PROGRESS（本地受控工程已闭环，等待独立验收）');s+='''\nSA14当前：第39节确认c711d545，正式0047六表／三文件最小接线／12路由及异步原Job已交付。
首次正式61项26PASS35FAIL；定向修复原29项29PASS、原6项1PASS5FAIL，最后原5项及新增原子编辑边界共6PASS。
两个单失败诊断轮的超时保留。62个正式独特目标均有分版本PASS，未重跑通过项；原65／29准备证据不重跑。
[本地工程交付](SA14_ACCEPTANCE_20261114.md)／[逐例摘要](accept/SAV1-14/accept.json)。
整卡仍IN_PROGRESS、独立NOT_RUN，SA15不启动；H12由另一CLI验收，本副本固定真实门FAIL保留，其他真实门NOT_RUN。\n''';p.write_text(s)
p=B/'IMPLEMENTATION_STATUS.md';s=p.read_text();p.write_text('''# 当前实施状态（2026-11-14）

SA00–SA13 已获用户接受独立本地受控闭环。SA14 本地受控工程已闭环，等待独立验收，
保持 IN_PROGRESS；SA15–SA24 PLANNED。62 个正式目标有分版本通过证据，已通过项未重跑。
见 [SA14交付](SA14_ACCEPTANCE_20261114.md)。真实平台／付费／外发／发布／长期门仍NOT_RUN；
本副本固定H12真实Agent／MVP仍FAIL，H12验收由另一CLI推进。

以下保留此前阶段说明，以当前状态和各卡独立关闭记录为准。

'''+s)
(B/'SA14_ASYNC.md').write_text('''# SA14 异步输入与 Job

状态：本地受控工程已闭环，等待独立验收；整卡 IN_PROGRESS，SA15未启动。
[工程交付](SA14_ACCEPTANCE_20261114.md) · [分版本成绩](accept/SAV1-14/accept.json) · [独立核验请求](SA14_INDEPENDENT_REQUEST.txt)。

原 Engine 持锁 PG 连接管理同一 Thread／Ticket／Run／Mandate／Assignment 上的输入和原资源 Job。
新输入按 revision 持久化为 received／applied／blocked；只保存 Source 引用，不另存正文。
Owner 的有类型 deny 或可信当前人类精确 Ticket 控制只能收紧原许可；CI、上下文、引用不授控制权。
安全点形成不可变视图，实际模型／工具／编辑／命令边界再验当前权威，完整丢弃旧模型回复。
受控模型 prepare／短 dispatch／poll 分开，命令复用原 bound Broker，等待期间不占 Engine 锁或SQL事务。
原子文件批次收尾保留真实树和原回执，新限制阻止后续不兼容操作；在途费用继续原账本真实结算。

第39节 c711d545 已确认并按原字节应用0047和三文件hook。正式44份迁移，12个 metadata/no-store 路由；
当前受控范围仍只支持既有公开fixture_git、纯函数Python及原H03／SA12 bound H06／固定H11。
没有引入H09文件Store／Inbox、另一Task或钱包，也没有导入H12。默认无Runtime／Worker／监听或后台线程。

正式首轮61项26PASS35FAIL；后续仅失败项复验，新增原子编辑边界首次通过。62个独特目标跨版本闭环，
并非最终源码单轮62PASS。私有65／SQL草案29的历史证据与所有失败保留。等待独立验收，不标DONE。
H12验收由另一CLI负责；本副本固定H12真实门FAIL、其他真实／付费／外发／发布／长期门NOT_RUN。

[冻结范围提案](sa14-proposal/SA14_SCOPE.md)和[提案哈希](sa14-proposal/proposal-manifest.json)保留原字节；
提案中的“等待确认”是当时状态，实际确认见[第39节固定记录](sa14-proposal/ownership-confirmed.json)。
''')
# Refresh current per-file manifests in dependency order, preserving all old records.
configs={rel(p):load(p) for p in (A/'config').glob('*.json') if isinstance(load(p),dict) and isinstance(load(p).get('artifacts'),list)}
ordered=[];active=set();done=set()
def visit(name):
 if name in done:return
 assert name not in active,name
 active.add(name)
 for v in configs[name]['artifacts']:
  if v['path'] in configs:visit(v['path'])
 active.remove(name);done.add(name);ordered.append(name)
for name in configs:visit(name)
for name in ordered:
 value=configs[name];changed=False
 for artifact in value['artifacts']:
  actual=sha(A/artifact['path'])
  if actual!=artifact['sha256']:artifact['sha256']=actual;changed=True
 if changed:
  value.update({'artifact_snapshot_utc':now,'artifact_snapshot_scope':'Current SA14 composition/catalogue hashes only; pre-SA14 bytes preserved in accept/SAV1-14/history/pre-sa14. Historical PASS/FAIL and independent closure records unchanged; no rerun implied.','artifact_status':'CURRENT_FORMAL_HASHES'})
  save(A/name,value)
p=A/'config/source-async-v1.json';j=load(p);paths={'migrations/0047_source_async.sql','config/verification.json','config/source-coding-v1.json','config/source-scheduler-v1.json','src/mgar_v3/coordination/engine.py','src/mgar_v3/api.py','src/mgar_v3/sources/ingress.py','tests/sa14_support.py','tests/sa14_crash_actor.py',*oldcat}
for glob in ['src/mgar_v3/super_agent/source_async*.py','src/mgar_v3/super_agent/source_coding*.py','tests/test_super_agent_sa14*.py','schemas/super-agent-v1/source-async*.json','docs/super-agent-v1/sa14-proposal/*']:
 paths.update(rel(p) for p in A.glob(glob) if p.is_file())
j['artifacts']=[{'path':name,'sha256':sha(A/name)} for name in sorted(paths)];j['artifact_snapshot_utc']=now;j['artifact_snapshot_scope']='Current source/contract/test/approved shared hashes; not a historical outcome rewrite or new capability gate.';save(p,j)
save(E/'metadata-followup.json',{'UTC':now,'functional_candidate':'be489f7','mechanical_catalogue_changes':changes,'schemas_exported':list(models),'historical_outcomes_repeated':False})
files=load(E/'reading-before-formal-implementation.json')['files'];save(E/'reading-after-final-statebook-update.json',{'UTC':now,'files':[{'path':f['path'],'sha256':sha(R/f['path'])} for f in files],'scope':'Reread current books for final metadata handoff; no further behavior changes'})
S.update({'work_state':ready,'functional_source_candidate':'be489f7a12f1ffe0e263cfac07a5b1c2b83abb91','functional_source_sha256':last['source_sha256'],'remaining_failed_cases':0,'independent_review':'NOT_RUN'});save(A/'.local/sa14-state.json',S)
print(json.dumps({'work_state':ready,'old_catalogue_files_updated':len(changes),'schemas':len(models),'old_static_PASS_not_repeated':True,'metadata_only_followup':True},indent=2))
