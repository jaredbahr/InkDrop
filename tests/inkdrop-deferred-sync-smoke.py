#!/usr/bin/env python3
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from core import inkdrop_deferred_sync


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp)/'state.sqlite3'
        con = sqlite3.connect(db)
        con.executescript("""
        create table queue_items(id text primary key);
        create table deferred_queue_syncs(id text primary key,source text,reason text,status text,created_at real,applied_at real,acked_at real,row_count integer,payload_json text);
        create table history_events(id text primary key,entity_type text,entity_id text,event_type text,source text,message text,created_at real,raw_json text,outcome text,display_phase text);
        """)
        now=time.time()
        con.execute("insert into queue_items values('live')")
        con.execute("insert into queue_items values('eligible')")
        payload=lambda key,status='retry_pending',replay=None: json.dumps({
            'processed':[],
            'skipped':[{'autopilot_queue_key':key,'status':status}],
            'native_attempt_replay': replay or [],
        })
        con.execute("insert into deferred_queue_syncs values('old','manual','series_autopilot_lock_busy','pending',?,null,null,1,?)", (now-90000,payload('gone')))
        con.execute(
            "insert into deferred_queue_syncs values('live','manual','series_autopilot_lock_busy','pending',?,null,null,1,?)",
            (
                now-10,
                payload(
                    'eligible',
                    replay=[{'queue_id':'eligible','attempt':{'status':'deferred_probe'}}],
                ),
            ),
        )
        con.execute("insert into deferred_queue_syncs values('future','manual','series_autopilot_lock_busy','pending',?,null,null,1,?)", (now-5,json.dumps({'processed':[],'skipped':[{'autopilot_queue_key':'live','status':'retry_pending','next_retry_after':now+600}]})))
        con.execute("insert into deferred_queue_syncs values('bad','manual','other','pending',?,null,null,1,'{')", (now-90000,))
        con.execute("insert into deferred_queue_syncs values('fresh-bad','manual','other','pending',?,null,null,1,'{')", (now-10,))
        con.commit(); con.close()
        audit=inkdrop_deferred_sync.classify_deferred_syncs(db,now=now)
        assert audit['count']==5 and audit['permanently_stale']==2, audit
        assert audit['eligible_now']==1 and audit['stale_signal'], audit
        assert audit['next_attempt'] == now + 600, audit
        assert audit['count_by_reason']['malformed_pending']==1, audit
        repaired=inkdrop_deferred_sync.reconcile_deferred_syncs(db,batch_size=3,now=now)
        assert repaired['reconciled']==2, repaired
        con=sqlite3.connect(db)
        assert con.execute("select count(*) from deferred_queue_syncs where status='acked'").fetchone()[0]==2
        assert con.execute("select count(*) from deferred_queue_syncs where status='applied'").fetchone()[0]==0
        assert con.execute("select count(*) from deferred_queue_syncs where status='pending' and id='live'").fetchone()[0]==1
        assert con.execute("select count(*) from deferred_queue_syncs where status='pending' and id='future'").fetchone()[0]==1
        assert con.execute("select count(*) from history_events where event_type='deferred_queue_syncs_reconciled'").fetchone()[0]==1
        con.close()
    print('inkdrop deferred sync smoke: ok')


if __name__=='__main__': main()
