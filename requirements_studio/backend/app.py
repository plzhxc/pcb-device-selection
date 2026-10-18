from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .codex import EFFORT, MODEL, MODEL_LABEL, SERVICE_TIER, Runner
from .domain import Category, Priority, State
from .store import Conflict, Missing, Store
from .module_catalog import summary as catalog_summary, detail as catalog_detail, TYPES
from .domain_store import identity_input
from .knowledge import KnowledgeUnavailable
from .handoff import readiness, build_spec, generate_archive, generation_adapter, legacy_document

ROOT = Path(__file__).resolve().parents[1]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProject(Body):
    example: bool = False


class SendMessage(Body):
    text: str = Field(min_length=1,max_length=16000)
    request_id: str = Field(min_length=1,max_length=80)
    answers: list["QuestionAnswer"] = Field(default_factory=list,max_length=30)
    after_run_id: str | None = Field(default=None,max_length=64)
    actions: list['DomainAction'] = Field(default_factory=list,max_length=10)


class DomainAction(Body):
    operation: Literal['set_intent','select_identity','remove_reference','add_reference']
    reference_id: str = Field(default='',max_length=64)
    intent: Literal['must','prefer','reference','undecided'] | None = None
    device_key: str | None = Field(default=None,max_length=100)
    expected_identity_hash: str = Field(default='',max_length=64)
    mention: str = Field(default='',max_length=160)
    purpose: str = Field(default='',max_length=1000)
    module_ids: list[str] = Field(default_factory=list,max_length=80)
    requirement_ids: list[str] = Field(default_factory=list,max_length=80)
    quantity: int | None = Field(default=None,ge=1,le=100000)


class ReferenceAction(Body):
    base_version: int = Field(ge=0)
    request_id: str = Field(min_length=1,max_length=80)
    action: DomainAction


class KnowledgeJobRequest(Body):
    request_id: str = Field(min_length=1,max_length=80)
    reference_ids: list[str] = Field(default_factory=list,max_length=80)
    refresh: bool = False


class HandoffRequest(Body):
    base_version: int = Field(ge=0)
    base_domain_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1,max_length=80)
    allow_draft: bool = False


class QuestionAnswer(Body):
    question_id: str = Field(min_length=1,max_length=64)
    answer: str = Field(min_length=1,max_length=2000)
    deferred: bool = False


class Retry(Body):
    request_id: str = Field(default_factory=lambda:uuid.uuid4().hex,max_length=80)


class EditRequirement(Body):
    base_version: int = Field(ge=0)
    id: str = Field(default="",max_length=32)
    category: Category
    name: str = Field(min_length=1,max_length=160)
    value: str = Field(max_length=3000)
    unit: str = Field(default="",max_length=40)
    priority: Priority
    state: State
    implementation: Literal['hardware','software','service','system'] = 'hardware'


class Rename(Body):
    title: str = Field(min_length=1,max_length=120)
    base_version: int = Field(ge=0)


class Approve(Body):
    base_version: int = Field(ge=0)
    base_domain_revision: int | None = Field(default=None,ge=0)


class EditModule(Body):
    base_version: int = Field(ge=0)
    id: str = Field(default='',max_length=64)
    type_id: str = Field(max_length=64)
    name: str = Field(min_length=1,max_length=160)
    purpose: str = Field(min_length=1,max_length=1000)
    boundary: str = Field(max_length=1500)
    quantity: int = Field(ge=1,le=1000)
    state: Literal['confirmed','design_stage','tbd','not_applicable']
    requirement_ids: list[str] = Field(min_length=1,max_length=80)


