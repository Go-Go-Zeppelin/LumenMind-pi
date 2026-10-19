"""Judge transaction tests use real SQLite with deterministic JSON decisions."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory.fact_pipeline import FactDecisionError
from memory.repository import CapacityError, ConflictError, MemoryRepository


@pytest.fixture
def repo(tmp_path):
    return MemoryRepository(str(tmp_path/'memory.sqlite3'))


def source(repo,text,request='r',conv='c'):
    receipt = repo.begin_turn('u',conv,request,text)
    repo.finish_turn('u',conv,request,receipt.lease_token,response='assistant is never source')
    return receipt


def claim(repo):
    repo.schedule_due_fact_jobs(force=True)
    return repo.claim_fact_job('u')


def payload(*decisions):
    return {'schema_version':1,'decisions':list(decisions)}


def add(text='email replies',seq=1,evidence='email',key='contact.channel',**kwargs):
    return dict(action='ADD',category='preference',fact_key=key,content=text,source_seqs=[seq],evidence=evidence,**kwargs)


def apply(repo,job,*decisions):
    return repo.apply_fact_result(job['job_id'],job['claim_token'],payload(*decisions))


def test_scheduler_threshold_fixed_range_and_single_scope_claim(repo):
    source(repo,'email one','a')
    assert repo.schedule_due_fact_jobs()==0
    source(repo,'email two','b')
    source(repo,'email three','c')
    assert repo.schedule_due_fact_jobs()==1
    source(repo,'new input beyond frozen range','d')
    other=MemoryRepository(repo.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda item:item.claim_fact_job('u'),[repo,other]))
    job=next(x for x in results if x)
    assert sum(x is not None for x in results)==1
    assert (job['from_seq'],job['to_seq'])==(1,3)
    assert [x['source_seq'] for x in job['messages']]==[1,2,3]
    assert not repo.schedule_due_fact_jobs(force=True)
    apply(repo,job,{'action':'NOOP'})
    assert repo.schedule_due_fact_jobs(force=True)==1
    assert repo.claim_fact_job('u')['messages'][0]['source_seq']==4


def test_source_batches_never_split_user_message(repo):
    source(repo,'x'*13000,'large')
    source(repo,'tail','small')
    job=claim(repo)
    assert len(job['messages'])==1 and len(job['messages'][0]['user_text'])==13000
    assert job['to_seq']==1


def test_add_supersede_duplicate_and_provenance(repo):
    source(repo,'email please')
    first=apply(repo,claim(repo),add())
    fact=repo.list_facts('u')['facts'][0]
    source(repo,'phone please','second')
    job=claim(repo)
    decision=add('phone replies',2,'phone');decision.update(action='SUPERSEDE',target_fact_id=fact['fact_id'])
    result=apply(repo,job,decision)
    assert result['revision']==first['revision']+1
    replacement=repo.list_facts('u')['facts'][0]
    with repo._connection() as db:
        old=db.execute('SELECT status,superseded_by FROM memory_fact WHERE fact_id=?',(fact['fact_id'],)).fetchone()
        assert tuple(old)==('INVALID',replacement['fact_id'])
        assert db.execute('SELECT evidence_excerpt FROM memory_fact_source WHERE fact_id=?',(replacement['fact_id'],)).fetchone()[0]=='phone'
    assert apply(repo,job,decision)==result  # Lost HTTP response replay.
    source(repo,'phone again','third')
    duplicate=add('phone replies',3,'phone');duplicate.update(action='SUPERSEDE',target_fact_id=replacement['fact_id'])
    assert apply(repo,claim(repo),duplicate)['status']=='NOOP'
    assert len(repo.list_facts('u')['facts'])==1


def test_noop_advances_cursor_but_failed_validation_does_not(repo):
    source(repo,'email please')
    job=claim(repo)
    for decision in (add(evidence='invented'),{'action':'NOOP','content':'extra'}):
        with pytest.raises(FactDecisionError):
            apply(repo,job,decision)
    with repo._connection() as db:
        assert db.execute('SELECT extract_cursor,fact_revision FROM memory_scope').fetchone()[:]==(0,0)
    result=apply(repo,job,{'action':'NOOP'})
    assert result['covered_source_seq']==1 and result['revision']==0


def test_entire_batch_rolls_back_for_invalid_target_or_capacity(repo):
    source(repo,'email phone')
    job=claim(repo)
    with pytest.raises(FactDecisionError):
        apply(repo,job,add(),dict(action='RETRACT',target_fact_id='other-user-fact',source_seqs=[1],evidence='phone'))
    assert not repo.list_facts('u')['facts']
    too_many=[add('a'*280+str(i),key=None) for i in range(12)]
    with pytest.raises(CapacityError):
        apply(repo,job,*too_many)
    assert not repo.list_facts('u')['facts']
    assert repo.lookup_fact_receipt('u','r')['status']=='PROCESSING'


def test_stable_key_final_source_wins_and_request_receipt_is_local(repo):
    source(repo,'email please','a')
    source(repo,'phone please','b')
    source(repo,'unrelated user input','c')
    job=claim(repo)
    result=apply(repo,job,add(),add('phone replies',2,'phone'))
    assert result['applied']==1
    assert repo.list_facts('u')['facts'][0]['content']=='phone replies'
    assert repo.lookup_fact_receipt('u','a')['status']=='NOOP'
    assert repo.lookup_fact_receipt('u','b')['status']=='WRITTEN'
    assert repo.lookup_fact_receipt('u','c')['status']=='NOOP'


def test_scope_revision_conflict_retries_same_range_without_failure_budget(repo):
    source(repo,'email please')
    job=claim(repo)
    repo.add_fact('u','stable_context','macOS',fact_key='device.os')
    with pytest.raises(ConflictError):
        apply(repo,job,add())
    retry=repo.fail_fact_job(job['job_id'],job['claim_token'],'CONFLICT',conflict=True)
    assert retry['failure_count']==0 and retry['status']=='RETRY_WAIT'
    with repo._connection() as db:
        db.execute("UPDATE memory_job SET next_run_at='2000-01-01'")
    new=repo.claim_fact_job('u')
    assert new['job_id']==job['job_id'] and new['to_seq']==1 and new['expected_revision']==1
    assert apply(repo,new,add())['revision']==2


def test_content_failures_quarantine_fixed_scope_batch(repo):
    source(repo,'email please')
    job=claim(repo)
    for failure in range(1,4):
        result=repo.fail_fact_job(job['job_id'],job['claim_token'],'INVALID_SCHEMA')
        assert result['failure_count']==failure
        if failure<3:
            with repo._connection() as db:db.execute("UPDATE memory_job SET next_run_at='2000-01-01'")
            job=repo.claim_fact_job('u')
    assert result['status']=='QUARANTINED'
    source(repo,'new message','after')
    assert not repo.schedule_due_fact_jobs(force=True)
    assert repo.claim_fact_job('u') is None
    repo.clear_facts('u','explicit-clear-bypasses-quarantine')
    source(repo,'after clear','new')
    assert repo.schedule_due_fact_jobs(force=True)==1


def test_expired_token_and_deletion_fence_late_model_result(repo):
    source(repo,'email please')
    job=claim(repo)
    with repo._connection() as db:db.execute("UPDATE memory_job SET lease_until='2000-01-01'")
    new=repo.claim_fact_job('u')
    with pytest.raises(ConflictError):apply(repo,job,add())
    repo.delete_conversation('u','c')
    with pytest.raises(ConflictError):apply(repo,new,add())
    assert not repo.list_facts('u')['facts']


def test_judge_clear_plus_add_is_atomic_and_barrier_is_replayable(repo):
    source(repo,'email please')
    apply(repo,claim(repo),add())
    source(repo,'clear old facts and use phone','second')
    job=claim(repo)
    decision=dict(action='CLEAR',source_seqs=[2],evidence='clear old facts')
    result=apply(repo,job,decision,add('phone replies',2,'phone'))
    assert repo.get_snapshot('u','c').erase_epoch==1
    assert [x['content'] for x in repo.list_facts('u')['facts']]==['phone replies']
    assert not repo.get_snapshot('u','c').recent_messages
    assert apply(repo,job,decision,add('phone replies',2,'phone'))==result


def test_policy_excluded_ranges_have_no_model_claim_and_real_noop_ledger(repo):
    repo.set_long_term_enabled('u',False)
    source(repo,'do not backfill this')
    repo.set_long_term_enabled('u',True)
    assert repo.schedule_due_fact_jobs(force=True)==1
    assert repo.claim_fact_job('u') is None
    receipt=repo.lookup_fact_receipt('u','r')
    assert receipt['status']=='NOOP' and receipt['reason']=='POLICY_EXCLUDED'
    with repo._connection() as db:
        assert db.execute('SELECT extract_cursor FROM memory_scope').fetchone()[0]==1


@pytest.mark.parametrize('bad',[
    {'schema_version':True,'decisions':[{'action':'NOOP'}]},
    {'schema_version':1,'decisions':[]},
    payload(dict(action='ADD',category='preference',fact_key=None,content='password: secretpass',source_seqs=[1],evidence='email')),
    payload(dict(action='RETRACT',target_fact_id={},source_seqs=[1],evidence='email')),
    payload(add(),dict(action='CLEAR',source_seqs=[1],evidence='email'),dict(action='RETRACT',target_fact_id='x',source_seqs=[1],evidence='email')),
])
def test_strict_schema_rejects_injections_and_sensitive_content(repo,bad):
    source(repo,'email please')
    job=claim(repo)
    with pytest.raises(FactDecisionError):repo.apply_fact_result(job['job_id'],job['claim_token'],bad)
    assert not repo.list_facts('u')['facts']


def test_clear_empty_collection_still_reports_context_barrier_mutation(repo):
    source(repo,'clear everything')
    result=apply(repo,claim(repo),dict(action='CLEAR',source_seqs=[1],evidence='clear everything'))
    assert result['status']=='WRITTEN' and result['mutated'] and result['applied']==0
    assert repo.get_snapshot('u','c').erase_epoch==1
    assert repo.lookup_fact_receipt('u','r')['status']=='WRITTEN'


def test_current_request_noop_retains_prior_batch_mutation_flag(repo):
    source(repo,'email please','prior')
    source(repo,'unrelated','current')
    apply(repo,claim(repo),add())
    result=repo.lookup_fact_receipt('u','current')
    assert result['status']=='NOOP' and result['mutated'] and result['applied']==0


def test_importing_scope_cannot_schedule_or_claim_fact_jobs(repo):
    source(repo,'email please')
    with repo._connection() as db:db.execute("UPDATE memory_scope SET migration_state='IMPORTING'")
    assert repo.schedule_due_fact_jobs(force=True)==0
    with repo._connection() as db:db.execute("UPDATE memory_scope SET migration_state='READY'")
    repo.schedule_due_fact_jobs(force=True)
    with repo._connection() as db:db.execute("UPDATE memory_scope SET migration_state='IMPORTING'")
    assert repo.claim_fact_job('u') is None
