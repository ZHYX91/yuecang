"""Guided workflows across the public API, queue, and durable retention boundary."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yuecang.api import create_app
from yuecang.jobs import JobQueue, dispatch
from yuecang.storage.maintenance import backup_project, cleanup_cache, restore_project, verify_project
from yuecang.application.service import Workbench
from yuecang.storage.project import DomainError


def app_client(tmp_path):
    app = create_app(tmp_path / 'library', start_worker=False)
    client = TestClient(app)
    headers = {'X-Yuecang-CSRF': client.get('/api/session').json()['csrf_token']}
    return app.state.workbench, client, headers


def test_assignment_job_confirm_correct_and_restore(tmp_path):
    wb, client, headers = app_client(tmp_path)
    path = tmp_path / 'synthetic.txt'
    path.write_bytes('《合成植物手册》\n每日记录盆土湿度，观察叶面颜色并检查排水孔，夜间收回测量工具。\n'.encode())
    source_id = wb.import_file(path)['source_id']
    posted = client.post('/api/assignment-plans', json={'source_ids': [source_id]}, headers=headers)
    assert posted.status_code == 200, posted.text
    job = posted.json()['job']
    queue = JobQueue(wb.project)
    finished = queue.run_one()
    assert finished['state'] == 'succeeded', finished
    job_public = client.get('/api/jobs/' + job['id']).json()
    assert job_public['result']['plan_id'] == finished['result']['plan_id']
    assert not {'payload', 'token', 'checkpoint'} & job_public.keys()
    plan_id = finished['result']['plan_id']
    plan = client.get('/api/assignment-plans/' + plan_id).json()
    decisions = [{'candidate_id': candidate['id'], 'action': 'confirm',
                  'target': {'new_book': {'key': 'plant', 'title': '合成植物手册', 'edition': '初版'}}}
                 for source in plan['sources'] for candidate in source['candidates']]
    response = client.post(f'/api/assignment-plans/{plan_id}/apply', json={'decisions': decisions}, headers=headers)
    assert response.status_code == 200, response.text
    assert client.post(f'/api/assignment-plans/{plan_id}/apply', json={'decisions': decisions}, headers=headers).json() == response.json()
    assignments = client.get(f'/api/sources/{source_id}/assignments').json()
    row = assignments['items'][0]
    expected = {key: row[key] for key in ['source_id', 'block_id', 'start', 'end', 'book_id', 'edition_id', 'blocks_sha']}
    # PATCH cannot silently become DELETE through null target.
    assert client.patch('/api/assignments/' + row['id'], json={'expected': expected, 'target': None}, headers=headers).status_code == 400
    generation = wb.state()['assignment_generation']
    removed = client.request('DELETE', '/api/assignments/' + row['id'], json={'expected': expected}, headers=headers)
    assert removed.status_code == 200, removed.text
    assert wb.state()['assignment_generation'] > generation
    cleanup_cache(wb.project, apply=True)
    assert verify_project(wb.project)['ok']
    backup = backup_project(wb.project)
    restored_path = tmp_path / 'restored'
    restore_project(backup['path'], restored_path)
    from yuecang.application.organization import Organization
    assert Organization(Workbench(restored_path)).get(plan_id)['state'] == 'applied'


def test_scoped_server_search_and_literal_metacharacters(tmp_path):
    wb, client, _ = app_client(tmp_path)
    first = wb.create_book('100%_合成教材', author='合成作者')
    other = wb.create_book('100abc合成教材', author='另一作者')
    assert client.get('/api/books', params={'search': '%_'}).json()['total'] == 1
    assert client.get('/api/books', params={'search': '合成作者'}).json()['items'][0]['id'] == first['id']
    assert client.get('/api/editions', params={'book_id': first['id']}).json()['total'] == 1
    assert client.get('/api/editions', params={'book_id': other['id']}).json()['items'][0]['id'] == other['edition_id']
    assert client.get('/api/books', params={'kind': 'ignored'}).status_code == 400
    assert client.get('/api/books', params={'offset': 2**63}).status_code == 400


def test_create_edition_invalidates_scoped_collection(tmp_path):
    wb, client, headers = app_client(tmp_path)
    book = wb.create_book('合成初版')
    event_id = client.get('/api/state').json()['event_id']
    response = client.post(f"/api/books/{book['id']}/editions", json={'label': '合成修订本'}, headers=headers)
    assert response.status_code == 200, response.text
    assert client.get('/api/state').json()['event_id'] > event_id
    editions = client.get('/api/editions', params={'book_id': book['id'], 'search': '修订本'}).json()
    assert editions['total'] == 1
    assert editions['items'][0]['id'] == response.json()['id'] and editions['items'][0]['label'] == '合成修订本'


def test_job_search_state_groups_and_pagination(tmp_path):
    wb, client, _ = app_client(tmp_path)
    queue = JobQueue(wb.project)
    queued = queue.enqueue('assignment_plan', {'source_ids': ['queued']})
    paused = queue.enqueue('assignment_plan', {'source_ids': ['paused']})
    queue.pause(paused['id'])
    cancelled = queue.enqueue('assignment_plan', {'source_ids': ['cancelled']})
    queue.cancel(cancelled['id'])
    query = {'search': 'assignment_plan', 'state': 'queued,paused', 'limit': 1}
    first = client.get('/api/jobs', params=query).json()
    assert first['total'] == 2 and first['next_offset'] == 1 and first['items'][0]['id'] == paused['id']
    second = client.get('/api/jobs', params={**query, 'offset': first['next_offset']}).json()
    assert second['total'] == 2 and second['next_offset'] is None and second['items'][0]['id'] == queued['id']
    assert client.get('/api/jobs', params={'search': cancelled['id']}).json()['total'] == 1
    assert client.get('/api/jobs', params={'state': 'queued,'}).status_code == 400
    assert client.get('/api/state').json()['state_counts']['jobs'] == {'cancelled': 1, 'paused': 1, 'queued': 1}


def test_composite_curation_job_retry_assemble_backup_restore(tmp_path):
    wb, client, headers = app_client(tmp_path)
    queue = JobQueue(wb.project)
    first_backup = client.post('/api/maintenance/backup', headers=headers).json()['job']
    first_snapshot = queue.run_one()['result']['path']
    book = wb.create_book('合成水文手册')
    other_edition = wb.create_edition(book['id'], '异版样本')
    first = '巡查记录指出，测量人员每天在水文站对照原始刻度并记录读数，随后检查测量器具。'
    middle = '实验组在清晨完成浮标检查，并对每个点位分别登记可见变化。'
    last = '样本按采集时间排序归档，设备重新放入实验室内的专用储物柜。'

    def assigned_source(name, edition, paragraph):
        path = tmp_path / name
        path.write_bytes('\n\n'.join([first, paragraph, last]).encode())
        result = wb.source(wb.import_file(path)['source_id'])
        wb.assign(book['id'], edition, [{'source_id': result['id'], 'block_id': b['id']} for b in result['blocks']])
        return result['id']

    base = assigned_source('base.txt', book['edition_id'], middle)
    donor = assigned_source('donor.txt', other_edition['id'], middle.replace('清晨', '黄昏'))
    assignments_before = wb.state()['assignments']
    url = f"/api/books/{book['id']}/curation-plans"
    scope = {'edition_id': book['edition_id'], 'base_source_id': base, 'donor_source_ids': [donor]}
    assert client.post(url, headers=headers, json={**scope, 'allow_composite': 1}).status_code == 400
    posted = client.post(url, headers=headers, json={**scope, 'allow_composite': True})
    assert posted.status_code == 200, posted.text
    job_id = posted.json()['job']['id']
    # The plan may commit even if an interrupted worker never records its result.
    claimed = queue.claim()
    committed = dispatch(wb.project, claimed)
    queue.fail(job_id, claimed['token'], 'synthetic_interruption')
    assert client.post(f'/api/jobs/{job_id}/retry', headers=headers).status_code == 200
    assert queue.run_one()['state'] == 'succeeded'
    assert client.get(f'/api/jobs/{job_id}').json()['result'] == committed
    plan_url = '/api/curation-plans/' + committed['plan_id']
    plan = client.get(plan_url).json()
    conflict = next(row for row in plan['rows'] if row['kind'] == 'conflict')
    chosen = next(option for option in conflict['options'] if option['spans'][0]['source_id'] == donor)
    title = '合成组稿 100%_样本'
    confirmation = {'snapshot_sha': plan['snapshot_sha'], 'choices': {conflict['id']: chosen['id']}, 'title': title}
    assert client.post(plan_url + '/assemble', headers=headers, json={**confirmation, 'snapshot_sha': 'old'}).status_code == 409
    assembled = client.post(plan_url + '/assemble', headers=headers, json=confirmation)
    assert assembled.status_code == 200, assembled.text
    draft = assembled.json()['revision']
    assert draft['metadata']['assembly_policy'] == 'composite-confirmed'
    assert {i['severity'] for i in draft['issues'] if i['code'] == 'edition_conflict'} == {'warning'}
    assert draft['blocks'][1]['source_span']['source_id'] == donor and '黄昏' in draft['blocks'][1]['text']
    assert wb.state()['assignments'] == assignments_before
    assert client.get('/api/revisions', params={'search': '%_'}).json()['items'][0]['id'] == draft['id']
    summary = client.get(f"/api/revisions/{draft['id']}", params={'summary': 'true'}).json()
    assert summary['metadata'] == draft['metadata'] and not {'blocks', 'changes'} & summary.keys()
    assert client.get(f"/api/branches/{assembled.json()['branch_id']}").json()['head_revision_id'] == draft['id']

    source_span = draft['blocks'][1]['source_span']
    expected_ranges = [{**source_span, 'edition_id': other_edition['id']}]
    assert draft['blocks'][1]['source_edition_ranges'] == expected_ranges
    assert draft['metadata']['source_editions'][other_edition['id']]['label'] == '异版样本'
    donor_assignments = client.get(f'/api/sources/{donor}/assignments').json()['items']
    used = next(item for item in donor_assignments if item['block_id'] == source_span['block_id'])
    expected = {key: used[key] for key in ('source_id', 'block_id', 'start', 'end', 'book_id', 'edition_id', 'blocks_sha')}
    correction = client.patch(f"/api/assignments/{used['id']}", headers=headers, json={
        'expected': expected, 'target': {'book_id': book['id'], 'edition_id': book['edition_id']},
    })
    assert correction.status_code == 200 and correction.json()['assignment']['edition_id'] == book['edition_id']
    assert client.get(f"/api/revisions/{draft['id']}").json() == draft
    export = client.post('/api/exports', headers=headers, json={'revision_id': draft['id'], 'allow_draft': True})
    assert export.status_code == 200, export.text
    exported = queue.run_one()
    assert exported['state'] == 'succeeded', exported
    destination = Path(exported['result']['report']['destination'])
    exported_revision = json.loads((destination / 'revision.json').read_text(encoding='utf-8'))
    assert exported_revision['blocks'][1]['source_edition_ranges'] == expected_ranges
    assert exported_revision['metadata']['source_editions'] == draft['metadata']['source_editions']
    assert client.get('/api/exports', params={'search': title}).json()['total'] == 1

    orphan = wb.project.put_json({'orphan': 'synthetic'}, 'curation_plan')
    cleanup_cache(wb.project, apply=True, minimum_age_seconds=0)
    assert not wb.project.object_path(orphan).exists()
    assert verify_project(wb.project)['ok']
    second_backup = client.post('/api/maintenance/backup', headers=headers).json()['job']
    assert second_backup['id'] != first_backup['id']
    second_snapshot = queue.run_one()['result']['path']
    assert second_snapshot != first_snapshot
    restored = tmp_path / 'restored'
    assert restore_project(second_snapshot, restored)['ok']
    restored_client = TestClient(create_app(restored, start_worker=False))
    restored_headers = {'X-Yuecang-CSRF': restored_client.get('/api/session').json()['csrf_token']}
    assert restored_client.get(plan_url).json()['state'] == 'applied'
    assert restored_client.get(f"/api/revisions/{draft['id']}").json() == draft
    replay = restored_client.post(plan_url + '/assemble', headers=restored_headers, json=confirmation)
    assert replay.status_code == 200 and replay.json()['revision_id'] == draft['id']

    with wb.project.transaction() as db:
        sha = db.execute("SELECT sha FROM object_refs WHERE owner='curation_plan' AND owner_id=?", (plan['id'],)).fetchone()[0]
        db.execute("DELETE FROM object_refs WHERE owner='curation_plan' AND owner_id=?", (plan['id'],))
        wb.project.ref(db, 'curation_plan', 'unrelated-plan', sha, 'payload')
    for action in (verify_project, backup_project):
        with pytest.raises(DomainError) as exc:
            action(wb.project)
        assert exc.value.code == 'object_reference_missing'


def test_malformed_guided_and_selection_ids_return_client_errors(tmp_path):
    wb, client, headers = app_client(tmp_path)
    book = wb.create_book('合成作品')
    bad_assignment = client.post('/api/assignment-plans', headers={**headers, 'Content-Type': 'application/json'},
                                 content=b'{"source_ids":["\\ud800"]}')
    assert bad_assignment.status_code == 400
    url = f"/api/books/{book['id']}/assemble"
    assert client.post(url, headers=headers, json={'selections': [[]]}).status_code == 400
    assert client.post(url, headers=headers, json={'selections': [{'source_id': [], 'block_id': 'b'}]}).status_code == 400


def test_assembly_precondition_is_in_the_write_transaction(tmp_path):
    wb, _, _ = app_client(tmp_path)
    path = tmp_path / 'fixture.txt'
    path.write_bytes('有来源的合成正文。'.encode())
    source = wb.source(wb.import_file(path)['source_id'])
    book = wb.create_book('合成')
    spans = [{'source_id': source['id'], 'block_id': b['id']} for b in source['blocks']]
    def stale(db):
        assert db.in_transaction
        raise DomainError('plan_stale', '合成过期计划', 409)
    with pytest.raises(DomainError, match='过期'):
        wb.assemble(book['id'], spans, precondition=stale)
    assert not wb.state()['revisions'] and not wb.state()['assignments']
