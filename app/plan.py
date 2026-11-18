"""退货回执专属锁定与门店替代分配。

纯确定性计算，不读写数据库。

口径（同一批库存只算一次）：

1. 先对全部可调拨库存跑基准 FEFO，得到基准调拨与基准剩余；
2. 回执在原批号上按整包装锁定，锁定量从基准调拨中剥离，不再发给门店；
3. 被剥离的数量先按 FEFO 从“其它合格批号”补足，记为替代分配；
4. 其它批号不足时，才用被锁批号未锁的余量补足，记为原批补足；
5. 仍补不足的部分是“无法替代缺口”，显式登记，不静默归零；
6. 未受锁定影响的门店分配原样保留，不会因替代而挤占。

对账恒等式（每条对账行差额必须为零）：

* 库存：期初 = 专属锁定 + 已调拨 + 结余；
* 需求：净需求 = 已调拨 + 缺口，且缺口 = 基准缺口 + 无法替代缺口；
* 回执：回执数量 = 专属锁定 + 回执缺量；
* 锁定：专属锁定 = 替代分配 + 原批补足 + 无法替代缺口 + 锁定结余。

“锁定结余”是回执锁下、基准方案本来也不会发出的整包装余量。
"""
from collections import defaultdict
from math import lcm

from app.domain import allocate, day, eligible

VOID_STATUS = {'void', 'cancelled', 'canceled', '撤销'}
CORE_FIELDS = ('需求单', '门店', '药品', '批号', '到期日', '调拨数量', '整包装', '到店余期_天')
BLANK_SOURCE = {'原锁定批号': '', '原锁定药品': ''}


def stock_key(stock):
    return stock.get('stock_key') or stock['sku'] + ':' + stock['lot']


def receipt_key(row):
    return row['sku'] + ':' + row['lot']


def demand_order(demand):
    return (demand['arrival'], demand['store'], demand['record'])


def active_receipts(receipts, voids=()):
    """排除来源即作废(status)与本次已撤销(voids)的回执。"""
    voids = set(voids or ())
    result = []
    for row in receipts or ():
        if row['record'] in voids:
            continue
        if str(row.get('status', '')).strip().lower() in VOID_STATUS:
            continue
        result.append(row)
    return sorted(result, key=lambda r: (r['record'], r['sku'], r['lot']))


def _skip(row, voids):
    return row['record'] in voids or str(row.get('status', '')).strip().lower() in VOID_STATUS


def lock_receipts(usable, receipts, voids=()):
    """按回执锁定原批号整包装库存，返回锁定明细、锁定后余量、按批号锁定量。

    已撤销或来源作废的回执仍列出，但锁定与缺量都归零，便于核对撤销前后。
    """
    voids = set(voids or ())
    stocks = {stock_key(s): s for s in usable}
    left = {stock_key(s): s['available'] for s in usable}
    locked = defaultdict(int)
    rows = []
    for row in receipts:
        key = receipt_key(row)
        stock = stocks.get(key)
        want, pack = row['qty_n'], row['pack_n']
        if _skip(row, voids):
            take, short = 0, 0
            detail = '回执已撤销，原锁定已释放' if row['record'] in voids else '来源标记作废，未参与分配'
            status = '已撤销' if row['record'] in voids else '来源已作废'
        elif stock is None:
            take, short = 0, want
            detail, status = '原批号无可用库存', '无库存可锁'
        else:
            # 同时满足回执包装与库存包装，保住回执上的整包装数量。
            unit = pack if pack == stock['pack_n'] else lcm(pack, stock['pack_n'])
            take = min(want, left[key]) // unit * unit
            left[key] -= take
            locked[key] += take
            short = want - take
            if take == want:
                detail, status = '', '已锁定'
            elif take == 0:
                detail, status = '原批号整包装不足', '无库存可锁'
            else:
                detail, status = '原批号库存不足', '部分锁定'
        rows.append({
            '回执': row['record'],
            '药品': row['sku'],
            '原批号': row['lot'],
            '原到期日': stock['expiry'] if stock else row.get('expiry', ''),
            '回执数量': want,
            '整包装': pack,
            '专属锁定': take,
            '回执缺量': short,
            '说明': detail,
            '状态': status,
        })
    return rows, left, dict(locked)


def _days(stock, demand):
    return (day(stock['expiry']) - day(demand['arrival'])).days


