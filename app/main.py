from flask import Flask, request, jsonify, Response
import csv,io
from app.errors import DomainError
from app.reader import decode
from app.pipeline import calculate
from app.store import save, latest, history, review, get_run
from app.web import bp as web
from app import ledger, report

from app.modules.m1_receiving.handler import bp as bp1
from app.modules.m2_lots.handler import bp as bp2
from app.modules.m3_expiry.handler import bp as bp3
from app.modules.m4_demand.handler import bp as bp4
from app.modules.m5_allocation.handler import bp as bp5
from app.modules.m6_reconcile.handler import bp as bp6
from app.modules.m7_approval.handler import bp as bp7
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=5*1024*1024
app.json.ensure_ascii=False
@app.template_filter('display')
def display(value):
    import re
    from decimal import Decimal
    text=str(value)
    if re.fullmatch(r'-?[0-9]+\.[0-9]+',text):
        return format(Decimal(text),'.4f').rstrip('0').rstrip('.')
    return text
app.register_blueprint(web)
app.register_blueprint(bp1)
app.register_blueprint(bp2)
app.register_blueprint(bp3)
app.register_blueprint(bp4)
app.register_blueprint(bp5)
app.register_blueprint(bp6)
app.register_blueprint(bp7)
@app.before_request
def validate_request_object():
    if request.method!='POST': return None
    if request.files:
        if request.path!='/api/run' or 'file' not in request.files:
            raise DomainError('REQUEST','上传必须使用file字段',400)
        return None
    if not request.is_json:
        raise DomainError('REQUEST','请求须为JSON对象或CSV文件',400)
    if not isinstance(request.get_json(silent=True),dict):
        raise DomainError('REQUEST','请求内容必须为JSON对象',400)
@app.errorhandler(DomainError)
def domain_error(error): return jsonify({'code':error.code,'message':error.message}),error.status
@app.errorhandler(413)
def too_big(error): return jsonify({'code':'LIMIT','message':'文件超过5MB'}),413
@app.route('/health')
def health(): return {'status':'ok'}
@app.route('/api/run',methods=['POST'])
def run():
    if request.files:
        raw=request.files['file'].read();text=decode(raw)
    else:
        payload=request.get_json(silent=True) or {};text=payload.get('csv')
    return jsonify(save(calculate(text)))
@app.route('/api/runs/<ident>')
def run_detail(ident):
    run=get_run(ident)
    return jsonify(report.hydrate(run,ledger.current(ident)))
@app.route('/api/history')
def runs(): return jsonify(history())
@app.route('/api/review',methods=['POST'])
def decide():
    body=request.get_json(silent=True) or {}
    try: version=int(body.get('revision',0))
    except (ValueError,TypeError): raise DomainError('VERSION','版本必须为整数')
    return jsonify(review(body.get('id'),version,body.get('decision')))
@app.route('/api/evidence.csv')
def evidence():
    result=get_run(request.args['run_id']) if request.args.get('run_id') else (latest() or save(calculate()))
    plan=ledger.current(result['manifest']['run_id'])
    # 阶段 5–7 用当前方案快照覆盖，避免证据停留在建批时的旧方案行。
    result=report.hydrate(result,plan)
    out=io.StringIO(newline='')
    rows=[]
    for index,stage in result['stages'].items():
        rows.extend({'阶段':index,**r} for r in stage['rows'])
    rows.extend({'阶段':'隔离',**r} for r in result['quarantine'])
    for section,items in report.evidence_sections(plan):
        rows.extend({'阶段':section,**r} for r in items)
    fields=list(dict.fromkeys(['阶段','run_id','规则版本','方案版本']+[k for r in rows for k in r]))
    writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
    for row in rows:
        writer.writerow({'run_id':result['manifest']['run_id'],
                         '规则版本':result['manifest']['rule_version'],
                         '方案版本':plan['version'],**row})
    return Response('\ufeff'+out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=evidence.csv'})
@app.route('/api/plan')
def plan_current():
    ident=request.args.get('run_id')
    if not ident: raise DomainError('REQUEST','缺少run_id',400)
    return jsonify(ledger.current(ident))
@app.route('/api/plan/execute',methods=['POST'])
def plan_execute():
    body=request.get_json(silent=True) or {}
    ident=body.get('run_id')
    if not ident: raise DomainError('REQUEST','缺少run_id',400)
    try: version=int(body['version']) if body.get('version') is not None else None
    except (TypeError,ValueError): raise DomainError('VERSION','版本必须为整数')
    fail_after=body.get('fail_after')
    if fail_after is not None:
        try: fail_after=int(fail_after)
        except (TypeError,ValueError): raise DomainError('REQUEST','fail_after必须为整数',400)
        if fail_after<0: raise DomainError('REQUEST','fail_after不能为负',400)
    return jsonify(ledger.execute(ident,version,fail_after))
@app.route('/api/plan/ledger')
def plan_ledger():
    ident=request.args.get('run_id')
    if not ident: raise DomainError('REQUEST','缺少run_id',400)
    version=request.args.get('version')
    try: version=int(version) if version else None
    except (TypeError,ValueError): raise DomainError('VERSION','版本必须为整数')
    return jsonify(ledger.ledger(ident,version))
@app.route('/api/plan/void',methods=['POST'])
def plan_void():
    body=request.get_json(silent=True) or {}
    if not body.get('run_id') or not body.get('receipt'):
        raise DomainError('REQUEST','缺少run_id或receipt',400)
    result=ledger.void_receipt(body['run_id'],body['receipt'])
    result.pop('方案',None)
    return jsonify(result)
if __name__=='__main__': app.run(host='0.0.0.0',port=8000)
