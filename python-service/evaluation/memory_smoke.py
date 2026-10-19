"""Opt-in real-model memory smoke test; always uses an isolated temporary database.

Run: PYTHONPATH=python-service python -m evaluation.memory_smoke --real-model --report /tmp/memory-smoke.json
"""
import argparse
import asyncio,json,tempfile,pathlib,logging
from dotenv import dotenv_values
from memory.repository import MemoryRepository
from memory.fact_service import FactService
from memory.summary_service import SummaryService
from memory.model_client import MemoryModelClient
from memory.metrics import repository_status
logging.disable(logging.CRITICAL)
env=dotenv_values(pathlib.Path(__file__).resolve().parents[2] / '.env')
cfg=dict(api_key=env.get('ANTHROPIC_API_KEY',''),base_url=env.get('ANTHROPIC_BASE_URL') or None,model=env.get('ANTHROPIC_MODEL','claude-sonnet-4-6'))
async def run(report_path):
 with tempfile.TemporaryDirectory(prefix='lumen-real-memory-') as folder:
  repo=MemoryRepository(str(pathlib.Path(folder)/'isolated.sqlite3'),namespace='lumenmind-memory-acceptance')
  report={'model':cfg['model'],'environment':'isolated temporary SQLite; real model, no live user records','facts':[]}
  for n,text in enumerate(['请记住，以后优先通过电话联系我。','请记住，以后不要电话联系我，改为优先邮件联系。'],1):
   begin=repo.begin_turn('acceptance-user','preferences',f'real-{n}',text)
   service=FactService(repo,MemoryModelClient(**cfg))
   foreground=await service.flush('acceptance-user',begin.request_id)
   await service.close()
   result=repo.lookup_fact_receipt('acceptance-user',begin.request_id)
   facts=repo.list_facts('acceptance-user')['facts']
   report['facts'].append({'step':n,'foreground_status':foreground['status'],'settled_status':result.get('status'),'active':[{'key':f['fact_key'],'content':f['content']} for f in facts]})
   repo.finish_turn('acceptance-user','preferences',begin.request_id,begin.lease_token,response='Synthetic final response for storage check')
  for n in range(1,14):
   t=repo.begin_turn('summary-user','technical',f'turn-{n}',f'第{n}轮：我正在排查登录错误401，暂缓退款申请，请继续排查。')
   repo.finish_turn('summary-user','technical',t.request_id,t.lease_token,response='仅建议检查客户端版本，尚未执行退款或创建工单。')
  model=MemoryModelClient(**cfg,purpose='summary')
  from memory.summary_repository import validate_summary_result
  repo.schedule_due_summary_jobs()
  job=repo.claim_summary_job()
  service=SummaryService(repo,model)
  summary=await service._process(job)
  await service.close()
  snap=repo.get_snapshot('summary-user','technical')
  report['summary']={'status':summary.get('status'),'generation':snap.summary_generation,'recent_message_count':len(snap.recent_messages),'content':snap.summary}
  report['clear']=repo.clear_facts('acceptance-user','clear-real')
  report['facts_after_clear']=len(repo.list_facts('acceptance-user')['facts'])
  report['status']=repository_status(repo)
  assert all(step['settled_status'] in {'WRITTEN','NOOP'} for step in report['facts']), 'Fact jobs did not settle successfully'
  assert len(report['facts'][1]['active']) == 1 and '邮件' in report['facts'][1]['active'][0]['content'], 'Correction did not replace the first preference'
  assert report['summary']['status'] == 'WRITTEN' and report['summary']['recent_message_count'] == 6, 'Complete-turn summary did not commit'
  assert report['facts_after_clear'] == 0, 'Clear left active facts'
  pathlib.Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2))
  print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__ == "__main__":
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--real-model',action='store_true',help='Explicitly authorize API calls to the configured model')
 parser.add_argument('--report',required=True,help='Save synthetic evidence JSON to this path')
 args=parser.parse_args()
 if not args.real_model:parser.error('--real-model is required; this script makes billable model calls')
 asyncio.run(run(args.report))