def _take(demand, target, remaining, stocks, exclude=()):
    """从 remaining 按 FEFO 为单个需求取整包装，exclude 中的批号不参与。"""
    rows = []
    need = target
    exclude = set(exclude)
    for stock in sorted(stocks, key=lambda s: (s['expiry'], s['lot'])):
        if need <= 0:
            break
        if stock_key(stock) in exclude or not eligible(stock, demand):
            continue
        key = stock_key(stock)
        take = min(remaining.get(key, 0), need) // stock['pack_n'] * stock['pack_n']
        if take <= 0:
            continue
        remaining[key] -= take
        need -= take
        rows.append({
            '需求单': demand['record'],
            '门店': demand['store'],
            '药品': demand['sku'],
            '批号': stock['lot'],
            '到期日': stock['expiry'],
            '调拨数量': take,
            '整包装': stock['pack_n'],
            '到店余期_天': _days(stock, demand),
        })
    return rows


def _strip_locked(baseline_rows, locked):
    """从基准分配中剥离被锁数量，返回保留行、按需求归属的失量、锁定结余。"""
    pool = dict(locked)
    residual = dict(locked)
    lost = defaultdict(lambda: defaultdict(int))
    kept = []
    for row in baseline_rows:
        key = row['药品'] + ':' + row['批号']
        qty = row['调拨数量']
        if qty <= 0:
            continue
        take = min(pool.get(key, 0), qty)
        if take > 0:
            lost[row['需求单']][key] += take
            pool[key] = pool.get(key, 0) - take
            residual[key] = residual.get(key, 0) - take
        if take < qty:
            kept.append({**row, '调拨数量': qty - take})
    return kept, lost, {k: v for k, v in residual.items() if v}


def _fill(demands, lost, remaining, stocks):
    """按需求补足被锁数量：先其它批号，再本批未锁余量，余下记缺口。

    返回补足行与按需求的替代/原批补足汇总；缺口由调用方按实际分配量统一计算。
    """
    rows = []
    substituted, same_lot = defaultdict(int), defaultdict(int)
    for demand in sorted(demands, key=demand_order):
        record = demand['record']
        for key, qty in sorted(lost.get(record, {}).items()):
            if qty <= 0:
                continue
            sku, lot = key.split(':', 1)
            need = qty
            others = _take(demand, need, remaining, stocks, exclude={key})
            for row in others:
                rows.append({**row, '分配类型': '替代', '原锁定批号': lot,
                             '原锁定药品': sku})
            filled = sum(r['调拨数量'] for r in others)
            substituted[record] += filled
            need -= filled
            if need > 0:
                # 仅允许动用该批自身未锁余量，避免误耗其它批号。
                other_keys = {stock_key(s) for s in stocks if stock_key(s) != key}
                refill = _take(demand, need, remaining, stocks, exclude=other_keys)
                for row in refill:
                    rows.append({**row, '分配类型': '原批补足', '原锁定批号': lot,
                                 '原锁定药品': sku})
                got = sum(r['调拨数量'] for r in refill)
                same_lot[record] += got
                need -= got
    return rows, substituted, same_lot


def _reconcile_row(scope, project, sku, opening, lock, sent, left, need, gap,
                   receipt, receipt_short, substitute, refill, residual, hard, diff):
    return {
        '维度': scope, '项目': project, '药品': sku,
        '期初': opening, '专属锁定': lock, '已调拨': sent, '结余或缺口': left,
        '净需求': need, '缺口': gap, '回执数量': receipt, '回执缺量': receipt_short,
        '替代分配': substitute, '原批补足': refill, '锁定结余': residual,
        '无法替代缺口': hard, '差额': diff,
    }


