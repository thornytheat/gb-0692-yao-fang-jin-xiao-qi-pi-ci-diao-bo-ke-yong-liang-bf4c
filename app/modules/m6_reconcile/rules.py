from app.plan import reconcile
def apply(ctx):
    plan=ctx.get('plan',{})
    rows,balanced,summary=reconcile(
        ctx['usable'],ctx.get('allocations',()),ctx.get('remaining',{}),
        ctx.get('gaps',()),ctx.get('receipts_locked',()),ctx.get('residual',{}),
        ctx.get('substitutions',()),ctx.get('refills',()))
    ctx['balanced']=balanced
    ctx['reconcile']=rows
    if plan:
        plan['balanced']=balanced
        plan['reconcile']=rows
        plan['summary']=summary
    return {'rows':rows,'metrics':[['库存差额',summary['期初']-summary['专属锁定']-summary['已调拨']-summary['结余'],'件'],
                                    ['需求差额',summary['净需求']-summary['已调拨']-summary['缺口'],'件'],
                                    ['回执差额',summary['回执数量']-summary['专属锁定']-summary['回执缺量'],'件'],
                                    ['收发核验','相符' if balanced else '差异','']]}
