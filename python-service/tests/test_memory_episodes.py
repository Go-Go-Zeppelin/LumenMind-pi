"""Vector indexing is repeatable; all retrieval and deletion use SQLite truth."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory.episodic_index import (EpisodicIndexRepositoryMixin,EpisodicIndexService,
                                  episode_collection_name,episode_document_id,validate_episode_candidates)
from memory.repository import MemoryRepository


class IndexRepository(MemoryRepository):
    pass


@pytest.fixture
def repo(tmp_path):
    return IndexRepository(str(tmp_path/'memory.sqlite3'))


def seed(repo,episode='e1',conv='c',left=1,right=1,epoch=0,version=1,summary='episode summary'):
    # Real source and session rows, including complete user/assistant pairs.
    with repo._connection() as db:
        exists=db.execute('SELECT next_turn_seq FROM memory_session WHERE namespace=? AND user_id=? AND conv_id=?',(repo.namespace,'u',conv)).fetchone()
    current=exists[0] if exists else 1
    for number in range(current,right+1):
        request=f'{conv}-{number}'
        receipt=repo.begin_turn('u',conv,request,f'user original {number}')
        repo.finish_turn('u',conv,request,receipt.lease_token,response=f'assistant {number}')
    with repo._connection() as db:
        db.execute("INSERT INTO memory_episode VALUES(?,?,?,?,?,?,?,?,?,?,?)",(repo.namespace,'u',episode,conv,left,right,summary,1,None,epoch,version))
        db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,expected_epoch,next_run_at,created_at,result) VALUES(?,?,?,?,?,?,?,?,?)",(repo.namespace,'u',f'job-{episode}','index_upsert',f'index-upsert:{episode}:{version}',epoch,'2000-01-01','2000-01-01',json.dumps({'episode_id':episode,'index_version':version})))
    return episode_document_id(repo.namespace,'u',conv,left,right,version)


class FakeCollection:
    def __init__(self):
        self.docs={};self.calls=[];self.after_write=None
    def upsert(self,ids,documents,metadatas):
        self.calls.append(('upsert',ids))
        for index,key in enumerate(ids):self.docs[key]=(documents[index],metadatas[index])
        if self.after_write:self.after_write()
    def delete(self,ids=None,where=None):
        self.calls.append(('delete',ids,where))
        if ids:
            for key in ids:self.docs.pop(key,None)
        else:
            conditions=where['$and'];namespace=conditions[0]['namespace'];uid=conditions[1]['user_id'];epoch=conditions[2]['erase_epoch']['$lt']
            self.docs={key:value for key,value in self.docs.items() if not(value[1]['namespace']==namespace and value[1]['user_id']==uid and value[1]['erase_epoch']<epoch)}
    def query(self,**kwargs):
        self.calls.append(('query',kwargs))
        return {'ids':[list(self.docs)]}


def process(service):
    async def run():
        await service.tick()
        if service._task:await service._task
    asyncio.run(run())


def verified(repo,ids,conv='new',**kwargs):
    with repo._connection() as db:
        db.execute('BEGIN')
        return validate_episode_candidates(db,repo.namespace,'u',conv,ids,**kwargs)


def test_ids_hash_unambiguous_scope_tuple_and_collection_is_new():
    assert episode_document_id('a:b','c','d',1,2,1)!=episode_document_id('a','b:c','d',1,2,1)
    assert episode_document_id('n','u','c',1,2,1)!=episode_document_id('n','u','c',1,2,2)
    assert episode_collection_name('n').startswith('lumenmind-episodes-v1-')


def test_outbox_upsert_is_idempotent_and_authoritative(repo):
    doc=seed(repo);collection=FakeCollection();service=EpisodicIndexService(repo,collection)
    process(service)
    assert list(collection.docs)==[doc]
    assert collection.docs[doc][1]==dict(namespace='lumenmind',user_id='u',conv_id='c',erase_epoch=0,index_version=1)
    assert verified(repo,[doc])[0]['summary']=='episode summary'
    # Simulate vector write succeeded but acknowledgment was lost.
    with repo._connection() as db:db.execute("UPDATE memory_job SET status='READY',lease_until=NULL WHERE kind='index_upsert'")
    process(service)
    assert len(collection.docs)==1


def test_two_processes_claim_one_index_lease(repo):
    seed(repo);other=IndexRepository(repo.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda item:item.claim_index_job(),[repo,other]))
    assert sum(job is not None for job in jobs)==1


def test_delete_filter_preserves_post_barrier_epoch_and_other_user(repo):
    doc=seed(repo);collection=FakeCollection();service=EpisodicIndexService(repo,collection);process(service)
    repo.clear_facts('u','clear')
    collection.docs['new']=('new',dict(namespace=repo.namespace,user_id='u',erase_epoch=1))
    collection.docs['other']=('other',dict(namespace=repo.namespace,user_id='v',erase_epoch=0))
    assert repo.index_cleanup_progress('u')['index_cleanup']=='PENDING'
    process(service)
    assert doc not in collection.docs and set(collection.docs)=={'new','other'}
    assert repo.index_cleanup_progress('u')['index_cleanup']=='COMPLETED'


def test_late_upsert_after_delete_is_hidden_and_compensated(repo):
    doc=seed(repo);collection=FakeCollection();service=EpisodicIndexService(repo,collection)
    collection.after_write=lambda:repo.clear_facts('u','during-write')
    process(service)
    assert doc in collection.docs  # A real network race can leave stale vector data.
    assert not verified(repo,[doc])  # It cannot be read as a memory.
    collection.after_write=None
    process(service);process(service)  # era cleanup and exact-ID compensation.
    assert doc not in collection.docs
    assert repo.index_cleanup_progress('u')['index_cleanup']=='COMPLETED'


def test_claim_rechecks_deleted_and_versioned_episode(repo):
    seed(repo)
    with repo._connection() as db:db.execute('UPDATE memory_episode SET index_version=2')
    assert repo.claim_index_job() is None
    with repo._connection() as db:
        assert db.execute("SELECT status FROM memory_job WHERE kind='index_upsert'").fetchone()[0]=='CANCELLED'


def test_vector_candidate_scope_version_and_overlap_are_rechecked(repo):
    first=seed(repo,'e1','c',1,2)
    overlapping=seed(repo,'e2','c',2,3)
    other=seed(repo,'e3','other',1,1)
    assert [x['episode_id'] for x in verified(repo,[first,overlapping,other])]==['e1','e3']
    assert [x['episode_id'] for x in verified(repo,[first,overlapping,other],conv='c',recent_turn_seqs=[3])]==['e1','e3']
    assert [x['episode_id'] for x in verified(repo,[first,other],conv='c',summary_covered_through=2)]==['e3']
    with repo._connection() as db:db.execute("UPDATE memory_episode SET index_version=2 WHERE episode_id='e1'")
    assert not verified(repo,[first,'fabricated-other-user-document'])


def test_candidate_budget_keeps_complete_episodes(repo):
    doc=seed(repo,summary='x'*3001)
    assert not verified(repo,[doc],max_chars=3000)


def test_chroma_failure_retains_durable_retry_and_backoff(repo):
    seed(repo)
    class Broken(FakeCollection):
        def upsert(self,**kwargs):raise ConnectionError('secret raw message must never be persisted')
    process(EpisodicIndexService(repo,Broken()))
    with repo._connection() as db:
        row=db.execute("SELECT status,error_code FROM memory_job WHERE kind='index_upsert'").fetchone()
        assert tuple(row)==('RETRY_WAIT','ConnectionError')
    assert repo.claim_index_job() is None


def test_search_queries_ids_only_and_disabled_scope_does_not_contact_vectors(repo):
    doc=seed(repo);collection=FakeCollection();service=EpisodicIndexService(repo,collection);process(service)
    assert asyncio.run(service.search_candidates('u','support issue'))==[doc]
    assert collection.calls[-1][1]['include']==[]
    assert collection.calls[-1][1]['where']['$and'][1]=={'user_id':'u'}
    count=len(collection.calls);repo.set_long_term_enabled('u',False)
    assert asyncio.run(service.search_candidates('u','support issue'))==[]
    assert len(collection.calls)==count
    assert asyncio.run(service.search_candidates('anonymous','issue'))==[]


def test_delete_all_cleanup_stays_pending_until_real_vector_success(repo):
    doc=seed(repo);collection=FakeCollection();service=EpisodicIndexService(repo,collection);process(service)
    repo.delete_all('u')
    assert repo.index_cleanup_progress('u')['index_cleanup']=='PENDING'
    assert not verified(repo,[doc])
    process(service)
    assert not collection.docs
    assert repo.index_cleanup_progress('u')['index_cleanup']=='COMPLETED'


def test_delete_between_claim_and_network_precheck_skips_chroma(repo):
    seed(repo)
    job=repo.claim_index_job()
    repo.delete_conversation('u','c')
    collection=FakeCollection()
    asyncio.run(EpisodicIndexService(repo,collection)._process(job))
    assert not collection.calls


def test_physical_cleanup_waits_for_old_write_then_compensation(repo):
    doc=seed(repo)
    old=repo.claim_index_job()
    repo.delete_all('u')
    collection=FakeCollection();service=EpisodicIndexService(repo,collection)
    process(service)  # Epoch deletion completes before the old call returns.
    progress=repo.index_cleanup_progress('u')
    assert progress['index_cleanup']=='PENDING' and progress['unacknowledged_writes']==1
    with repo._connection() as db:
        tombstones=db.execute("SELECT result,claim_token,lease_until FROM memory_job WHERE error_code='INDEX_WRITE_UNACKNOWLEDGED'").fetchall()
        assert tombstones and all(json.loads(row['result'])=={'document_id':doc} for row in tombstones)
        assert all(row['claim_token']==old['claim_token'] and row['lease_until'] for row in tombstones)
    service._write(old)  # The remote request eventually succeeds.
    repo.finish_index_job(old)
    assert doc in collection.docs
    assert repo.index_cleanup_progress('u')['index_cleanup']=='PENDING'
    process(service)
    assert doc not in collection.docs
    assert repo.index_cleanup_progress('u')['index_cleanup']=='COMPLETED'


def test_reclaimed_old_token_cannot_ack_newer_inflight_write(repo):
    doc=seed(repo)
    old=repo.claim_index_job()
    with repo._connection() as db:
        db.execute("UPDATE memory_job SET lease_until='2000-01-01' WHERE job_id=?",(old['job_id'],))
    new=repo.claim_index_job()
    assert new['claim_token']!=old['claim_token']
    repo.delete_all('u')
    collection=FakeCollection();service=EpisodicIndexService(repo,collection)
    process(service)
    assert repo.index_cleanup_progress('u')['unacknowledged_writes']==2
    service._write(old);repo.finish_index_job(old)
    process(service)
    progress=repo.index_cleanup_progress('u')
    assert progress['index_cleanup']=='PENDING' and progress['unacknowledged_writes']==1
    service._write(new);repo.finish_index_job(new)
    process(service)
    assert not collection.docs
    assert repo.index_cleanup_progress('u')['index_cleanup']=='COMPLETED'


def test_dead_old_process_keeps_body_free_pending_tombstone(repo):
    seed(repo)
    old=repo.claim_index_job()
    repo.clear_facts('u','forget')
    with repo._connection() as db:
        db.execute("UPDATE memory_job SET lease_until='2000-01-01' WHERE error_code='INDEX_WRITE_UNACKNOWLEDGED'")
    process(EpisodicIndexService(repo,FakeCollection()))
    # Lease expiry is insufficient proof that a remote write stopped.
    assert repo.index_cleanup_progress('u')['index_cleanup']=='PENDING'
    assert repo.index_cleanup_progress('u')['unacknowledged_writes']==1
