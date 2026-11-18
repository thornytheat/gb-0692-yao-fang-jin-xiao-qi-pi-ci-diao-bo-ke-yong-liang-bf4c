const pharmacy = {pending: false, plan: JSON.parse(document.getElementById('plan').textContent)};
const message = document.getElementById('message');
const execmsg = document.getElementById('execmsg');

async function savePlan(endpoint, body) {
  if (pharmacy.pending) return;
  pharmacy.pending = true;
  if (message) message.textContent = '正在安排批号…';
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  try {
    let request = {method: 'POST', body: body};
    if (!(body instanceof FormData)) {
      request.body = JSON.stringify(body);
      request.headers = {'Content-Type': 'application/json'};
    }
    let response = await fetch(endpoint, request);
    let result = await response.json();
    if (response.status >= 400) throw Error(result.message);
    return result;
  } catch (e) {
    if (message) message.textContent = e.message || '提交失败，请重试';
    return null;
  } finally {
    pharmacy.pending = false;
    for (const b of document.querySelectorAll('button')) b.disabled = false;
  }
}

async function runPlan(endpoint, body, redirect = true) {
  const result = await savePlan(endpoint, body);
  if (!result) return null;
  if (redirect) {
    if (result.manifest) location.href = location.pathname + '?run_id=' + encodeURIComponent(result.manifest.run_id);
    else location.reload();
  }
  return result;
}

document.getElementById('demo').addEventListener('click', () => runPlan('/api/run', {}));
document.getElementById('commit').addEventListener('click', () => runPlan('/api/run', {csv: document.getElementById('source').value}));
document.getElementById('sendfile').addEventListener('click', () => {
  let f = document.getElementById('attachment').files[0];
  if (!f) { message.textContent = '先选择库存文件。'; return; }
  let data = new FormData();
  data.append('file', f);
  runPlan('/api/run', data);
});
document.querySelectorAll('[data-approve]').forEach(el => el.addEventListener('click', () => runPlan('/api/review', {...pharmacy.plan, decision: el.dataset.approve})));
const paper = document.getElementById('paper');
if (paper) paper.addEventListener('click', () => window.print());

const execute = document.getElementById('execute');
if (execute) execute.addEventListener('click', async () => {
  execmsg.textContent = '正在执行方案…';
  const result = await savePlan('/api/plan/execute', {run_id: pharmacy.plan.id, version: pharmacy.plan.version});
  if (!result) return;
  execmsg.textContent = result['状态'] + '：已完成 ' + result['已完成'] + ' / ' + result['步骤总数'] + ' 步';
  setTimeout(() => location.reload(), 600);
});

const ledgerButton = document.getElementById('ledger');
if (ledgerButton) ledgerButton.addEventListener('click', async () => {
  const response = await fetch('/api/plan/ledger?run_id=' + encodeURIComponent(pharmacy.plan.id) + '&version=' + pharmacy.plan.version);
  const result = await response.json();
  if (response.status >= 400) { execmsg.textContent = result.message; return; }
  const parts = Object.entries(result['占用']['按类型']).map(([k, v]) => k + ' ' + v).join(' · ');
  execmsg.textContent = '方案 v' + result['方案版本'] + ' 已占用：' + (parts || '无');
});

document.querySelectorAll('[data-void]').forEach(el => el.addEventListener('click', async () => {
  const receipt = el.dataset.void;
  if (!confirm('撤销回执 ' + receipt + '？将释放原锁定量并按先到期重算，切换到新方案版本。')) return;
  const result = await savePlan('/api/plan/void', {run_id: pharmacy.plan.id, receipt});
  if (!result) return;
  location.reload();
}));