def reconcile(usable, allocations, remaining, gaps, receipts, residual,
              substitutions=(), refills=(), sent_override=None):
    """按传入的实际状态对账；篡改任一桶都会体现为差额。"""
    usable = list(usable or ())
    allocations = list(allocations or ())
    gaps = list(gaps or ())
    receipts = list(receipts or ())
    substitutions = list(substitutions or ())
    refills = list(refills or ())
    residual = dict(residual or {})

    live = [r for r in receipts if r.get('状态') not in ('已撤销', '来源已作废')]
    opening = sum(s['available'] for s in usable)
    lock = sum(r['专属锁定'] for r in receipts)
    sent = sum(r['调拨数量'] for r in allocations)
    if sent_override is not None:
        sent = sent_override
    left = sum(remaining.values())
    need = sum(r['净需求'] for r in gaps)
    gap = sum(r['缺口'] for r in gaps)
    base_gap = sum(r.get('基准缺口', r['缺口']) for r in gaps)
    receipt_qty = sum(r['回执数量'] for r in live)
    receipt_short = sum(r['回执缺量'] for r in live)
    substitute = sum(r['调拨数量'] for r in substitutions)
    refill = sum(r['调拨数量'] for r in refills)
    residual_total = sum(residual.values())
    hard = sum(max(0, r['缺口'] - r.get('基准缺口', r['缺口'])) for r in gaps)

    rows = [
        _reconcile_row('合计', '库存', '', opening, lock, sent, left, 0, 0, 0, 0,
                       substitute, refill, residual_total, hard,
                       opening - lock - sent - left),
        _reconcile_row('合计', '需求', '', 0, 0, sent, 0, need, gap, 0, 0,
                       substitute, refill, residual_total, hard, need - sent - gap),
        _reconcile_row('合计', '回执', '', 0, lock, 0, 0, 0, 0, receipt_qty, receipt_short,
                       substitute, refill, residual_total, hard,
                       receipt_qty - lock - receipt_short),
        _reconcile_row('合计', '锁定', '', 0, lock, 0, 0, 0, 0, 0, 0,
                       substitute, refill, residual_total, hard,
                       lock - substitute - refill - residual_total - hard),
    ]
    skus = sorted({s['sku'] for s in usable}
                  | {r['药品'] for r in allocations}
                  | {r['药品'] for r in gaps}
                  | {r['药品'] for r in receipts})
    for sku in skus:
        a = [r for r in allocations if r['药品'] == sku]
        g = [r for r in gaps if r['药品'] == sku]
        rc = [r for r in receipts if r['药品'] == sku]
        rl = [r for r in rc if r.get('状态') not in ('已撤销', '来源已作废')]
        sb = [r for r in substitutions if r['药品'] == sku]
        rf = [r for r in refills if r['药品'] == sku]
        sku_open = sum(s['available'] for s in usable if s['sku'] == sku)
        sku_lock = sum(r['专属锁定'] for r in rc)
        sku_sent = sum(r['调拨数量'] for r in a)
        sku_left = sum(q for k, q in remaining.items() if k.split(':', 1)[0] == sku)
        sku_need = sum(r['净需求'] for r in g)
        sku_gap = sum(r['缺口'] for r in g)
        sku_rq = sum(r['回执数量'] for r in rl)
        sku_rs = sum(r['回执缺量'] for r in rl)
        sku_sub = sum(r['调拨数量'] for r in sb)
        sku_ref = sum(r['调拨数量'] for r in rf)
        sku_res = sum(q for k, q in residual.items() if k.split(':', 1)[0] == sku)
        sku_hard = sum(max(0, r['缺口'] - r.get('基准缺口', r['缺口'])) for r in g)
        rows += [
            _reconcile_row('药品', '库存', sku, sku_open, sku_lock, sku_sent, sku_left,
                           0, 0, 0, 0, sku_sub, sku_ref, sku_res, sku_hard,
                           sku_open - sku_lock - sku_sent - sku_left),
            _reconcile_row('药品', '需求', sku, 0, 0, sku_sent, 0, sku_need, sku_gap,
                           0, 0, sku_sub, sku_ref, sku_res, sku_hard,
                           sku_need - sku_sent - sku_gap),
            _reconcile_row('药品', '回执', sku, 0, sku_lock, 0, 0, 0, 0,
                           sku_rq, sku_rs, 0, 0, sku_res, sku_hard,
                           sku_rq - sku_lock - sku_rs),
            _reconcile_row('药品', '锁定', sku, 0, sku_lock, 0, 0, 0, 0, 0, 0,
                           sku_sub, sku_ref, sku_res, sku_hard,
                           sku_lock - sku_sub - sku_ref - sku_res - sku_hard),
        ]
    balanced = left >= 0 and all(r['差额'] == 0 for r in rows)
    summary = {'期初': opening, '专属锁定': lock, '已调拨': sent, '结余': left,
               '净需求': need, '缺口': gap, '基准缺口': base_gap,
               '回执数量': receipt_qty, '回执缺量': receipt_short,
               '替代分配': substitute, '原批补足': refill, '锁定结余': residual_total,
               '无法替代缺口': hard}
    return rows, balanced, summary


