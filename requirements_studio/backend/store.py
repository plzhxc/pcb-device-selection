from __future__ import annotations

import json
import copy
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import empty_document, now, render_markdown
from .coverage import review_document
from .convergence import view as convergence_view, invalidate as invalidate_convergence, ModuleUpdate, apply as apply_convergence
from .module_catalog import TYPES
from .domain_store import DomainStore
from .history_codec import pack_history, history_document

MAX_INTERNAL_CONTINUATIONS = 3


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class Store(DomainStore):
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            mode=os.environ.get('REQUIREMENTS_STUDIO_JOURNAL','DELETE').upper()
            if mode not in ('DELETE','WAL'):raise ValueError('存储日志模式仅支持 DELETE 或 WAL。')
            # GPFS is the deployed filesystem. Avoid WAL shared-memory files there;
            # local-disk installations may explicitly opt into WAL.
            db.execute('PRAGMA journal_mode='+mode)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, title TEXT NOT NULL, document TEXT NOT NULL, is_example INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, version INTEGER NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(project_id,version));
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), message_id TEXT NOT NULL, request_id TEXT NOT NULL, base_version INTEGER NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(project_id,request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(project_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,seq));
                CREATE TABLE IF NOT EXISTS input_queue(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), text TEXT NOT NULL, request_id TEXT NOT NULL, answers TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending', run_id TEXT, created_at TEXT NOT NULL, UNIQUE(project_id,request_id));
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
            for name, definition in {"kind":"TEXT NOT NULL DEFAULT 'user'", "answers":"TEXT NOT NULL DEFAULT '[]'", "continuation_task":"TEXT NOT NULL DEFAULT ''", "actions":"TEXT NOT NULL DEFAULT '[]'"}.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            columns={r['name'] for r in db.execute('PRAGMA table_info(input_queue)')}
            if 'actions' not in columns:db.execute("ALTER TABLE input_queue ADD COLUMN actions TEXT NOT NULL DEFAULT '[]'")
            self.init_domain(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            for row in db.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall():
                db.execute("UPDATE runs SET status='interrupted',error=?,finished_at=? WHERE id=?", ("服务已重启，本轮分析中断。已保存文档保持完整，可以重试。", now(), row["id"]))
                self._event(db, row["id"], "interrupted", {"message": "服务已重启，本轮分析中断，可以重试。"})
            self.recover_domain(db)

    @staticmethod
    def _document(db, project_id):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise Missing("项目不存在。")
        return row, json.loads(row["document"])

    def list_projects(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY updated_at DESC,id").fetchall()
            return [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"], "is_example": bool(r["is_example"]), "version": json.loads(r["document"])["version"], "requirement_count": len(json.loads(r["document"])["requirements"])} for r in rows]

    def create_project(self, example=False):
        project_id = uuid.uuid4().hex
        doc = empty_document()
        messages = []
        if example:
            message_id = uuid.uuid4().hex
            text = "我想做一个室内蓝牙温度记录器，电池供电，每分钟采样一次，续航至少30天，手机能读取历史数据，单台硬件预算80元以内。测量精度我还没想好。"
            doc.update(title="蓝牙温度记录器", summary="面向室内使用的低功耗温度记录器，定时采样并保存，通过蓝牙在手机端读取历史数据。当前已明确供电形式、采样间隔、续航和预算，测量精度与存储周期仍待细化。", version=1)
            rows = [
                ("overview", "使用场景", "室内环境", "", "室内"),
                ("functions", "温度采集", "定时采集温度", "", "每分钟采样一次"),
                ("functions", "蓝牙数据读取", "手机通过蓝牙读取历史数据", "", "手机能读取历史数据"),
                ("functions", "历史数据保存", "保存温度采样记录", "", "手机能读取历史数据"),
                ("performance", "采样间隔", "1", "分钟", "每分钟采样一次"),
                ("performance", "续航目标", "至少 30", "天", "续航至少30天"),
                ("performance", "测量精度", "待确认", "", "测量精度我还没想好"),
                ("architecture", "供电方式", "电池供电", "", "电池供电"),
                ("selection", "单台硬件预算", "不超过 80", "元", "单台硬件预算80元以内"),
            ]
            doc["requirements"] = [{"id": f"REQ-{i:03d}", "category": c, "name": n, "value": v, "unit": u, "priority": "must", "state": "tbd" if v == "待确认" else "stated", "source_message_id": message_id, "source_quote": q, "source_type": "user", "updated_at": now()} for i, (c,n,v,u,q) in enumerate(rows,1)]
            doc["questions"] = [{"id": "accuracy", "question": "温度测量需要达到怎样的精度？", "reason": "精度会影响传感器选择和硬件成本。", "options": ["±0.5°C，日常监测", "±0.1°C，精细测量", "暂不确定，先给我建议"], "requirement_ids": ["REQ-007"], "source_message_id": message_id}]
            doc["flow"] = [{"label": label, "requirement_ids": ids} for label,ids in [("定时唤醒",["REQ-005"]),("采集温度",["REQ-002"]),("保存记录",["REQ-004"]),("蓝牙同步",["REQ-003"]),("低功耗休眠",["REQ-006"])]]
            doc["changes"] = ["示例数据：建立产品目标、3项功能和主要约束。", "保留测量精度为待确认，不预设器件型号。"]
            doc["next_requirement_number"] = len(rows)+1
            messages = [(message_id, "user", text), (uuid.uuid4().hex, "assistant", "我已把这个想法整理成右侧的初版需求，包括温度采集、记录保存和蓝牙读取。\n\n已记录每分钟采样、至少 30 天续航和 80 元硬件预算。接下来先确认测量精度，它会影响传感器选择与成本。\n\n_这是用于查看界面与操作的示例内容。继续发送后将调用真实 Codex CLI。_")]
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (project_id, doc["title"], json.dumps(doc,ensure_ascii=False), int(example), now()))
            for mid, role, text in messages:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (mid,project_id,role,text,now()))
            if doc["version"]:
                db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id,doc["version"],pack_history(json.dumps(doc,ensure_ascii=False)),now()))
        return self.get_project(project_id)

    @staticmethod
    def _pending_analysis(db,project_id):
        last=db.execute("SELECT id FROM messages WHERE project_id=? AND role='user' ORDER BY rowid DESC LIMIT 1",(project_id,)).fetchone()
        if not last:return None
        runs=db.execute('SELECT id,status FROM runs WHERE project_id=? AND message_id=? ORDER BY rowid DESC',(project_id,last['id'])).fetchall()
        if not runs or any(r['status']=='completed' for r in runs):return None
        return {'message_id':last['id'],'run_id':runs[0]['id'],'status':runs[0]['status']}

    def get_project(self, project_id):
        with self.connect() as db:
            row, doc = self._document(db, project_id)
            messages = [dict(m) for m in db.execute("SELECT * FROM messages WHERE project_id=? ORDER BY rowid", (project_id,))]
            snapshots=[history_document(r['document']) for r in db.execute('SELECT document FROM revisions WHERE project_id=? ORDER BY version',(project_id,))]
            revisions=[{'version':s['version'],'created_at':s['updated_at'],'changes':s['changes']} for s in reversed(snapshots)]
            latest = db.execute("SELECT * FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
            queue = [dict(q) for q in db.execute("SELECT id,text,created_at FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid", (project_id,))]
            submissions={}
            for run in db.execute("SELECT id,message_id,answers,status FROM runs WHERE project_id=? AND answers!='[]' ORDER BY rowid",(project_id,)):
                for answer in json.loads(run['answers']):
                    submissions[answer['question_id']]={**answer,'run_id':run['id'],'message_id':run['message_id'],'status':'processing' if run['status'] in ('queued','running') else 'needs_input' if run['status']=='completed' else 'failed'}
            for item in db.execute("SELECT id,answers FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid",(project_id,)):
                for answer in json.loads(item['answers']):submissions[answer['question_id']]={**answer,'queue_id':item['id'],'status':'queued'}
            reference_submissions=[]
            for item in db.execute("SELECT id,actions FROM runs WHERE project_id=? AND status IN ('queued','running') ORDER BY rowid",(project_id,)):
                reference_submissions.extend({**a,'run_id':item['id'],'status':'processing'} for a in json.loads(item['actions']))
            for item in db.execute("SELECT id,actions FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid",(project_id,)):
                reference_submissions.extend({**a,'queue_id':item['id'],'status':'queued'} for a in json.loads(item['actions']))
            pending_ids={q['id'] for q in doc['questions']}
            recent=[dict(r) for r in db.execute('SELECT id,message_id,kind,status,created_at FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT 40',(project_id,))]
            event_groups={r['id']:[] for r in recent}
            if recent:
                marks=','.join('?' for _ in recent)
                for e in db.execute(f"SELECT run_id,seq,kind,payload FROM events WHERE run_id IN ({marks}) AND kind IN ('stage','progress','completed','failed','cancelled','interrupted') ORDER BY seq",list(event_groups)):
                    event_groups[e['run_id']].append({'seq':e['seq'],'kind':e['kind'],**json.loads(e['payload'])})
            activity=[]
            for run in reversed(recent):
                events=event_groups[run['id']]
                waiting=[e for e in events if e.get('phase')=='waiting']
                visible=[e for e in events if e.get('phase')!='waiting' or e is waiting[-1]] if waiting else events
                activity.append({**run,'events':visible[-16:]})
            project={"id": row["id"], "title": row["title"], "is_example": bool(row["is_example"]), "updated_at": row["updated_at"], "document": doc, "messages": messages, "revisions": revisions, "latest_run": dict(latest) if latest else None, "input_queue":queue,"review":review_document(doc),'convergence':convergence_view(doc),'question_submissions':[s for qid,s in submissions.items() if qid in pending_ids],'run_activity':activity}
            from .decisions import journey_view
            answer_runs=[{**dict(r),'answers':json.loads(r['answers'])} for r in db.execute('SELECT message_id,answers,status FROM runs WHERE project_id=? ORDER BY rowid',(project_id,))]
            project['decision_path']=journey_view(doc,messages,snapshots,answer_runs,project['question_submissions'])
            project['analysis_pending']=self._pending_analysis(db,project_id)
            if project['analysis_pending']:
                project['review'].update(status='draft',analysis_pending='最新补充尚未完成整理。请等待本轮完成；若分析未完成，可重试或继续补充后再整体审核。')
        domain=self.domain_view(project_id,doc)
        project['domain']=domain
        project['reference_submissions']=reference_submissions
        project['domain_revision']=domain['revision']
        from .canvas_sync import view as canvas_sync_view
        project['canvas_sync']=canvas_sync_view(doc)
        from .decision_map import decision_map
        project['decision_map']=decision_map(doc,domain,snapshots,project['question_submissions'])
        project['run_activity']=sorted(activity+domain['activity'],key=lambda r:r['created_at'])
        if domain['blockers']:
            project['review']['status']='draft'
            project['review']['domain_blockers']=domain['blockers']
            project['review']['consistency_issues']+= [{'id':c['id'],'kind':'conflict','title':c['title'],'description':c['description'],'requirement_ids':c['requirement_ids']} for c in domain['blockers']]
            project['convergence']['issues']+= [{'id':c['id'],'kind':'conflict','title':c['title'],'description':c['description'],'requirement_ids':c['requirement_ids']} for c in domain['blockers']]
        from .document_export import render_project_document
        rendered=render_project_document({**doc,'analysis_pending':project['analysis_pending']},domain,structured=True)
        project['markdown']=rendered.pop('markdown')
        project['specification']=rendered
        from .delivery import view as delivery_view
        project['delivery']=delivery_view(project)
        with self.connect() as db:
            scheduled=[dict(r) for r in db.execute("SELECT id,project_id,status,kind,created_at FROM runs WHERE status IN ('queued','running') ORDER BY rowid")]
        queued=[r for r in scheduled if r['status']=='queued']
        project['scheduling']={'running':sum(r['status']=='running' for r in scheduled),'queued':len(queued),'position':next((i+1 for i,r in enumerate(queued) if r['project_id']==project_id),None),'capacity':2}
        return project

    @staticmethod
    def _event(db, run_id, kind, payload):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?)", (run_id,seq,kind,json.dumps({'at':now(),**payload},ensure_ascii=False)))
        return seq

    def event(self, run_id, kind, payload):
        with self.connect() as db:
            self._event(db,run_id,kind,payload)

    def events(self, run_id, after):
        with self.connect() as db:
            return [{**dict(r),"payload":json.loads(r["payload"])} for r in db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id,after))]

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise Missing("分析任务不存在。")
            return dict(row)

    def previous_failure(self,run_id):
        with self.connect() as db:
            run=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            row=db.execute("SELECT * FROM runs WHERE project_id=? AND message_id=? AND id!=? AND status IN ('failed','interrupted') ORDER BY rowid DESC LIMIT 1",(run['project_id'],run['message_id'],run_id)).fetchone()
            return dict(row) if row else None

    def begin_run(self, project_id, text, request_id, retry=False, answers=None, actions=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._begin_run(db,project_id,text,request_id,retry,answers,actions=actions)

    def _begin_run(self, db, project_id, text, request_id, retry=False, answers=None, kind="user", message_id=None, task="", actions=None):
        _, doc = self._document(db, project_id)
        duplicate = db.execute("SELECT * FROM runs WHERE project_id=? AND request_id=?", (project_id,request_id)).fetchone()
        if duplicate:
            return dict(duplicate), False
        if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
            raise Conflict("这个项目正在分析，请等待完成或先停止。")
        if retry:
            last = db.execute("SELECT * FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
            if not last or last["status"] not in ("failed","cancelled","interrupted"):
                raise Conflict("当前没有可以重试的任务。")
            message_id,kind,task = last["message_id"],last["kind"],last["continuation_task"]
            answers = json.loads(last["answers"])
            actions = json.loads(last['actions'])
        elif kind == "user":
            message_id = uuid.uuid4().hex
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (message_id,project_id,"user",text,now()))
            self.apply_actions(db,project_id,doc,message_id,text,actions or [])
        run_id = uuid.uuid4().hex
        db.execute("INSERT INTO runs(id,project_id,message_id,request_id,base_version,status,created_at,kind,answers,continuation_task,actions) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)", (run_id,project_id,message_id,request_id,doc["version"],now(),kind,json.dumps(answers or [],ensure_ascii=False),task,json.dumps(actions or [],ensure_ascii=False)))
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(),project_id))
        self._event(db,run_id,"stage",{"message":"继续梳理可独立推进的部分，你可以随时补充。" if kind=="continuation" else "已收到需求，等待开始分析。"})
        return dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), True

    def submit_message(self, project_id, text, request_id, answers=None, after_run_id=None, actions=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._document(db,project_id)
            duplicate = db.execute("SELECT * FROM runs WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
            if duplicate:
                return dict(duplicate),False,None
            queued = db.execute("SELECT * FROM input_queue WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
            running = db.execute("SELECT * FROM runs WHERE project_id=? AND status IN ('queued','running')",(project_id,)).fetchone()
            parent = db.execute("SELECT status FROM runs WHERE id=? AND project_id=?",(after_run_id,project_id)).fetchone() if after_run_id else None
            if after_run_id and not parent:
                raise Conflict("这条补充关联的分析轮次不存在，请刷新后发送。")
            paused = parent and parent["status"] in ("failed","cancelled","interrupted") and not running
            if queued:
                return dict(running) if running else None,False,dict(queued)
            if actions:
                # Reloading a page creates a new HTTP request ID. The same pending
                # structured choice is still one decision, including its link scope.
                if running and json.loads(running['actions'])==actions:
                    return dict(running),False,None
                for item in db.execute("SELECT * FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid",(project_id,)):
                    if json.loads(item['actions'])==actions:
                        return dict(running) if running else None,False,dict(item)
            requested={a['question_id'] for a in answers or []}
            if requested:
                _,doc=self._document(db,project_id)
                if requested-{q['id'] for q in doc['questions']}:
                    raise Conflict('这个问题已完成处理；如需修改，请在对话中补充。')
                locked=set()
                if running:locked.update(a['question_id'] for a in json.loads(running['answers']))
                for item in db.execute("SELECT answers FROM input_queue WHERE project_id=? AND status='pending'",(project_id,)):
                    locked.update(a['question_id'] for a in json.loads(item['answers']))
                if requested & locked:raise Conflict('这条回答已经收到，正在处理。可在输入框继续补充，无需重复选择。')
            pending = db.execute("SELECT COUNT(*) FROM input_queue WHERE project_id=? AND status='pending'",(project_id,)).fetchone()[0]
            # A queued internal pass has not read a context yet. Replace it with
            # real customer input; an already running pass is allowed to finish.
            if running and running['kind']=='continuation' and running['status']=='queued':
                db.execute("UPDATE runs SET status='cancelled',finished_at=?,error=? WHERE id=? AND status='queued'",(now(),'已由新收到的用户补充替代，未执行旧上下文。',running['id']))
                self._event(db,running['id'],'cancelled',{'message':'新回答优先，尚未开始的内部整理已合并到下一轮。'})
                running=None
            if not running and not pending and not paused:
                run,created = self._begin_run(db,project_id,text,request_id,answers=answers,actions=actions)
                return run,created,None
            if pending >= 20:
                raise Conflict("待处理补充已达 20 条，请等待处理或移除部分后再发送。输入仍保留。")
            qid = uuid.uuid4().hex
            db.execute("INSERT INTO input_queue(id,project_id,text,request_id,answers,created_at,actions) VALUES(?,?,?,?,?,?,?)",(qid,project_id,text,request_id,json.dumps(answers or [],ensure_ascii=False),now(),json.dumps(actions or [],ensure_ascii=False)))
            queued = dict(db.execute("SELECT * FROM input_queue WHERE id=?",(qid,)).fetchone())
            if running:
                self._event(db,running["id"],"queue",{"message":"已保存补充，当前分析完成后按顺序处理。"})
                return dict(running),False,queued
            if paused:
                return None,False,queued
            run = self._take_next(db,project_id)
            return run,True,queued

    def _take_next(self, db, project_id):
        queued = db.execute("SELECT * FROM input_queue WHERE project_id=? AND status='pending' ORDER BY rowid LIMIT 1",(project_id,)).fetchone()
        if not queued:
            return None
        run,_ = self._begin_run(db,project_id,queued["text"],queued["request_id"],answers=json.loads(queued["answers"]),actions=json.loads(queued['actions']))
        db.execute("UPDATE input_queue SET status='started',run_id=? WHERE id=?",(run["id"],queued["id"]))
        return run

    def advance(self, run_id, next_task="", *, finalize=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if finalize and run['status']=='running':
                db.execute("UPDATE runs SET status='completed',finished_at=? WHERE id=?",(now(),run_id))
                self._event(db,run_id,'completed',{'message':'本轮整理已完成。'})
                run={**dict(run),'status':'completed'}
            latest = db.execute("SELECT id FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",(run["project_id"],)).fetchone()
            if run["status"]!="completed" or latest["id"]!=run_id:
                return None
            queued = self._take_next(db,run["project_id"])
            if queued:
                return queued
            _,doc = self._document(db,run["project_id"])
            if not next_task or doc.get('questions_paused'):
                return None
            review=review_document(doc)
            internal=bool(review['internal_rechecks'] or review['consistency_issues'])
            if not internal and not (run['kind']=='user' and doc['questions']):
                return None
            previous=db.execute("SELECT continuation_task FROM runs WHERE project_id=? AND message_id=? AND kind='continuation' ORDER BY rowid",(run['project_id'],run['message_id'])).fetchall()
            task_key=lambda value:' '.join(value.split()).casefold()
            if len(previous)>=MAX_INTERNAL_CONTINUATIONS or task_key(next_task) in {task_key(row['continuation_task']) for row in previous}:
                return None
            following=self._begin_run(db,run['project_id'],'','continue:'+run_id,kind='continuation',message_id=run['message_id'],task=next_task)[0]
            self._event(db,following['id'],'progress',{'phase':'internal_review','message':f'根据已有输入继续内部核对（{len(previous)+1}/{MAX_INTERNAL_CONTINUATIONS}）。未答问题保留，新补充优先处理。'})
            return following

    def resume_queue(self, project_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._document(db,project_id)
            if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')",(project_id,)).fetchone():
                raise Conflict("已经在处理补充，请等待。")
            run = self._take_next(db,project_id)
            if not run:
                raise Conflict("当前没有待处理补充。")
            return run

    def remove_queued(self, project_id, queue_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._document(db,project_id)
            cursor = db.execute("UPDATE input_queue SET status='cancelled' WHERE id=? AND project_id=? AND status='pending'",(queue_id,project_id))
            if not cursor.rowcount:
                raise Conflict("这条补充已经开始处理或已移除，请刷新查看。")

    def mark_running(self, run_id):
        with self.connect() as db:
            return bool(db.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,)).rowcount)

    def finish(self, run_id, status, error=""):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row and row["status"] in ("queued","running"):
                db.execute("UPDATE runs SET status=?,error=?,finished_at=? WHERE id=?", (status,error,now(),run_id))
                self._event(db,run_id,status,{"message":error or "分析完成。"})

    def commit_analysis(self, run_id, doc, reply, *, finalize=True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run["status"] not in ("queued","running"):
                raise Conflict("本轮已停止，未提交新的文档。")
            _, current = self._document(db,run["project_id"])
            if current["version"] != run["base_version"]:
                raise Conflict("文档已在另一处更新，本轮结果未覆盖它。请重新分析。")
            from .decisions import public_change
            before_review=review_document(current)['status']
            after_review=review_document({**doc,'review_status':'draft'})['status']
            milestone=before_review=='draft' and after_review=='ready'
            if milestone and doc.get('guidance_version')==1:
                reply=reply.rstrip()+'\n\n关联内容已整理完成，请到需求说明书整体审阅本版内容。'
            self._save(db,run["project_id"],doc,current["version"]+1,event={
                'message_id':run['message_id'],'run_id':run_id,'kind':run['kind'],
                'reply':reply,'answers':json.loads(run['answers'])})
            silent_internal=doc.get('guidance_version')==1 and run['kind']=='continuation' and before_review==after_review and not public_change(current,doc)
            if silent_internal:
                self._event(db,run_id,'progress',{'phase':'review','message':'已根据已有回答复核关联内容，当前产品决定保持有效。'})
            else:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (uuid.uuid4().hex,run["project_id"],"assistant",reply,now()))
            self._event(db,run_id,"document",{"version":doc["version"],"changed_ids":doc["changed_ids"]})
            if finalize:
                db.execute("UPDATE runs SET status='completed',finished_at=? WHERE id=?", (now(),run_id))
                self._event(db,run_id,"completed",{"message":"需求文档已更新。"})
            else:
                self._event(db,run_id,'progress',{'phase':'finalizing','message':'本轮修改已保存，正在完成资料排程和内部核对。可以继续补充，内容将按顺序处理。'})

    @staticmethod
    def _save(db,project_id,doc,version,approved=False,event=None):
        if doc.get('domain_schema_version'):
            from .device_domain import reconcile_selection_items
            reconcile_selection_items(doc)
        if not doc.get("created_at"):
            first=db.execute("SELECT created_at FROM revisions WHERE project_id=? ORDER BY version LIMIT 1",(project_id,)).fetchone()
            doc["created_at"]=first["created_at"] if first else now()
        doc["version"] = version
        doc["updated_at"] = now()
        doc["review_status"] = "approved" if approved else "draft"
        if event is not None:doc['decision_event']={**event,'version':version}
        else:doc.pop('decision_event',None)
        raw = json.dumps(doc,ensure_ascii=False)
        db.execute("UPDATE projects SET title=?,document=?,updated_at=? WHERE id=?", (doc["title"],raw,now(),project_id))
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id,version,pack_history(raw),now()))

    def edit_requirement(self, project_id, base_version, update):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, doc = self._document(db,project_id)
            original=copy.deepcopy(doc)
            if doc["version"] != base_version:
                raise Conflict("文档版本已变化，请刷新后再保存。")
            if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
                raise Conflict("分析中暂时不能直接编辑，请先停止或等待完成。")
            rid = update["id"]
            existing = next((r for r in doc["requirements"] if r["id"] == rid),None)
            if rid and existing is None:
                raise Missing("需求条目不存在。")
            if not rid:
                number = max([int(r["id"][4:]) for r in doc["requirements"]]+[doc.get("next_requirement_number",1)-1])+1
                rid = f"REQ-{number:03d}"
                doc["next_requirement_number"] = number+1
            message_id = uuid.uuid4().hex
            text = f"手动编辑 {rid}：{update['name']} = {update['value']} {update['unit']}；状态：{update['state']}。"
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (message_id,project_id,"user",text,now()))
            row = {**update,"id":rid,"source_message_id":message_id,"source_quote":text,"source_type":"manual","updated_at":now()}
            row['spec_field_ids']=existing.get('spec_field_ids',[]) if existing else []
            if existing:
                doc["requirements"][doc["requirements"].index(existing)] = row
            else:
                doc["requirements"].append(row)
            doc["changed_ids"] = [rid]
            doc["changes"] = [f"手动编辑 {rid} · {row['name']}。"]
            doc["questions"] = [q for q in doc["questions"] if rid not in q["requirement_ids"]]
            for item in doc.get("coverage",[]):
                if rid in item["requirement_ids"]:
                    item.update(state="tbd",conclusion="关联需求已手动修改，待重新核对。",source_message_id="",source_quote="")
            invalidate_convergence(doc,[rid])
            if doc.get('guidance_version')==1:
                from types import SimpleNamespace
                from .decisions import reconcile_requirements
                reconcile_requirements(doc,original,SimpleNamespace(guidance_revision=1,reviewed_requirement_ids=[],updates=[SimpleNamespace(id=rid)]),set(),{rid},{},message_id)
            overview=[r["value"] for r in doc["requirements"] if r["category"]=="overview" and r["state"]=="stated"]
            features=[r["name"] for r in doc["requirements"] if r["category"]=="functions" and r["state"]!="not_applicable"]
            doc["summary"] = ("；".join(overview[:2])+"。" if overview else "") + ("主要功能："+"、".join(features[:6])+"。" if features else "") + "具体指标与约束以以下需求条目为准。"
            self._save(db,project_id,doc,base_version+1,event={'message_id':message_id,'kind':'manual_requirement','reply':text})
        return self.get_project(project_id)

    def edit_module(self,project_id,base_version,update,remove=False):
        import copy
        from types import SimpleNamespace
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _,doc=self._document(db,project_id)
            if doc['version']!=base_version:raise Conflict('版本已变化，请刷新后再保存模块方案。')
            if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')",(project_id,)).fetchone():
                raise Conflict('分析中请在对话里补充模块想法，或等待本轮完成后编辑。')
            existing=next((m for m in doc.get('modules',[]) if m['id']==update.get('id')),None)
            if update.get('id') and not existing:raise Missing('模块实例不存在。')
            original=copy.deepcopy(doc)
            mid=uuid.uuid4().hex
            if remove:
                text=f"从本版模块方案移除「{existing['name']}」，保留原需求并重新检查功能承接。"
                updates=[];removals=[existing['id']]
            else:
                if update['type_id'] not in TYPES:raise Conflict('请选择规范目录中适用的模块类别。')
                if not update['requirement_ids']:raise Conflict('请关联至少一项需求，说明这个模块的作用。')
                text=f"模块方案确认：{update['name']}；类别：{TYPES[update['type_id']]['name']}；数量：{update['quantity']}；用途：{update['purpose']}；边界：{update['boundary']}；处理状态：{update['state']}。"
                row={**update,'id':update.get('id') or 'new-manual','source_message_id':mid,'source_quote':text}
                updates=[ModuleUpdate.model_validate(row)];removals=[]
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,project_id,'user',text,now()))
            changes=SimpleNamespace(analysis_revision=2,field_resolutions=[],module_updates=updates,remove_module_ids=removals,module_links=[],numeric_constraints=[])
            try:apply_convergence(doc,changes,original,[{'id':mid,'role':'user','content':text}],{},set())
            except ValueError as exc:raise Conflict(str(exc)) from exc
            doc.update(changes=[text],changed_ids=[])
            self._save(db,project_id,doc,base_version+1,event={'message_id':mid,'kind':'manual_module','reply':text})
        return self.get_project(project_id)

    def rename(self,project_id,title,base_version):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _,doc = self._document(db,project_id)
            if doc["version"] != base_version:
                raise Conflict("版本已变化，请刷新后重试。")
            if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')",(project_id,)).fetchone():
                raise Conflict("请等待本轮分析完成后再重命名。")
            doc.update(title=title,title_locked=True,changes=[f"项目重命名为「{title}」。"],changed_ids=[])
            self._save(db,project_id,doc,base_version+1,approved=doc.get('review_status')=='approved')
        return self.get_project(project_id)

    def approve(self,project_id,base_version,base_domain_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _,doc = self._document(db,project_id)
            if doc["version"]!=base_version:
                raise Conflict("文档版本已变化，请重新审阅后确认。")
            if db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')",(project_id,)).fetchone() or db.execute("SELECT id FROM input_queue WHERE project_id=? AND status='pending'",(project_id,)).fetchone():
                raise Conflict("请先完成或移除正在处理的补充，再审核本版需求。")
            if self._pending_analysis(db,project_id):
                raise Conflict('最新补充尚未完成需求整理，请重试或继续补充后再审核；上个版本及输入已保留。')
            review=review_document(doc)
            domain=self.domain_view(project_id,doc)
            if base_domain_revision is not None and base_domain_revision!=domain['revision']:
                raise Conflict('资料核对状态已更新，请查看最新依据后再审核。')
            if domain['active_jobs']:
                raise Conflict('资料仍在核对，请等待本批结果后审核；需求与回答已保存。')
            if domain['blockers']:
                raise Conflict('存在已核实的器件硬约束冲突，请先处理相关取舍。')
            if review["status"] not in ("ready","approved"):
                gaps=review['decision_gaps'] if doc.get('convergence_version')==2 else review['gaps']
                missing=f'{len(gaps)} 项产品决定待整理' if doc.get('convergence_version')==2 else f'{len(gaps)} 个章节缺口'
                raise Conflict(f"仍有 {missing}、{review['open_questions']} 个待答问题、{len(review['unsettled_requirements'])} 项未确认需求、{len(review['module_gaps'])+len(review['module_pending'])} 项模块待核对、{len(review['consistency_issues'])} 项一致性问题及 {review['internal_rechecks']} 项 AI 关联复核；助手需依据已有决定完成内部整理，暂不能标记已审核。")
            if review["status"]=="approved":
                return self.get_project(project_id)
            doc.update(changes=["用户审核通过当前需求说明书。"],changed_ids=[])
            self._save(db,project_id,doc,base_version+1,approved=True)
        return self.get_project(project_id)
