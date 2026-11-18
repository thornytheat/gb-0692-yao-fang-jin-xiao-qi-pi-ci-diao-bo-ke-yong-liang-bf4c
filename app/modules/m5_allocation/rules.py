from app.plan import build_plan
def apply(ctx):
    plan=build_plan(ctx['usable'],ctx['net_demands'],ctx.get('receipts',()),
                    ctx.get('voids',()),ctx.get('plan_version',1))
    ctx.update(allocations=plan['allocations'],remaining=plan['remaining'],
               shortages=plan['shortages'],plan=plan,substitutions=plan['substitutions'],
               refills=plan['refills'],gaps=plan['gaps'],
               receipts_locked=plan['receipts'],plan_rows=plan['plan_rows'],
               residual=plan['residual'])
    return {'rows':plan['plan_rows'],'metrics':plan['metrics']}
