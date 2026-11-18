import pytest
from app.config import SAMPLE
from app.plan import build_plan, plan_steps, active_receipts

RECEIPT = 'R0001,receipt,0002,B0001,30,0,5,2026-12-02,中央仓,0,2026-11-20,1,normal\n'


def _stock(sku, lot, qty, pack, expiry, frozen=0):
    return {'sku': sku, 'lot': lot, 'qty_n': qty, 'frozen_n': frozen, 'pack_n': pack,
            'expiry': expiry, 'store': '中央仓', 'status': 'normal',
            'arrival': '2026-11-20', 'min_days_n': 0,
            'stock_key': sku + ':' + lot, 'available': (qty - frozen) // pack * pack,
            'record': 'S'}


def _demand(record, sku, qty, store='门店01', min_days=10):
    return {'record': record, 'sku': sku, 'qty_n': qty, 'frozen_n': 0, 'pack_n': 1,
            'store': store, 'arrival': '2026-11-20', 'min_days_n': min_days,
            'net_need': qty}


def _receipt(record, sku, lot, qty, pack=5, expiry='2026-12-02'):
    return {'record': record, 'sku': sku, 'lot': lot, 'qty_n': qty, 'pack_n': pack,
            'expiry': expiry, 'status': 'normal'}


def test_no_receipt_matches_plain_fefo(ctx):
    from app.domain import allocate
    rows, left, short = allocate(ctx['usable'], ctx['net_demands'])
    plan = build_plan(ctx['usable'], ctx['net_demands'])
    assert plan['allocations'] == rows
    assert plan['remaining'] == left
    assert [r['缺口'] for r in plan['gaps']] == [r['缺口'] for r in short]
    assert plan['summary']['专属锁定'] == 0
    assert plan['balanced'] is True


def test_receipt_locks_original_lot_whole_packages():
    usable = [_stock('0001', 'A', 130, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 100)],
                      [_receipt('R1', '0001', 'A', 30)])
    lock = plan['receipts'][0]
    assert lock['专属锁定'] == 30
    assert lock['回执缺量'] == 0
    assert lock['专属锁定'] % lock['整包装'] == 0
    # 同批仍有未锁余量：用同批补足，不算跨批替代。
    assert plan['summary']['替代分配'] == 0
    assert plan['summary']['原批补足'] == 30
    assert plan['summary']['无法替代缺口'] == 0


def test_same_lot_refill_keeps_store_whole():
    usable = [_stock('0001', 'A', 100, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 70)],
                      [_receipt('R1', '0001', 'A', 30)])
    assert plan['receipts'][0]['专属锁定'] == 30
    assert plan['gaps'][0]['缺口'] == 0
    assert plan['gaps'][0]['无法替代缺口'] == 0
    assert plan['balanced'] is True


def test_lock_exhausting_supply_leaves_cannot_substitute_gap():
    usable = [_stock('0001', 'A', 100, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 100)],
                      [_receipt('R1', '0001', 'A', 30)])
    assert plan['receipts'][0]['专属锁定'] == 30
    assert plan['summary']['替代分配'] == 0
    assert plan['gaps'][0]['无法替代缺口'] == 30
    assert plan['balanced'] is True


def test_locked_stock_never_sent_twice():
    usable = [_stock('0001', 'A', 60, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 60)],
                      [_receipt('R1', '0001', 'A', 20)])
    sent = sum(r['调拨数量'] for r in plan['allocations'] if r['批号'] == 'A')
    assert sent + plan['receipts'][0]['专属锁定'] == 60
    assert plan['remaining']['0001:A'] == 0
    assert plan['balanced'] is True


def test_substitute_uses_other_lot_fefo_first():
    usable = [_stock('0001', 'A', 40, 5, '2026-12-02'),
              _stock('0001', 'B', 40, 5, '2026-12-20')]
    plan = build_plan(usable, [_demand('D1', '0001', 40)],
                      [_receipt('R1', '0001', 'A', 20)])
    subs = plan['substitutions']
    assert subs and all(r['批号'] == 'B' for r in subs)
    assert sum(r['调拨数量'] for r in subs) == 20
    assert all(r['原锁定批号'] == 'A' for r in subs)


