# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

## 方案

`GET /api/plan?run_id=`：当前方案版本、回执锁定、替代分配、原批补足、缺口、对账与步骤。页面 `/work/5`、`/work/6` 与 `/api/evidence.csv?run_id=` 读取同一份方案快照。

`POST /api/plan/execute`：JSON 接收 run_id、可选 version 与 fail_after。按步骤幂等执行，重复提交只补做未完成步骤，不追加占用；fail_after 用于演练中断，之后再次提交从已完成步骤继续。方案版本已撤销返回409。

`GET /api/plan/ledger?run_id=&version=`：步骤明细与按批号/需求/类型的占用合计。

`POST /api/plan/void`：JSON 接收 run_id 与 receipt。释放该回执原锁定量与旧版已执行占用，按先到期重算并切换至新方案版本；重复撤销同一回执幂等（reused=true），未知回执404。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
