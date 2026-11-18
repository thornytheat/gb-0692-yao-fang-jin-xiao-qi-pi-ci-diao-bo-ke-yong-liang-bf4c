import pytest
from app.config import SAMPLE

RECEIPT = 'R0001,receipt,0002,B0001,30,0,5,2026-12-02,中央仓,0,2026-11-20,1,normal\n'


@pytest.fixture
def seeded(client):
    raw = SAMPLE.read_text(encoding='utf-8-sig')
    result = client.post('/api/run', json={'csv': raw + RECEIPT}).get_json()
    return result['manifest']['run_id'], result


def test_plan_endpoint_matches_stored_snapshot(client, seeded):
    ident, _ = seeded
    plan = client.get('/api/plan?run_id=' + ident).get_json()
    assert plan['version'] == 1
    assert plan['balanced'] is True
    assert plan['receipts'][0]['专属锁定'] == 30
    assert plan['summary']['回执数量'] == 30


def test_plan_requires_run_id(client):
    assert client.get('/api/plan').status_code == 400


def test_execute_partial_then_resume_without_double_count(client, seeded):
    ident, _ = seeded
    first = client.post('/api/plan/execute', json={'run_id': ident, 'fail_after': 4}).get_json()
    assert first['中断'] is True
    assert first['已完成'] == 4
    before = first['占用']['按类型']
    second = client.post('/api/plan/execute', json={'run_id': ident}).get_json()
    assert second['状态'] == '已完成'
    assert second['待执行'] == 0
    assert sum(second['占用']['按类型'].values()) > sum(before.values())
    third = client.post('/api/plan/execute', json={'run_id': ident}).get_json()
    assert third['已完成'] == second['已完成']
    assert third['占用'] == second['占用']


def test_execute_never_exceeds_lot_availability(client, seeded):
    ident, _ = seeded
    result = client.post('/api/plan/execute', json={'run_id': ident}).get_json()
    assert all(check['未超限'] for check in result['按批号校验'])
    assert all(check['占用'] == check['应占'] for check in result['按批号校验'])


def test_ledger_is_stable_across_repeats(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/execute', json={'run_id': ident})
    a = client.get('/api/plan/ledger?run_id=' + ident).get_json()
    client.post('/api/plan/execute', json={'run_id': ident})
    b = client.get('/api/plan/ledger?run_id=' + ident).get_json()
    assert a['占用'] == b['占用']
    assert len(a['步骤']) == len(b['步骤'])


def test_void_releases_lock_and_switches_version(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/execute', json={'run_id': ident})
    result = client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'}).get_json()
    assert result['方案版本'] == 2
    assert result['释放原量'] == 30
    current = client.get('/api/plan?run_id=' + ident).get_json()
    assert current['version'] == 2
    assert current['summary']['专属锁定'] == 0
    assert current['voids'] == ['R0001']
    assert current['receipts'][0]['状态'] == '已撤销'
    assert current['balanced'] is True


def test_void_is_idempotent(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'})
    again = client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'}).get_json()
    assert again['reused'] is True
    assert again['方案版本'] == 2
    assert client.get('/api/plan?run_id=' + ident).get_json()['version'] == 2


def test_void_unknown_receipt_is_404(client, seeded):
    ident, _ = seeded
    assert client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'NOPE'}).status_code == 404
    assert client.post('/api/plan/void', json={'run_id': ident}).status_code == 400


def test_old_version_cannot_execute_after_void(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'})
    response = client.post('/api/plan/execute', json={'run_id': ident, 'version': 1})
    assert response.status_code == 409


def test_void_releases_all_prior_occupancy(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/execute', json={'run_id': ident})
    prior = client.get('/api/plan/ledger?run_id=' + ident + '&version=1').get_json()
    result = client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'}).get_json()
    assert result['释放占用合计'] == sum(prior['占用']['按类型'].values())
    old = client.get('/api/plan/ledger?run_id=' + ident + '&version=1').get_json()
    assert old['方案状态'] == 'superseded'
    assert old['占用']['按类型'] == {}


def test_page_and_evidence_share_plan_version(client, seeded):
    ident, _ = seeded
    page = client.get('/work/5?run_id=' + ident).data.decode()
    evidence = client.get('/api/evidence.csv?run_id=' + ident).data.decode('utf-8-sig')
    assert '方案 v1' in page and 'R0001' in page
    assert '方案回执' in evidence and '方案对账' in evidence and '方案步骤' in evidence
    assert 'R0001' in evidence


def test_evidence_follows_void_version(client, seeded):
    ident, _ = seeded
    client.post('/api/plan/void', json={'run_id': ident, 'receipt': 'R0001'})
    page = client.get('/work/6?run_id=' + ident).data.decode()
    assert '方案 v2' in page
    evidence = client.get('/api/evidence.csv?run_id=' + ident).data.decode('utf-8-sig')
    assert '已撤销' in evidence


def test_run_detail_reflects_current_plan(client, seeded):
    ident, _ = seeded
    detail = client.get('/api/runs/' + ident).get_json()
    assert detail['方案版本'] == 1
    assert detail['stages']['5']['rows'][0]['分配类型'] in ('替代', '普通')


def test_execute_requires_run_id(client):
    assert client.post('/api/plan/execute', json={}).status_code == 400
    assert client.post('/api/plan/execute', json={'run_id': 'absent'}).status_code == 404


def test_bad_version_argument(client, seeded):
    ident, _ = seeded
    assert client.post('/api/plan/execute', json={'run_id': ident, 'version': 'x'}).status_code == 422
    assert client.get('/api/plan/ledger?run_id=' + ident + '&version=x').status_code == 422


def test_no_receipt_sample_still_plans(client):
    result = client.post('/api/run', json={}).get_json()
    ident = result['manifest']['run_id']
    plan = client.get('/api/plan?run_id=' + ident).get_json()
    assert plan['receipts'] == []
    assert plan['summary']['专属锁定'] == 0
    assert plan['summary']['替代分配'] == 0
    assert plan['balanced'] is True