def test_unsubstitutable_gap_is_explicit():
    usable = [_stock('0001', 'A', 30, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 30)],
                      [_receipt('R1', '0001', 'A', 30)])
    gap = plan['gaps'][0]
    assert gap['缺口'] == 30
    assert gap['无法替代缺口'] == 30
    assert gap['受锁定影响'] == '是'
    assert plan['summary']['替代分配'] == 0
    assert plan['balanced'] is True


def test_receipt_not_matching_stock_reports_shortage():
    usable = [_stock('0001', 'B', 50, 5, '2026-12-20')]
    plan = build_plan(usable, [_demand('D1', '0001', 20)],
                      [_receipt('R1', '0001', 'A', 20)])
    lock = plan['receipts'][0]
    assert lock['专属锁定'] == 0
    assert lock['回执缺量'] == 20
    assert lock['状态'] == '无库存可锁'


def test_receipt_beyond_available_locks_whole_packages():
    usable = [_stock('0001', 'A', 12, 5, '2026-12-02')]
    plan = build_plan(usable, [], [_receipt('R1', '0001', 'A', 20)])
    lock = plan['receipts'][0]
    assert lock['专属锁定'] == 10
    assert lock['回执缺量'] == 10


def test_void_receipt_releases_and_replans():
    usable = [_stock('0001', 'A', 60, 5, '2026-12-02')]
    demands = [_demand('D1', '0001', 60)]
    receipts = [_receipt('R1', '0001', 'A', 20)]
    before = build_plan(usable, demands, receipts)
    after = build_plan(usable, demands, receipts, voids=['R1'], version=2)
    assert before['summary']['专属锁定'] == 20
    assert after['summary']['专属锁定'] == 0
    assert after['summary']['替代分配'] == 0
    assert after['receipts'][0]['专属锁定'] == 0
    assert after['receipts'][0]['状态'] == '已撤销'
    assert after['summary']['已调拨'] == before['summary']['已调拨'] + 20
    assert after['balanced'] is True


def test_reconcile_detects_tampering():
    from app.plan import reconcile
    usable = [_stock('0001', 'A', 60, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 60)],
                      [_receipt('R1', '0001', 'A', 20)])
    rows, balanced, _ = reconcile(usable, plan['allocations'],
                                  {'0001:A': 1}, plan['gaps'], plan['receipts'],
                                  plan['substitutions'], {})
    assert balanced is False
    assert any(r['差额'] for r in rows)


def test_plan_steps_stable_and_nonnegative():
    usable = [_stock('0001', 'A', 60, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 60)],
                      [_receipt('R1', '0001', 'A', 20)])
    first = [s['步骤'] for s in plan_steps(plan)]
    again = [s['步骤'] for s in plan_steps(build_plan(usable, [_demand('D1', '0001', 60)],
                                                      [_receipt('R1', '0001', 'A', 20)]))]
    assert first == again
    assert len(first) == len(set(first))
    assert all(s['数量'] >= 0 for s in plan_steps(plan))


def test_voided_receipt_excluded():
    receipts = [_receipt('R1', '0001', 'A', 10), _receipt('R2', '0001', 'A', 10)]
    assert [r['record'] for r in active_receipts(receipts, ['R1'])] == ['R2']
    assert active_receipts([_receipt('R3', '0001', 'A', 10) | {'status': 'void'}]) == []


def test_store_sku_cannot_use_other_sku_stock():
    usable = [_stock('0002', 'A', 40, 5, '2026-12-02')]
    plan = build_plan(usable, [_demand('D1', '0001', 20)],
                      [_receipt('R1', '0001', 'A', 20)])
    assert plan['summary']['替代分配'] == 0
    assert plan['receipts'][0]['专属锁定'] == 0
    assert plan['receipts'][0]['回执缺量'] == 20
    assert plan['balanced'] is True
