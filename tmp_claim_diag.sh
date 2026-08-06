#!/usr/bin/env bash
set -euo pipefail
cd /home/ec2-user/qc-python

./.venv/bin/python - <<'"'"'PY'"'"'
import json
from sqlalchemy import text
from app.db.session import SessionLocal

claim_external = '49735039'
db = SessionLocal()
try:
    claim = db.execute(text("""
        select id, external_claim_id, status, assigned_doctor_id, created_at, updated_at
        from claims where external_claim_id=:e
        order by updated_at desc limit 1
    """), {'e': claim_external}).mappings().first()
    print('CLAIM', json.dumps({k:str(v) for k,v in (claim or {}).items()}, default=str))
    if not claim:
        raise SystemExit(0)
    cid = str(claim['id'])

    tables = [r[0] for r in db.execute(text("select table_name from information_schema.tables where table_schema='public' order by table_name")).all()]
    print('REPORT_LIKE_TABLES', [t for t in tables if 'report' in t or 'version' in t])

    def has(t):
        return t in tables
    def cnt(t):
        if not has(t):
            return None
        return db.execute(text(f"select count(*) from {t} where claim_id=:cid"), {'cid': cid}).scalar()

    for t in ['claim_documents','claim_structured_data','claim_reports','report_versions','decision_results','workflow_events','claim_report_uploads']:
        print('HAS', t, has(t), 'COUNT', cnt(t))

    if has('claim_documents'):
        cols = [r[0] for r in db.execute(text("select column_name from information_schema.columns where table_schema='public' and table_name='claim_documents' order by ordinal_position")).all()]
        print('CLAIM_DOCUMENTS_COLUMNS', cols)
        order_col = 'uploaded_at' if 'uploaded_at' in cols else ('created_at' if 'created_at' in cols else cols[0])
        sel_cols = [c for c in ['id','uploaded_at','created_at','file_name','mime_type','storage_uri'] if c in cols]
        docs = db.execute(text(f"select {', '.join(sel_cols)} from claim_documents where claim_id=:cid order by {order_col} desc limit 5"), {'cid': cid}).mappings().all()
        print('DOCS', json.dumps([{k:str(v) for k,v in r.items()} for r in docs], default=str))

    if has('claim_structured_data'):
        cols = [r[0] for r in db.execute(text("select column_name from information_schema.columns where table_schema='public' and table_name='claim_structured_data' order by ordinal_position")).all()]
        print('CLAIM_STRUCTURED_COLUMNS', cols)
        latest = db.execute(text("select * from claim_structured_data where claim_id=:cid order by updated_at desc nulls last, created_at desc nulls last limit 1"), {'cid': cid}).mappings().first()
        if latest:
            out={}
            for k,v in latest.items():
                s='' if v is None else str(v)
                out[k]=s[:500]
            print('STRUCTURED_LATEST', json.dumps(out, default=str))

    report_table = 'claim_reports' if has('claim_reports') else ('report_versions' if has('report_versions') else None)
    if report_table:
        cols = [r[0] for r in db.execute(text(f"select column_name from information_schema.columns where table_schema='public' and table_name='{report_table}' order by ordinal_position")).all()]
        print('REPORT_TABLE', report_table)
        print('REPORT_COLUMNS', cols)
        html_col = 'report_html' if 'report_html' in cols else None
        md_col = 'report_markdown' if 'report_markdown' in cols else None
        created_col = 'created_at' if 'created_at' in cols else ('generated_at' if 'generated_at' in cols else cols[0])
        src_col = 'report_source' if 'report_source' in cols else None
        status_col = 'report_status' if 'report_status' in cols else None
        ver_col = 'version_no' if 'version_no' in cols else ('version' if 'version' in cols else None)
        select_parts = ['id']
        for c in [status_col, src_col, ver_col, created_col]:
            if c and c not in select_parts:
                select_parts.append(c)
        if html_col: select_parts.append(f"length(coalesce({html_col},'')) as html_len")
        if md_col: select_parts.append(f"length(coalesce({md_col},'')) as markdown_len")
        reps = db.execute(text(f"select {', '.join(select_parts)} from {report_table} where claim_id=:cid order by {created_col} desc limit 10"), {'cid': cid}).mappings().all()
        print('REPORT_ROWS', json.dumps([{k:str(v) for k,v in r.items()} for r in reps], default=str))

    if has('decision_results'):
        cols = [r[0] for r in db.execute(text("select column_name from information_schema.columns where table_schema='public' and table_name='decision_results' order by ordinal_position")).all()]
        print('DECISION_COLUMNS', cols)
        pieces=['id']
        for c in ['recommendation','generated_at']:
            if c in cols: pieces.append(c)
        if 'result_json' in cols: pieces.append("length(coalesce(result_json::text,'')) as result_json_len")
        if 'decision_json' in cols: pieces.append("length(coalesce(decision_json::text,'')) as decision_json_len")
        dec = db.execute(text(f"select {', '.join(pieces)} from decision_results where claim_id=:cid order by generated_at desc limit 10"), {'cid': cid}).mappings().all()
        print('DECISIONS', json.dumps([{k:str(v) for k,v in r.items()} for r in dec], default=str))

    if has('workflow_events'):
        ev = db.execute(text("select event_type, occurred_at, left(coalesce(event_payload::text,''),250) as payload from workflow_events where claim_id=:cid order by occurred_at desc limit 30"), {'cid': cid}).mappings().all()
        print('EVENTS', json.dumps([{k:str(v) for k,v in r.items()} for r in ev], default=str))

finally:
    db.close()
PY
