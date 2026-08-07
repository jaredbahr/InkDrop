#!/usr/bin/env python3
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from core import inkdrop_activity


def schema(con):
    con.executescript("""
    create table series(id text primary key,title text);
    create table issues(id text primary key,series_id text,issue_number text,title text);
    create table wanted_items(id text primary key,series_id text,issue_id text,status text);
    create table queue_items(id text primary key,wanted_id text,series_id text,issue_id text,state text,current_source text,retry_after real,last_event text,updated_at real,created_at real,active integer);
    create table source_attempts(id text primary key,queue_id text,source text,provider_id text,provider text,protocol text,status text,download_client text,lifecycle_phase text,started_at real,completed_at real,save_path text,raw_json text);
    create table download_tasks(id text primary key,queue_id text,source text,provider_id text,provider text,protocol text,download_client text,external_id text,status text,state text,progress real,size_bytes integer,started_at real,updated_at real,completed_at real,local_path text,raw_json text);
    create table import_results(id text primary key,queue_id text,series_id text,issue_id text,status text,verified integer,folder_imported integer,completion_truth text,library_visibility_required integer,library_visibility_status text,library_visibility_provider text,source_path text,dest_path text,created_at real,raw_json text);
    """)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.sqlite3"
        con = sqlite3.connect(db)
        schema(con)
        now = time.time()
        con.execute("insert into series values('s1','Powers')")
        con.execute("insert into issues values('i1','s1','11','Issue Eleven')")
        con.execute("insert into wanted_items values('w1','s1','i1','in_progress')")
        con.execute("insert into queue_items values('q1','w1','s1','i1','importing','slskd',null,'waiting',?,?,1)", (now, now - 60))
        con.execute("insert into source_attempts values('a1','q1','slskd','slskd','slskd','soulseek','grab_sent','SLSKD','handoff',?,?,null,?)", (now - 60, now - 50, '{}'))
        raw = json.dumps({'detected_path':'/stage/Powers 011.cbz','transfer_state':'Completed, Succeeded','bytes_completed':100,'bytes_total':100})
        con.execute("insert into download_tasks values('old','q1','slskd','slskd','slskd','soulseek','SLSKD','old','failed','failed',null,100,?,?,null,null,'{}')", (now - 100, now - 90))
        con.execute("insert into download_tasks values('task','q1','slskd','slskd','slskd','soulseek','SLSKD','remote-1','staged_file_ready','import_ready',null,100,?,?,null,'Powers 011.cbz',?)", (now - 50, now, raw))
        con.execute("insert into import_results values('success',null,'s1','i1','folder_verified',1,1,'folder',0,'optional','folder','/stage/Powers 010.cbz','/library/Powers 010.cbz',?,'{}')", (now - 20,))
        fixtures = [
            ('q2', 'qBittorrent', 'downloading', 'downloading', None),
            ('q3', 'SABnzbd', 'queued', 'queued', now + 600),
            ('q4', 'direct', 'downloading', 'downloading', now - 120),
        ]
        for idx, (queue_id, client, task_state, task_status, retry_after) in enumerate(fixtures, start=2):
            issue_id = f'i{idx}'
            con.execute("insert into issues values(?,?,?,?)", (issue_id, 's1', str(idx), f'Issue {idx}'))
            con.execute("insert into wanted_items values(?,?,?,'in_progress')", (f'w{idx}', 's1', issue_id))
            con.execute("insert into queue_items values(?,?,? ,?,'downloading','download_client',?,'active',?,?,1)", (queue_id, f'w{idx}', 's1', issue_id, retry_after, now, now - 30))
            provider_id = 'prowlarr_torrentleech_comics' if client == 'qBittorrent' else client.lower()
            provider_name = 'TorrentLeech' if client == 'qBittorrent' else client
            protocol = 'torrent' if client == 'qBittorrent' else ('usenet' if client == 'SABnzbd' else 'http')
            attempt_raw = json.dumps({'indexer': provider_name} if client == 'qBittorrent' else {})
            task_raw = json.dumps({
                'bytes_completed': 100,
                'bytes_total': 200,
                'indexer': provider_name,
                'protocol': protocol,
            })
            con.execute("insert into source_attempts values(?,?, 'download_client', ?, ?, ?, 'grab_sent', ?, 'handoff',?,?,null,?)", (f'a{idx}', queue_id, provider_id, provider_name, protocol, client, now - 30, now - 20, attempt_raw))
            con.execute("insert into download_tasks values(?,?, 'download_client', ?, ?, ?, ?, ?, ?, ?, .5, 200,?,?,null,?,?)", (f't{idx}', queue_id, provider_id, provider_name, protocol, client, f'external-{idx}', task_status, task_state, now - 20, now, f'{client} file.cbz', task_raw))
        con.commit(); con.close()

        payload = inkdrop_activity.activity_current(db, limit=20)
        assert payload['total_count'] == 4, payload
        row = next(item for item in payload['activity'] if item['queue_id'] == 'q1')
        assert row['stage'] == 'ready_to_import', row
        assert row['download_client'] == 'SLSKD', row
        assert row['client'] == 'slskd', row
        assert row['provider_id'] == 'slskd', row
        assert row['protocol'] == 'soulseek', row
        assert row['transfer_state'] == 'Completed, Succeeded', row
        assert row['percent_complete'] is None, row
        assert row['next_action'] == 'The download finished. InkDrop will check the file and import it.', row
        assert row['ownership_evidence']['download_task'] == 'task', row
        assert row['ownership_evidence']['completion']['file_present'] is True, row
        assert row['ownership_evidence']['completion']['reader_visible'] is False, row
        assert inkdrop_activity.activity_detail(db, row['activity_id'])['queue_id'] == 'q1'

        requested = inkdrop_activity.activity_current(db, sort='bogus', direction='sideways', filters={'bogus':'x','client':'soulseek'})
        assert requested['applied_sort'] == 'last_updated'
        assert requested['applied_direction'] == 'desc'
        assert requested['unsupported_filters'] == ['bogus']
        assert requested['applied_filters'] == {'client':'slskd'}
        assert requested['total_count'] == 1, requested
        assert requested['activity'][0]['queue_id'] == 'q1', requested
        assert inkdrop_activity.activity_current(db, filters={'client':'qbit'})['total_count'] == 1
        assert inkdrop_activity.activity_current(db, filters={'client':'qbittorrent'})['total_count'] == 1
        qbit_row = inkdrop_activity.activity_current(db, filters={'client':'qbittorrent'})['activity'][0]
        assert qbit_row['provider_id'] == 'prowlarr_torrentleech_comics', qbit_row
        assert qbit_row['provider'] == 'TorrentLeech', qbit_row
        assert qbit_row['child_source_name'] == 'TorrentLeech', qbit_row
        assert qbit_row['protocol'] == 'torrent', qbit_row
        assert inkdrop_activity.activity_current(db, filters={'client':'sab'})['total_count'] == 1
        assert inkdrop_activity.activity_current(db, filters={'client':'sabnzbd'})['total_count'] == 1
        assert inkdrop_activity.activity_current(db, filters={'client':'direct'})['total_count'] == 1
        summary = inkdrop_activity.activity_summary(db)
        assert summary['ready_to_import'] == 1
        assert summary['next_scheduled_worker_run'] == now + 600, summary
        assert summary['scheduler']['next_run_at'] == now + 600, summary
        assert summary['scheduler']['due_now'] is True, summary
        assert summary['scheduler']['overdue'] is True, summary
        assert summary['scheduler']['late_job_count'] == 1, summary
        assert summary['clients']['slskd']['active'] == 1
        for client in ('qbittorrent', 'sabnzbd', 'slskd', 'direct_download'):
            expected = summary['clients'][client].get('active', 0) + summary['clients'][client].get('queued', 0) + summary['clients'][client].get('remote_queued', 0)
            actual = inkdrop_activity.activity_current(db, filters={'client': client})['total_count']
            assert actual == expected, (client, actual, expected, summary['clients'][client])
        assert summary['last_successful_action']['id'] == 'success'
    print('inkdrop activity truth smoke: ok')


if __name__ == '__main__':
    main()
