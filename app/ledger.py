"""方案执行账本：分步提交、幂等重放、撤销回执后换版。

账本按 (run_id, 方案版本, 步骤键) 唯一。同一版本重复执行只补做未完成步骤，
不追加占用；中断后再次执行从第一个未完成步骤继续。

每版方案在建立时把完整方案（含库存快照）存入 plans.payload，
页面、证据导出与执行都读取这同一份快照，保证三者对应同一方案版本。
"""
import json
import time
from collections import defaultdict

from app.errors import DomainError
from app.plan import build_plan, plan_steps
from app.store import connection, get_run

STOCK_STEP = ('回执锁定', '替代分配', '原批补足', '普通调拨')
DEMAND_STEP = ('替代分配', '原批补足', '普通调拨')
SNAPSHOT_STOCK = ('sku', 'lot', 'expiry', 'pack_n', 'available', 'stock_key', 'status',
                  'arrival', 'min_days_n')
SNAPSHOT_DEMAND = ('record', 'sku', 'store', 'arrival', 'min_days_n', 'net_need')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS plans(
        run_id TEXT NOT NULL, version INTEGER NOT NULL,
        voids TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'ready',
        payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(run_id, version))''')
    db.execute('''CREATE TABLE IF NOT EXISTS plan_steps(
        run_id TEXT NOT NULL, version INTEGER NOT NULL, step_key TEXT NOT NULL,
        kind TEXT NOT NULL, ref TEXT NOT NULL, sku TEXT NOT NULL, lot TEXT NOT NULL,
        qty INTEGER NOT NULL, status TEXT NOT NULL, applied_at TEXT NOT NULL,
        PRIMARY KEY(run_id, version, step_key))''')


def _plan_of(payload, version, voids):
    plan = build_plan(payload['usable'], payload['net_demands'],
                      payload.get('receipts', ()), voids, version)
    # 方案快照自带库存与需求的最小投影，执行校验与页面渲染都不再依赖 payload。
    plan['usable'] = [{k: s.get(k) for k in SNAPSHOT_STOCK} for s in payload['usable']]
    plan['net_demands'] = [{k: d.get(k) for k in SNAPSHOT_DEMAND}
                           for d in payload['net_demands']]
    plan['steps'] = plan_steps(plan)
    return plan


def _insert(db, run_id, version, voids, status, plan, created=None):
    now = _now()
    db.execute('INSERT INTO plans(run_id,version,voids,status,payload,created_at,updated_at)'
               ' VALUES (?,?,?,?,?,?,?)',
               (run_id, version, json.dumps(sorted(voids)), status,
                json.dumps(plan, ensure_ascii=False), created or now, now))


def _row(db, run_id, version=None):
    _ensure(db)
    if version is None:
        return db.execute('SELECT * FROM plans WHERE run_id=? ORDER BY version DESC LIMIT 1',
                          (run_id,)).fetchone()
    return db.execute('SELECT * FROM plans WHERE run_id=? AND version=?',
                      (run_id, version)).fetchone()


def _load(row):
    return {**json.loads(row['payload']), 'version': row['version'],
            'voids': json.loads(row['voids']), 'status': row['status']}


def ensure(run_id, version=1, voids=()):
    """确保某版方案存在并返回它；已存在时原样返回，不重算。"""
    with connection() as db:
        row = _row(db, run_id, version)
        if row is None:
            payload = get_run(run_id)
            plan = _plan_of(payload, version, voids)
            _insert(db, run_id, version, voids, 'ready', plan)
            row = _row(db, run_id, version)
    return _load(row)


def current(run_id):
    """读取当前生效版本：最新的非 superseded 版本，没有则建立第 1 版。"""
    with connection() as db:
        _ensure(db)
        row = db.execute(
            'SELECT * FROM plans WHERE run_id=? AND status!="superseded"'
            ' ORDER BY version DESC LIMIT 1', (run_id,)).fetchone()
        if row is None:
            row = _row(db, run_id)
    return _load(row) if row else ensure(run_id)


def _applied(db, run_id, version):
    return [dict(r) for r in db.execute(
        'SELECT step_key,kind,ref,sku,lot,qty FROM plan_steps'
        ' WHERE run_id=? AND version=? AND status="applied" ORDER BY rowid',
        (run_id, version))]


def _occupancy(rows):
    lot, demand, kind = defaultdict(int), defaultdict(int), defaultdict(int)
    for row in rows:
        kind[row['kind']] += row['qty']
        if row['kind'] in STOCK_STEP:
            lot[row['sku'] + ':' + row['lot']] += row['qty']
        if row['kind'] in DEMAND_STEP:
            demand[row['ref']] += row['qty']
    return {'按批号': dict(sorted(lot.items())),
            '按需求': dict(sorted(demand.items())),
            '按类型': dict(sorted(kind.items()))}


def _check(plan, steps):
    """占用上限：同一批号的锁定+替代+普通调拨 ≤ 该批可调拨量；
    同一需求单的替代+普通调拨 ≤ 净需求。"""
    available = {s.get('stock_key', s['sku'] + ':' + s['lot']): s['available']
                 for s in plan['usable']}
    net_need = {d['record']: d['net_need'] for d in plan.get('net_demands', ())}
    used_lot, used_demand = defaultdict(int), defaultdict(int)
    for step in steps:
        if step['类型'] in STOCK_STEP:
            used_lot[step['药品'] + ':' + step['批号']] += step['数量']
        if step['类型'] in DEMAND_STEP:
            used_demand[step['关联']] += step['数量']
    for key, qty in used_lot.items():
        if qty > available.get(key, 0):
            raise DomainError('PLAN_CONFLICT',
                              '批号 %s 占用 %d 超过可调拨量 %d' % (key, qty, available.get(key, 0)))
    for record, qty in used_demand.items():
        if qty > net_need.get(record, qty):
            raise DomainError('PLAN_CONFLICT',
                              '需求单 %s 占用 %d 超过净需求 %d' % (record, qty, net_need.get(record, qty)))


def _report(run_id, version, plan, applied, interrupted=False):
    total = len(plan['steps'])
    expected = defaultdict(int)
    for step in plan['steps']:
        if step['类型'] in STOCK_STEP:
            expected[step['药品'] + ':' + step['批号']] += step['数量']
    actual = _occupancy(applied)['按批号']
    available = {s.get('stock_key', s['sku'] + ':' + s['lot']): s['available']
                 for s in plan['usable']}
    checks = []
    for key in sorted(expected):
        ceiling = available.get(key, 0) - plan['remaining'].get(key, 0)
        used = actual.get(key, 0)
        checks.append({
            '批号': key,
            '占用': used,
            '应占': expected[key],
            '可调拨量': available.get(key, 0),
            '上限': ceiling,
            '结余': plan['remaining'].get(key, 0),
            '未超限': used <= available.get(key, 0),
            '与方案一致': used == ceiling,
        })
    return {
        'run_id': run_id, '方案版本': version,
        '步骤总数': total, '已完成': len(applied), '待执行': total - len(applied),
        '状态': '已中断，可从已完成步骤继续' if interrupted else (
            '已完成' if len(applied) >= total else '部分完成'),
        '中断': interrupted,
        '占用': _occupancy(applied),
        '按批号校验': checks,
    }


def execute(run_id, version=None, fail_after=None):
    """执行或继续执行；重复调用不追加占用。"""
    if version is None:
        plan = current(run_id)
    else:
        with connection() as db:
            row = _row(db, run_id, version)
        if row is None:
            raise DomainError('PLAN', '方案版本不存在', 404)
        plan = _load(row)
    version = plan['version']
    if plan['status'] in ('applied',):
        with connection() as db:
            applied = _applied(db, run_id, version)
        return _report(run_id, version, plan, applied)
    if plan['status'] == 'superseded':
        raise DomainError('PLAN', '方案版本已撤销，请执行当前版本', 409)
    _check(plan, plan['steps'])

    with connection() as db:
        _ensure(db)
        done = {r['step_key'] for r in _applied(db, run_id, version)}
        added = 0
        for step in plan['steps']:
            if step['步骤'] in done:
                continue
            if fail_after is not None and added >= fail_after:
                db.execute('UPDATE plans SET status=?, updated_at=?'
                           ' WHERE run_id=? AND version=?',
                           ('partial', _now(), run_id, version))
                applied = _applied(db, run_id, version)
                return _report(run_id, version, plan, applied, interrupted=True)
            cursor = db.execute(
                'INSERT OR IGNORE INTO plan_steps(run_id,version,step_key,kind,ref,sku,lot,'
                'qty,status,applied_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (run_id, version, step['步骤'], step['类型'], step['关联'],
                 step['药品'], step['批号'], step['数量'], 'applied', _now()))
            if cursor.rowcount:
                added += 1
            done.add(step['步骤'])
        complete = len(done) >= len(plan['steps'])
        db.execute('UPDATE plans SET status=?, updated_at=? WHERE run_id=? AND version=?',
                   ('applied' if complete else 'partial', _now(), run_id, version))
        applied = _applied(db, run_id, version)
    return _report(run_id, version, plan, applied)


def ledger(run_id, version=None):
    if version is None:
        plan = current(run_id)
    else:
        with connection() as db:
            row = _row(db, run_id, version)
        if row is None:
            raise DomainError('PLAN', '方案版本不存在', 404)
        plan = _load(row)
    with connection() as db:
        rows = [dict(r) for r in db.execute(
            'SELECT step_key,kind,ref,sku,lot,qty,status,applied_at FROM plan_steps'
            ' WHERE run_id=? AND version=? ORDER BY rowid', (run_id, plan['version']))]
    applied = [r for r in rows if r['status'] == 'applied']
    return {'run_id': run_id, '方案版本': plan['version'], '方案状态': plan['status'],
            '占用': _occupancy(applied),
            '步骤': [{'步骤': r['step_key'], '类型': r['kind'], '关联': r['ref'],
                      '药品': r['sku'], '批号': r['lot'], '数量': r['qty'],
                      '状态': '已占用' if r['status'] == 'applied' else r['status'],
                      '时间': r['applied_at']} for r in rows]}


def void_receipt(run_id, receipt):
    """撤销回执：释放旧版全部占用，按先到期重算并切换到新版本。

    旧版与新版的步骤键会重叠，保留旧占用会让同一批库存被两版各算一次，
    因此撤销时把旧版所有已执行步骤一并标记为 released，并报告释放合计。
    """
    payload = get_run(run_id)
    previous = current(run_id)
    if receipt not in {r['record'] for r in payload.get('receipts', ())}:
        raise DomainError('RECEIPT', '回执不存在', 404)
    if receipt in previous['voids']:
        return {'run_id': run_id, '回执': receipt, '方案版本': previous['version'],
                'reused': True, '释放原量': 0,
                'message': '回执已撤销，方案版本未变'}
    voids = sorted(set(previous['voids']) | {receipt})
    version = previous['version'] + 1
    receipt_lock = sum(r['专属锁定'] for r in previous['receipts'] if r['回执'] == receipt)
    with connection() as db:
        _ensure(db)
        released_rows = [dict(r) for r in db.execute(
            'SELECT kind,ref,sku,lot,qty FROM plan_steps'
            ' WHERE run_id=? AND version=? AND status="applied"',
            (run_id, previous['version']))]
        db.execute('UPDATE plans SET status=?, updated_at=? WHERE run_id=? AND version=?',
                   ('superseded', _now(), run_id, previous['version']))
        db.execute('UPDATE plan_steps SET status=?, applied_at=?'
                   ' WHERE run_id=? AND version=? AND status="applied"',
                   ('released', _now(), run_id, previous['version']))
        if _row(db, run_id, version) is None:
            _insert(db, run_id, version, voids, 'ready', _plan_of(payload, version, voids))
        row = _row(db, run_id, version)
    released_total = sum(r['qty'] for r in released_rows)
    return {'run_id': run_id, '回执': receipt, '方案版本': version,
            '释放原量': receipt_lock, '释放占用合计': released_total,
            '释放步骤': len(released_rows), 'reused': False,
            '方案': _load(row),
            'message': '已撤销回执、释放原锁定量并按先到期重算，切换至方案版本 %d' % version}
