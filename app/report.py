"""把一版方案渲染成 M5/M6/M7 的行与指标。

页面（分拨）与证据导出共用本模块，确保两边对应同一方案版本。
"""


def _min_days(plan, demand):
    rows = [a for a in plan['allocations'] if a['需求单'] == demand]
    return min((r['到店余期_天'] for r in rows), default=0)


def allocation_rows(plan):
    return [{field: row[field] for field in
             ('需求单', '门店', '药品', '批号', '到期日', '调拨数量', '整包装',
              '到店余期_天', '分配类型', '原锁定批号')}
            for row in plan['plan_rows']]


def receipt_rows(plan):
    return [{k: row[k] for k in ('回执', '药品', '原批号', '原到期日', '回执数量',
                                 '整包装', '专属锁定', '回执缺量', '状态', '说明')}
            for row in plan['receipts']]


def substitution_rows(plan):
    return [{k: row[k] for k in ('需求单', '门店', '药品', '批号', '到期日',
                                 '调拨数量', '整包装', '原锁定批号', '原锁定药品')}
            for row in plan['substitutions']]


def gap_rows(plan):
    return [{k: row[k] for k in ('需求单', '门店', '药品', '净需求', '已分配', '缺口',
                                 '基准缺口', '替代分配', '原批补足', '无法替代缺口',
                                 '受锁定影响')}
            for row in plan['gaps']]


def reconcile_rows(plan):
    return list(plan['reconcile'])


def approval_rows(plan):
    rows = []
    for gap in plan['gaps']:
        rows.append({
            '需求单': gap['需求单'],
            '门店': gap['门店'],
            '药品': gap['药品'],
            '净需求': gap['净需求'],
            '已分配': gap['已分配'],
            '缺口': gap['缺口'],
            '替代分配': gap['替代分配'],
            '无法替代缺口': gap['无法替代缺口'],
            '最短余期_天': _min_days(plan, gap['需求单']),
            '复核': '可下发' if not gap['缺口'] else '缺口待补',
            '计划版本': plan['version'],
        })
    return rows


STAGE_NAMES = {5: '调拨分配', 6: '收发对账', 7: '方案复核'}


def stage_view(plan, stage):
    """返回某阶段的行、指标与面板需要的附加块。"""
    summary = plan['summary']
    if stage == 5:
        rows = allocation_rows(plan)
        metrics = [
            ['调拨', summary['已调拨'], '件'],
            ['分配行', len(rows), '条'],
            ['专属锁定', summary['专属锁定'], '件'],
            ['替代分配', summary['替代分配'], '件'],
            ['无法替代缺口', summary['无法替代缺口'], '件'],
            ['方案版本', plan['version'], ''],
        ]
    elif stage == 6:
        rows = reconcile_rows(plan)
        metrics = [
            ['库存差额', summary['期初'] - summary['专属锁定'] - summary['已调拨'] - summary['结余'], '件'],
            ['需求差额', summary['净需求'] - summary['已调拨'] - summary['缺口'], '件'],
            ['回执差额', summary['回执数量'] - summary['专属锁定'] - summary['回执缺量'], '件'],
            ['收发核验', '相符' if plan['balanced'] else '差异', ''],
        ]
    elif stage == 7:
        rows = approval_rows(plan)
        metrics = [
            ['可下发', sum(r['复核'] == '可下发' for r in rows), '单'],
            ['待补', sum(r['复核'] == '缺口待补' for r in rows), '单'],
            ['版本', plan['version'], ''],
        ]
    else:
        return None
    return {'name': STAGE_NAMES[stage], 'stage': stage,
            'rows': rows, 'metrics': metrics,
            'receipts': receipt_rows(plan), 'substitutions': substitution_rows(plan),
            'refills': refill_rows(plan), 'gaps': gap_rows(plan),
            'balanced': plan['balanced'], 'version': plan['version'],
            'summary': summary, 'voids': plan.get('voids', []),
            'status': plan.get('status', 'ready'), 'issues': {}, 'new_quarantine': 0}


def refill_rows(plan):
    return [{k: row[k] for k in ('需求单', '门店', '药品', '批号', '到期日',
                                 '调拨数量', '整包装', '原锁定批号')}
            for row in plan.get('refills', ())]


def summary_rows(plan):
    summary = plan['summary']
    return [{
        '方案版本': plan['version'],
        '方案状态': plan.get('status', 'ready'),
        '对账': '相符' if plan['balanced'] else '差异',
        '已撤销回执': ','.join(plan.get('voids', [])) or '无',
        **summary,
    }]


def evidence_sections(plan):
    """证据导出中方案相关的小节。"""
    return [
        ('方案摘要', summary_rows(plan)),
        ('方案回执', receipt_rows(plan)),
        ('方案替代', substitution_rows(plan)),
        ('方案原批补足', refill_rows(plan)),
        ('方案缺口', gap_rows(plan)),
        ('方案对账', reconcile_rows(plan)),
        ('方案步骤', [{k: s[k] for k in ('步骤', '类型', '关联', '药品', '批号', '数量')}
                      for s in plan.get('steps', ())]),
    ]


def hydrate(run, plan):
    """用当前方案快照覆盖阶段 5/6/7，使批次详情与页面、证据同版。"""
    for stage in (5, 6, 7):
        view = stage_view(plan, stage)
        base = run['stages'].get(str(stage), {})
        run['stages'][str(stage)] = {
            **base, 'rows': view['rows'], 'metrics': view['metrics'],
            'stage': stage, 'name': base.get('name', ''),
            'issues': base.get('issues', {}), 'new_quarantine': base.get('new_quarantine', 0),
            '方案版本': plan['version'],
        }
    run['方案版本'] = plan['version']
    return run
