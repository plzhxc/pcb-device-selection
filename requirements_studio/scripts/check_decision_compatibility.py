"""Read-only audit: existing requirements keep their previous review semantics."""
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'.runtime/decision-refactor-20261019'
sys.path.insert(0,str(ROOT))


def main():
    package=RUNTIME/'compatibility/baseline_backend';package.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(RUNTIME/'source-before.zip') as archive:
        for name in archive.namelist():
            path=Path(name)
            if not name.startswith('backend/') or path.suffix not in ('.py','.json'):continue
            relative=path.relative_to('backend')
            if '..' in relative.parts:raise ValueError('Invalid backup path')
            target=package/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(name))
    sys.path.insert(0,str(package.parent))
    baseline=importlib.import_module('baseline_backend.coverage')
    from backend.coverage import review_document
    differences=[];count=0;states={}
    with sqlite3.connect((ROOT/'.runtime/studio.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
        for pid,raw in db.execute('SELECT id,document FROM projects'):
            doc=json.loads(raw)
            if doc.get('intake_policy_version'):continue
            old=baseline.review_document(doc);new=review_document(doc)
            count+=1;states[old['status']]=states.get(old['status'],0)+1
            fields=('status','open_questions','internal_rechecks','module_pending','module_gaps')
            delta={key:{'before':old.get(key),'after':new.get(key)} for key in fields if old.get(key)!=new.get(key)}
            if delta:differences.append({'project_id':pid,'differences':delta})
    report={'ok':not differences,'read_only_database':True,'checked_projects':count,'old_states':states,'differences':differences}
    (RUNTIME/'compatibility/report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
