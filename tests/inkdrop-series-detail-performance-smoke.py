#!/usr/bin/env python3
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_state


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db=Path(tmp)/'state.sqlite3'
        con=sqlite3.connect(db); con.row_factory=sqlite3.Row
        inkdrop_state.init_schema(con)
        now=time.time()
        series=[]; issues=[]; wanted=[]; queues=[]
        for n in range(1200):
            sid=f's:{n}'; series.append((sid,f'Series {n}',f'series {n}','comic',2000+n%20,'Publisher','comicvine',str(n),None,'comicvine',1,1,1,now,now,'{}'))
            for issue in range(8):
                iid=f'{sid}:i:{issue}'; issues.append((iid,sid,str(issue),f'{issue:04d}',f'Issue {issue}',None,'comicvine',f'{n}-{issue}',None,1,now,now,'{}'))
                wanted.append((f'w:{n}:{issue}',sid,iid,'missing','wanted',50,now,now,'{}'))
                queues.append((f'q:{n}:{issue}',f'w:{n}:{issue}',sid,iid,'queued','prowlarr',f'Series {n} {issue}','waiting',1,now,now,'[]','[]','{}'))
        con.executemany("insert into series(id,title,sort_title,media_type,year,publisher,metadata_provider,metadata_id,kapowarr_id,source,monitored,monitor_new,auto_grab,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",series)
        con.executemany("insert into issues(id,series_id,issue_number,normalized_number,title,release_date,metadata_provider,metadata_id,kapowarr_issue_id,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",issues)
        con.executemany("insert into wanted_items(id,series_id,issue_id,reason,status,priority,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",wanted)
        con.executemany("insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,query,last_event,active,created_at,updated_at,source_order_json,recovery_steps_json,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",queues)
        history = [
            (
                f"h:{n}",
                "series",
                f"s:{n % 1200}",
                f"s:{n % 1200}",
                None,
                "source_ladder",
                "smoke",
                "Synthetic history",
                now - n,
                "{}",
                "ok",
                "diagnostic",
            )
            for n in range(25000)
        ]
        con.executemany(
            "insert into history_events(id,entity_type,entity_id,series_id,issue_id,event_type,source,message,created_at,raw_json,outcome,display_phase) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            history,
        )
        indexes = {row[1] for row in con.execute("pragma index_list('history_events')")}
        assert "idx_history_series_created" in indexes, indexes
        plan = " ".join(str(row[3]) for row in con.execute("explain query plan select count(*) from history_events where series_id=?", ("s:1199",)))
        assert "idx_history_series_created" in plan, plan
        con.commit(); con.close()
        samples=[]
        for _ in range(5):
            started=time.perf_counter(); detail=inkdrop_state.series_detail_contract(db,'s:1199',recent_limit=20); samples.append(time.perf_counter()-started)
        assert detail and len(detail['recent_issues'])==8
        assert max(samples)<2.0, samples
        assert detail['issues_endpoint'].endswith('limit=50&summary=compact&rows=compact')
        print({'samples_seconds':samples,'max_seconds':max(samples)})
    print('inkdrop series detail performance smoke: ok')


if __name__=='__main__': main()