def create_app(data_dir: Path | None = None, knowledge_provider=None):
    runtime = data_dir or Path(os.environ.get("REQUIREMENTS_STUDIO_DATA",str(ROOT/".runtime")))
    store = Store(runtime/"studio.sqlite3")
    runner = Runner(store,runtime)
    if knowledge_provider is not None:runner.knowledge.provider=knowledge_provider
    catalog=runner.knowledge.provider
    search_limit=asyncio.Semaphore(4)
    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        await runner.close()
    app = FastAPI(title="PCBAgent Requirements Studio",version="0.3.0",lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner

    @app.exception_handler(Conflict)
    async def conflict(_,exc):
        return JSONResponse({"detail":str(exc)},status_code=409)

    @app.exception_handler(Missing)
    async def missing(_,exc):
        return JSONResponse({"detail":str(exc)},status_code=404)

    @app.exception_handler(KnowledgeUnavailable)
    async def knowledge_failure(_,exc):
        return JSONResponse({'detail':str(exc),'status':'unavailable'},status_code=503)

    @app.exception_handler(sqlite3.Error)
    async def storage_failure(_,exc):
        logging.getLogger('requirements.storage').error('SQLite %s: %s',getattr(exc,'sqlite_errorname',type(exc).__name__),exc)
        return JSONResponse({'detail':'本地存储暂时无法读写，已保存文档保留。请稍后重试。'},status_code=503)

    @app.middleware("http")
    async def local_mutations(request: Request,call_next):
        # Same-origin local application; do not allow a remote webpage to trigger CLI jobs.
        origin = request.headers.get("origin")
        if request.method in ("POST","PATCH","DELETE") and origin:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host") and origin not in ("http://127.0.0.1:5178","http://localhost:5178","http://[::1]:5178"):
                return JSONResponse({"detail":"请从工作台页面执行此操作。"},status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        return {"ok":True,"app":"requirements-studio","model":MODEL,"effort":EFFORT,"service_tier":SERVICE_TIER,"model_label":MODEL_LABEL,"cli_available":bool(shutil.which("codex")),"version":"0.3.0",'backend_build':runner.release_hash}

    @app.get("/api/projects")
    async def projects():
        return store.list_projects()

    @app.get('/api/module-catalog')
    async def module_catalog():
        return catalog_summary()

    @app.get('/api/module-catalog/{type_id}')
    async def module_type(type_id: str):
        try:return catalog_detail(type_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc

    @app.post('/api/projects/{project_id}/modules')
    async def edit_module(project_id: str,body: EditModule):
        data=body.model_dump();version=data.pop('base_version')
        if not data['name'].strip() or not data['purpose'].strip():raise HTTPException(422,'请填写模块名称及用途。')
        return store.edit_module(project_id,version,data)

    @app.delete('/api/projects/{project_id}/modules/{module_id}')
    async def remove_module(project_id: str,module_id: str,base_version: int):
        return store.edit_module(project_id,base_version,{'id':module_id},remove=True)

    @app.post("/api/projects",status_code=201)
    async def new_project(body: CreateProject):
        return store.create_project(body.example)

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        return runner.project(project_id)

    @app.patch("/api/projects/{project_id}")
    async def rename(project_id: str,body: Rename):
        title=body.title.strip()
        if not title:
            raise HTTPException(422,"项目名称不能为空。")
        return store.rename(project_id,title,body.base_version)

    @app.post("/api/projects/{project_id}/approve")
    async def approve(project_id: str,body: Approve):
        return store.approve(project_id,body.base_version,body.base_domain_revision)

    @app.post("/api/projects/{project_id}/messages",status_code=202)
    async def send(project_id: str,body: SendMessage):
        await runner.reconcile(project_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(422,"请输入需求内容。")
        project = store.get_project(project_id)
        question_ids = {q["id"] for q in project["document"]["questions"]+project["document"].get("question_history",[])}
        if any(a.question_id not in question_ids or a.answer not in text for a in body.answers):
            raise HTTPException(422,"回答需要对应已存在的问题，并包含在发送内容中。")
        if body.actions:raise HTTPException(422,'器件操作请使用器件引用接口，以保存明确的操作依据。')
        run,created,queued = store.submit_message(project_id,text,body.request_id,[a.model_dump() for a in body.answers],body.after_run_id)
        if run:runner.start(run["id"])
        return {"run":run,"queued":bool(queued),"queue_item":queued,"project":store.get_project(project_id)}

    @app.delete("/api/projects/{project_id}/queue/{queue_id}")
    async def remove_queued(project_id: str,queue_id: str):
        store.remove_queued(project_id,queue_id)
        return store.get_project(project_id)

    @app.post("/api/projects/{project_id}/queue/resume",status_code=202)
    async def resume_queued(project_id: str):
        await runner.reconcile(project_id)
        run = store.resume_queue(project_id)
        runner.start(run["id"])
        return store.get_project(project_id)

    @app.post("/api/projects/{project_id}/retry",status_code=202)
    async def retry(project_id: str,body: Retry):
        await runner.reconcile(project_id)
        run,created = store.begin_run(project_id,"",body.request_id,retry=True)
        runner.start(run["id"])
        return {"run":run,"project":store.get_project(project_id)}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = store.get_run(run_id)
        await runner.cancel(run_id)
        return store.get_project(run["project_id"])

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str,request: Request,last_event_id: Annotated[str|None,Header()]=None):
        store.get_run(run_id)
        try:
            after = max(0,int(last_event_id or request.query_params.get("after","0")))
        except ValueError:
            after = 0
        async def stream():
            cursor = after
            ticks = 0
            while not await request.is_disconnected():
                if run_id in runner.failed_runs:
                    yield 'data: '+json.dumps({'kind':'failed','message':runner.failed_runs[run_id]},ensure_ascii=False)+'\n\n'
                    break
                try:
                    rows = store.events(run_id,cursor)
                    status=store.get_run(run_id)['status']
                except sqlite3.Error:
                    yield 'data: '+json.dumps({'kind':'reconnecting','message':'正在恢复读取进度；已提交的输入保留。'},ensure_ascii=False)+'\n\n'
                    await asyncio.sleep(1)
                    continue
                for row in rows:
                    cursor = row["seq"]
                    payload = {"kind":row["kind"],**row["payload"]}
                    yield f"id: {cursor}\ndata: {json.dumps(payload,ensure_ascii=False)}\n\n"
                if status not in ("queued","running"):
                    break
                ticks += 1
                if ticks % 20 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no"})

    @app.patch("/api/projects/{project_id}/requirements")
    async def edit(project_id: str,body: EditRequirement):
        return store.edit_requirement(project_id,body.base_version,body.model_dump(exclude={"base_version"}))

    @app.get("/api/projects/{project_id}/export/{kind}")
    async def export(project_id: str,kind: str):
        project = store.get_project(project_id)
        if kind not in ("md","json","studio-json","requirementspec"):
            raise HTTPException(404,"不支持的导出格式。")
        extension='md' if kind=='md' else 'json'
        filename=quote(f"{project['title']}_需求说明书_v{project['document']['version']}.{extension}",safe="")
        content = project['markdown'] if kind=='md' else json.dumps(build_spec(project) if kind=='requirementspec' else
                    {'schema_version':'requirements-studio/2','project_id':project_id,'document':project['document'],'domain':project['domain'],'messages':project['messages']} if kind=='studio-json' else
                    {'schema_version':'requirements-studio/1','project_id':project_id,'document':legacy_document(project['document'])},ensure_ascii=False,indent=2)
        return Response(content,media_type="text/markdown; charset=utf-8" if kind=="md" else "application/json",headers={"X-Document-Version":str(project['document']['version']),"Content-Disposition":f"attachment; filename=\"requirements-v{project['document']['version']}.{kind}\"; filename*=UTF-8''{filename}"})

    @app.get('/api/knowledge/status')
    async def knowledge_status(refresh: bool=False):
        async with search_limit:return await asyncio.to_thread(catalog.status,refresh)

    @app.get('/api/catalog/search')
    async def catalog_search(q: str='',mode: str='auto',limit: int=8,offset: int=0,type_id: str=''):
        from .device_domain import module_mapping
        if type_id and type_id not in TYPES:
            raise HTTPException(422,'请选择规范目录中的模块类别。')
        leaves=module_mapping(type_id).get('leaf_codes',[]) if type_id else None
        try:
            async with search_limit:return await asyncio.to_thread(catalog.search,q,mode,limit,offset,leaves)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc

    @app.get('/api/catalog/devices/{device_key}')
    async def catalog_device(device_key: str):
        try:
            async with search_limit:return await asyncio.to_thread(catalog.device,device_key)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc

    async def reference_action(project_id,body):
        from .device_domain import INTENTS
        await runner.reconcile(project_id)
        p=store.get_project(project_id)
        # Idempotent replays remain valid even after the original action changed the version.
        with store.connect() as db:
            old=db.execute('SELECT id FROM runs WHERE project_id=? AND request_id=?',(project_id,body.request_id)).fetchone()
            queued=db.execute('SELECT id FROM input_queue WHERE project_id=? AND request_id=?',(project_id,body.request_id)).fetchone()
        if old or queued:
            if old:runner.start(old['id'])
            return {'project':p,'queued':bool(queued),'replayed':True}
        if p['document']['version']!=body.base_version:raise Conflict('需求已更新，请查看当前器件后再操作；选择草稿保留。')
        action=body.action.model_dump(exclude_none=True)
        refs={r['id']:r for r in p['document'].get('device_references',[]) if r.get('lifecycle')=='active'}
        ref=refs.get(action['reference_id'])
        if action['operation']!='add_reference' and not ref:raise Missing('器件引用不存在或已撤销。')
        if ref:action['expected_identity_hash']=identity_input(ref)
        if action['operation'] in ('select_identity','add_reference'):
            if not action.get('device_key'):raise HTTPException(422,'请选择真实目录身份。')
            async with search_limit:detail=await asyncio.to_thread(catalog.device,action['device_key'])
            identity=detail['identity']
            if action['operation']=='select_identity' and ref['identity_scope']=='exact_part':
                from .knowledge import normalize
                if normalize(ref['mention']) not in {normalize(identity['model']),normalize(identity.get('supplier_part',''))}:
                    raise HTTPException(422,'这条引用指定了 '+ref['mention']+'，所选记录是另一个型号。若要换型，请在对话中明确修改器件要求。')
        if action['operation']=='add_reference':
            action['mention']=identity['model']
            if not action.get('intent'):raise HTTPException(422,'请明确器件使用意图。')
            text=f"我将「{identity['model']}」作为{INTENTS[action['intent']]}的器件引用，作用：{action.get('purpose') or '参与本项目设计'}。目录身份：{identity['device_key']}。这项操作确认引用意图，不表示工程验证通过。"
        elif action['operation']=='select_identity':
            text=f"我为{ref['id']}「{ref['mention']}」明确选择目录身份「{identity['model']} / {identity.get('manufacturer','')} / {identity.get('supplier_part','')}」，身份键{identity['device_key']}。使用意图仍为{INTENTS[ref['intent']]}；其他工程条件继续核对。"
        elif action['operation']=='set_intent':
            if action.get('intent') not in INTENTS:raise HTTPException(422,'请明确新的使用意图。')
            text=f"将{ref['id']}「{ref['mention']}」的使用意图改为{INTENTS[action['intent']]}。保留其他产品要求，按这项修改重新核对相关约束。"
        else:text=f"撤销{ref['id']}「{ref['mention']}」的器件引用；保留其他需求，重新核对相关选型约束。"
        run,created,item=store.submit_message(project_id,text,body.request_id,actions=[action])
        if run:runner.start(run['id'])
        return {'project':store.get_project(project_id),'queued':bool(item),'run':run,'replayed':False}

    @app.post('/api/projects/{project_id}/device-references',status_code=202)
    async def add_reference(project_id: str,body: ReferenceAction):
        return await reference_action(project_id,body)

    @app.patch('/api/projects/{project_id}/device-references/{reference_id}',status_code=202)
    async def update_reference(project_id: str,reference_id: str,body: ReferenceAction):
        if body.action.reference_id!=reference_id:raise HTTPException(422,'器件引用编号不一致。')
        return await reference_action(project_id,body)

    @app.delete('/api/projects/{project_id}/device-references/{reference_id}',status_code=202)
    async def remove_reference(project_id: str,reference_id: str,body: ReferenceAction):
        if body.action.reference_id!=reference_id or body.action.operation!='remove_reference':raise HTTPException(422,'请选择要撤销的器件引用。')
        return await reference_action(project_id,body)

    @app.post('/api/projects/{project_id}/knowledge-jobs',status_code=202)
    async def knowledge_job(project_id: str,body: KnowledgeJobRequest):
        doc=store.raw_document(project_id)
        refs=[r for r in doc.get('device_references',[]) if r.get('lifecycle')=='active' and (not body.reference_ids or r['id'] in body.reference_ids)]
        if set(body.reference_ids)-{r['id'] for r in refs}:raise HTTPException(422,'待检索器件已改变或不存在。')
        try:job=await runner.knowledge.enqueue(project_id,refs,body.request_id,refresh=body.refresh)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return {'job_id':job,'project':store.get_project(project_id)}

    @app.get('/api/projects/{project_id}/knowledge-jobs/{job_id}/events')
    async def knowledge_events(project_id: str,job_id: str,request: Request,last_event_id: Annotated[str|None,Header()]=None):
        store.domain_job(project_id,job_id)
        try:after=max(0,int(last_event_id or request.query_params.get('after','0')))
        except ValueError:after=0
        async def stream():
            cursor=after
            while not await request.is_disconnected():
                for row in store.domain_events(project_id,job_id,cursor):
                    cursor=row['seq'];yield f'id: {cursor}\ndata: '+json.dumps(row,ensure_ascii=False)+'\n\n'
                if store.domain_job(project_id,job_id)['status'] not in ('queued','running'):break
                yield ': heartbeat\n\n';await asyncio.sleep(.8)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/projects/{project_id}/evidence/{evidence_id}')
    async def project_evidence(project_id: str,evidence_id: str):return store.evidence(project_id,evidence_id)

    @app.post('/api/projects/{project_id}/handoff-check')
    async def handoff_check(project_id: str):
        project=store.get_project(project_id)
        result=readiness(project)
        try:
            spec=build_spec(project)
            result['contract_valid']=True;result['selection_items']=len(spec['selection_items'])
        except ValueError as exc:
            result.update(status='blocked',contract_valid=False)
            result['blockers'].append('交接结构校验失败：'+str(exc)[:250])
        return result

    @app.post('/api/projects/{project_id}/handoff')
    async def create_handoff(project_id: str,body: HandoffRequest):
        project=store.get_project(project_id)
        with store.connect() as db:
            old=db.execute('SELECT id,manifest,version,domain_revision FROM handoff_artifacts WHERE project_id=? AND request_id=?',(project_id,body.request_id)).fetchone()
            if old:
                if old['version']!=body.base_version or old['domain_revision']!=body.base_domain_revision:raise Conflict('这个下载请求已对应另一版本，请重新发起。')
                return {'id':old['id'],'manifest':json.loads(old['manifest']),'replayed':True}
        if project['document']['version']!=body.base_version or project['domain_revision']!=body.base_domain_revision:raise Conflict('需求或资料已更新，请重新检查交接内容。')
        ready=readiness(project)
        if ready['status']=='blocked' and not body.allow_draft:raise Conflict('；'.join(ready['blockers']))
        try:manifest,content=generate_archive(project,store)
        except ValueError as exc:raise HTTPException(422,'交接结构未通过：'+str(exc)[:250]) from exc
        key=uuid.uuid4().hex
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay=db.execute('SELECT id,manifest FROM handoff_artifacts WHERE project_id=? AND request_id=?',(project_id,body.request_id)).fetchone()
            if replay:return {'id':replay['id'],'manifest':json.loads(replay['manifest']),'replayed':True}
            _,current=store._document(db,project_id)
            phase=store.domain_view(project_id,current)
            if current['version']!=body.base_version or phase['revision']!=body.base_domain_revision or bool(phase['active_jobs'])!=bool(project['domain']['active_jobs']):raise Conflict('生成期间需求或资料任务已更新，请重新检查后下载。')
            queue_ids=[r['id'] for r in db.execute("SELECT id FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid",(project_id,))]
            if store._pending_analysis(db,project_id)!=project.get('analysis_pending') or queue_ids!=[q['id'] for q in project['input_queue']]:
                raise Conflict('生成期间收到了新的用户补充，请重新检查交接；之前保存的需求保留。')
            db.execute('INSERT INTO handoff_artifacts VALUES(?,?,?,?,?,?,?,?)',(key,project_id,body.request_id,body.base_version,body.base_domain_revision,json.dumps(manifest,ensure_ascii=False),content,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()))
        return {'id':key,'manifest':manifest,'replayed':False}

    @app.get('/api/projects/{project_id}/handoff/{artifact_id}')
    async def download_handoff(project_id: str,artifact_id: str):
        with store.connect() as db:row=db.execute('SELECT content FROM handoff_artifacts WHERE project_id=? AND id=?',(project_id,artifact_id)).fetchone()
        if not row:raise Missing('交接包不存在。')
        return Response(row['content'],media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="requirements-{artifact_id[:8]}.zip"'})

    @app.post('/api/projects/{project_id}/downstream-adapter')
    async def downstream_adapter(project_id: str):
        try:return await asyncio.to_thread(generation_adapter,build_spec(store.get_project(project_id)))
        except ValueError as exc:raise HTTPException(422,'下游输入适配失败：'+str(exc)[:250]) from exc

    dist=Path(os.environ.get("REQUIREMENTS_STUDIO_DIST",str(ROOT/"frontend"/"dist")))
    @app.get("/{path:path}")
    async def frontend(path: str):
        if path=="api" or path.startswith("api/"):
            raise HTTPException(404,"接口不存在。")
        file=(dist/path).resolve()
        if file.is_relative_to(dist.resolve()) and file.is_file():
            return FileResponse(file)
        if "." in Path(path).name:
            raise HTTPException(404,"资源不存在。")
        if (dist/"index.html").is_file():
            return FileResponse(dist/"index.html",headers={"Cache-Control":"no-cache"})
        return Response("前端尚未构建，请进入 requirements_studio/frontend 执行 npm install && npm run build。",status_code=503)
    return app


app=create_app()
