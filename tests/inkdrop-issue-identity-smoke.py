#!/usr/bin/env python3
import json
import sqlite3
import tempfile
from pathlib import Path

import inkdrop_issue_identity


def setup(db):
    con = sqlite3.connect(db)
    con.executescript("""
    create table series(id text primary key,title text,year integer,publisher text,metadata_provider text,metadata_id text);
    create table issues(id text primary key,series_id text,issue_number text,normalized_number text,title text,release_date text,metadata_provider text,metadata_id text,kapowarr_issue_id integer,monitored integer,created_at real,updated_at real,raw_json text);
    create table wanted_items(id text primary key,series_id text,issue_id text,status text);
    create table queue_items(id text primary key,wanted_id text,series_id text,issue_id text,state text,active integer);
    create table source_attempts(id text primary key,issue_id text);
    create table download_tasks(id text primary key,issue_id text);
    create table import_results(id text primary key,issue_id text);
    create table history_events(id text primary key,entity_type text,entity_id text,series_id text,issue_id text,event_type text,source text,message text,created_at real,raw_json text,outcome text,display_phase text);
    """)
    con.execute("insert into series values('s','Amulet',2008,'Scholastic','comicvine','51629')")
    native = json.dumps({'id':'354087','kapowarrIssueId':2304})
    adapter = json.dumps({'id':'354087','kapowarrIssueId':2304})
    con.execute("insert into issues values('native','s','4','0004','The Last Council',null,'comicvine','354087',2304,1,1,2,?)", (native,))
    con.execute("insert into issues values('adapter','s','4','0004','The Last Council',null,'kapowarr','2304',2304,1,2,3,?)", (adapter,))
    con.execute("insert into issues values('edition','s','4','0004','The Last Council',null,'comicvine','other',null,1,3,4,?)", (json.dumps({'year':2024,'edition':'deluxe'}),))
    con.execute("insert into wanted_items values('w','s','adapter','satisfied')")
    con.execute("insert into queue_items values('q','w','s','adapter','verified',0)")
    con.execute("insert into import_results values('ir','native')")
    con.commit(); con.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp)/'state.sqlite3'; setup(db)
        plan = inkdrop_issue_identity.reconciliation_plan(db, ['adapter','native'])
        assert plan['classification'] == 'equivalent', plan
        assert plan['canonical_issue_id'] == 'native', plan
        conflict = inkdrop_issue_identity.reconciliation_plan(db, ['native','edition'])
        assert conflict['classification'] in {'conflict','uncertain'} and not conflict['apply_allowed'], conflict
        backup = Path(tmp)/'backup.sqlite3'
        result = inkdrop_issue_identity.apply_reconciliation(db, ['adapter','native'], backup_path=backup)
        assert result['applied'] and backup.exists(), result
        con = sqlite3.connect(db)
        assert con.execute("select issue_id from wanted_items where id='w'").fetchone()[0] == 'native'
        assert con.execute("select count(*) from import_results where issue_id='native'").fetchone()[0] == 1
        raw = json.loads(con.execute("select raw_json from issues where id='adapter'").fetchone()[0])
        assert raw['retired_duplicate'] and raw['canonical_issue_id'] == 'native'
        aliases = json.loads(con.execute("select raw_json from issues where id='native'").fetchone()[0])['linked_external_identities']
        assert any(alias['metadata_id'] == '2304' for alias in aliases)
        con.close()
        second = inkdrop_issue_identity.apply_reconciliation(db, ['adapter','native'], backup_path=Path(tmp)/'backup2.sqlite3')
        assert second['idempotent'] and not second['applied'], second
    print('inkdrop issue identity smoke: ok')


if __name__ == '__main__': main()
