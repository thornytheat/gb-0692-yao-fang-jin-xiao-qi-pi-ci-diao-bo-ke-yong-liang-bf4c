from app.domain import whole,day
from app.errors import DomainError
from app.reasons import isolate
KINDS=('stock','demand','receipt')
def apply(ctx):
    stocks=[];demands=[];receipts=[];seen={};duplicates=0
    for r in ctx['rows']:
        try:
            if r['kind'] not in KINDS: raise DomainError('KIND','记录类别无效')
            qty=whole(r['qty']);frozen=whole(r['frozen']);pack=whole(r['pack']);days=whole(r['min_days'])
            if (r['kind']=='stock' and frozen>qty) or pack<1: raise DomainError('QUANTITY','冻结量或包装数量无效')
            if not r['sku'] or not r['record']: raise DomainError('MISSING','业务编号为空')
            if r['version']!='1': raise DomainError('VERSION','来源版本不支持')
            day(r['arrival'])
            if r['kind'] in ('stock','receipt'):
                if not r['lot']: raise DomainError('MISSING','批号为空')
            if r['kind']=='stock':
                day(r['expiry'])
            if r['kind']=='receipt' and r['expiry']: day(r['expiry'])
            key=(r['kind'],r['record']);fingerprint=tuple((k,v) for k,v in r.items() if k!='行号')
            if key in seen:
                if seen[key]==fingerprint: duplicates+=1
                else: isolate(ctx,r,'CONFLICT','来源单号内容冲突')
                continue
            seen[key]=fingerprint
            item={**r,'qty_n':qty,'frozen_n':frozen,'pack_n':pack,'min_days_n':days}
            {'stock':stocks,'demand':demands,'receipt':receipts}[r['kind']].append(item)
        except DomainError as e: isolate(ctx,r,e.code,e.message)
    # Recall is dominant even when a normal copy appeared earlier.
    recalls={(r['sku'],r['lot']) for r in ctx['rows'] if r['kind']=='stock' and r['status']=='recall'}
    unique={};valid=[]
    for s in stocks:
        if (s['sku'],s['lot']) in recalls: isolate(ctx,s,'RECALL','召回批禁止调拨');continue
        if (s['sku'],s['lot']) in unique: isolate(ctx,s,'LOT_DUP','批号重复来源');continue
        s['stock_key']=s['sku']+':'+s['lot'];unique[(s['sku'],s['lot'])]=s;valid.append(s)
    for s in receipts:
        s['stock_key']=s['sku']+':'+s['lot']
    ctx.update(stocks=valid,demands=demands,receipts=receipts,duplicates=duplicates)
    return {'rows':valid+demands+receipts,'metrics':[['有效库存',len(valid),'批'],['门店需求',len(demands),'单'],['退货回执',len(receipts),'单'],['隔离',len(ctx['quarantine']),'条']]}