def build_plan(usable, demands, receipts=(), voids=(), version=1):
    """构建一版方案；没有有效回执时与直接 FEFO 完全一致。"""
    usable, demands = list(usable), list(demands)
    locks, _, locked = lock_receipts(usable, receipts, voids)
    baseline_rows, baseline_left, base_short = allocate(usable, demands)
    base_gap = {r['需求单']: r['缺口'] for r in base_short}

    if locked:
        kept_rows, lost, residual = _strip_locked(baseline_rows, locked)
    else:
        kept_rows, lost, residual = baseline_rows, {}, {}
    # 补货池 = 基准剩余 - 回执锁定但基准未发出的余量（这部分为回执保留）。
    remaining = dict(baseline_left)
    for key, qty in residual.items():
        remaining[key] = remaining.get(key, 0) - qty

    fill_rows, substituted, same_lot = _fill(demands, lost, remaining, usable)

    kept_by, fill_by = defaultdict(list), defaultdict(list)
    for row in kept_rows:
        kept_by[row['需求单']].append(row)
    for row in fill_rows:
        fill_by[row['需求单']].append(row)

    allocations, plan_rows = [], []
    for demand in sorted(demands, key=demand_order):
        record = demand['record']
        for row in fill_by[record]:
            core = {field: row[field] for field in CORE_FIELDS}
            allocations.append(core)
            plan_rows.append({**core, '分配类型': row['分配类型'],
                              '原锁定批号': row['原锁定批号'],
                              '原锁定药品': row['原锁定药品'],
                              '来源段': row['分配类型']})
        for row in kept_by[record]:
            allocations.append(row)
            plan_rows.append({**row, '分配类型': '普通', **BLANK_SOURCE, '来源段': '普通'})

    allocated = defaultdict(int)
    for row in allocations:
        allocated[row['需求单']] += row['调拨数量']
    gap_rows = []
    for demand in sorted(demands, key=demand_order):
        record = demand['record']
        gap = demand['net_need'] - allocated[record]
        base = base_gap.get(record, 0)
        gap_rows.append({
            '需求单': record, '门店': demand['store'], '药品': demand['sku'],
            '净需求': demand['net_need'], '已分配': allocated[record], '缺口': gap,
            '基准缺口': base,
            '替代分配': substituted.get(record, 0),
            '原批补足': same_lot.get(record, 0),
            '无法替代缺口': max(0, gap - base),
            '受锁定影响': '是' if gap > base else '',
        })

    subs = [r for r in fill_rows if r['分配类型'] == '替代']
    refills = [r for r in fill_rows if r['分配类型'] == '原批补足']
    rows, balanced, summary = reconcile(
        usable, allocations, remaining, gap_rows, locks, residual, subs, refills)

    return {
        'version': version,
        'voids': sorted(set(voids or ())),
        'receipts': locks,
        'allocations': allocations,
        'normal_allocations': kept_rows,
        'plan_rows': plan_rows,
        'substitutions': subs,
        'refills': refills,
        'shortages': [{'需求单': r['需求单'], '门店': r['门店'], '净需求': r['净需求'],
                       '已分配': r['已分配'], '缺口': r['缺口']} for r in gap_rows],
        'gaps': gap_rows,
        'reconcile': rows,
        'balanced': balanced,
        'summary': summary,
        'remaining': remaining,
        'residual': residual,
        'usable': usable,
        'net_demands': demands,
        'metrics': [
            ['调拨', summary['已调拨'], '件'],
            ['分配行', len(allocations), '条'],
            ['缺口', summary['缺口'], '件'],
            ['专属锁定', summary['专属锁定'], '件'],
            ['替代分配', summary['替代分配'], '件'],
            ['无法替代缺口', summary['无法替代缺口'], '件'],
            ['方案版本', version, ''],
        ],
    }


def plan_steps(plan):
    """把方案展开为可幂等执行的步骤；步骤键在版本内稳定。"""
    version = plan['version']
    steps = []
    for row in plan['receipts']:
        if row['专属锁定']:
            steps.append({
                '步骤': 'lock|%s|%s|%s' % (row['回执'], row['药品'], row['原批号']),
                '类型': '回执锁定', '关联': row['回执'], '药品': row['药品'],
                '批号': row['原批号'], '数量': row['专属锁定'], '方案版本': version,
            })
    for row in plan['substitutions']:
        steps.append({
            '步骤': 'sub|%s|%s|%s|%s' % (row['需求单'], row['药品'], row['批号'],
                                          row['原锁定批号']),
            '类型': '替代分配', '关联': row['需求单'], '药品': row['药品'],
            '批号': row['批号'], '数量': row['调拨数量'], '方案版本': version,
        })
    for row in plan.get('refills', ()):
        steps.append({
            '步骤': 'refill|%s|%s|%s' % (row['需求单'], row['药品'], row['批号']),
            '类型': '原批补足', '关联': row['需求单'], '药品': row['药品'],
            '批号': row['批号'], '数量': row['调拨数量'], '方案版本': version,
        })
    for row in plan['normal_allocations']:
        steps.append({
            '步骤': 'send|%s|%s|%s' % (row['需求单'], row['药品'], row['批号']),
            '类型': '普通调拨', '关联': row['需求单'], '药品': row['药品'],
            '批号': row['批号'], '数量': row['调拨数量'], '方案版本': version,
        })
    for row in plan['gaps']:
        hard = row['无法替代缺口']
        if hard > 0:
            steps.append({
                '步骤': 'gap|%s' % row['需求单'],
                '类型': '缺口登记', '关联': row['需求单'], '药品': row['药品'],
                '批号': '', '数量': hard, '方案版本': version,
            })
    return steps
