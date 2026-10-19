"""Recoverable vector outbox with SQLite validation at every read boundary.

Chroma is a replaceable index. Its documents and metadata are never accepted as
memory authority, and a delayed write cannot resurrect a deleted episode.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def episode_document_id(namespace: str, user_id: str, conv_id: str,
                        from_turn: int, to_turn: int, index_version: int) -> str:
    value = json.dumps([namespace,user_id,conv_id,from_turn,to_turn,index_version],ensure_ascii=False,separators=(',',':'))
    return 'episode-'+hashlib.sha256(value.encode()).hexdigest()


def episode_collection_name(namespace: str) -> str:
    return 'lumenmind-episodes-v1-'+hashlib.sha256(namespace.encode()).hexdigest()[:20]


def _episode_metadata(episode: Dict[str,Any]) -> Dict[str,Any]:
    return {key:episode[key] for key in ('namespace','user_id','conv_id','erase_epoch','index_version')}


def _doc_id(episode: Dict[str,Any]) -> str:
    return episode_document_id(*(episode[key] for key in ('namespace','user_id','conv_id','from_turn','to_turn','index_version')))


def validate_episode_candidates(db, namespace: str, user_id: str, conv_id: str,
                                candidate_ids: Iterable[str], recent_turn_seqs: Iterable[int] = (),
                                summary_covered_through: int = 0, max_chars: int = 3000, max_episodes: int = 3) -> List[Dict[str,Any]]:
    """Call inside the same read transaction as scope/facts/recent history."""
    if user_id.strip().casefold() in {'anonymous','demo'}:
        return []
    scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?",(namespace,user_id)).fetchone()
    if not scope or not scope['long_term_enabled'] or scope['migration_state']=='IMPORTING':
        return []
    requested = {value:index for index,value in enumerate(candidate_ids) if isinstance(value,str)}
    if not requested:
        return []
    rows = db.execute("""SELECT e.* FROM memory_episode e JOIN memory_session s ON e.namespace=s.namespace AND e.user_id=s.user_id AND e.conv_id=s.conv_id
                         WHERE e.namespace=? AND e.user_id=? AND e.invalid_at IS NULL AND e.erase_epoch=? AND s.deleted_at IS NULL
                         AND EXISTS (SELECT 1 FROM memory_turn t WHERE t.namespace=e.namespace AND t.user_id=e.user_id AND t.conv_id=e.conv_id AND t.turn_seq=e.from_turn AND t.source_seq>? AND t.deleted_at IS NULL)""",(namespace,user_id,scope['erase_epoch'],scope['context_floor_source_seq'])).fetchall()
    recent = set(recent_turn_seqs); candidates=[]
    for row in rows:
        episode = dict(row); document_id = _doc_id(episode)
        if document_id not in requested:
            continue
        if episode['conv_id']==conv_id and (episode['from_turn']<=summary_covered_through or any(episode['from_turn']<=turn<=episode['to_turn'] for turn in recent)):
            continue
        candidates.append((requested[document_id],episode))
    result=[]; used=0; ranges={}
    for _,episode in sorted(candidates,key=lambda item:item[0]):
        if len(result)>=max_episodes:
            break
        previous=ranges.setdefault(episode['conv_id'],[])
        if any(episode['from_turn']<=right and episode['to_turn']>=left for left,right in previous):
            continue
        length=len(episode['summary'])
        if used+length>max_chars:
            continue
        used+=length; previous.append((episode['from_turn'],episode['to_turn']))
        result.append({key:episode[key] for key in ('episode_id','conv_id','from_turn','to_turn','summary','source_generation','erase_epoch','index_version')})
    return result


class EpisodicIndexRepositoryMixin:
    def _index_episode(self, db, user_id: str, episode_id: str, expected_epoch: int, index_version: int):
        if not self.long_term_enabled or user_id.strip().casefold() in {'anonymous','demo'}:
            return None
        return db.execute("""SELECT e.* FROM memory_episode e JOIN memory_scope c ON e.namespace=c.namespace AND e.user_id=c.user_id
                             JOIN memory_session s ON e.namespace=s.namespace AND e.user_id=s.user_id AND e.conv_id=s.conv_id
                             WHERE e.namespace=? AND e.user_id=? AND e.episode_id=? AND e.index_version=? AND e.invalid_at IS NULL
                             AND e.erase_epoch=? AND c.erase_epoch=? AND c.long_term_enabled=1 AND c.migration_state!='IMPORTING' AND s.deleted_at IS NULL""",(self.namespace,user_id,episode_id,index_version,expected_epoch,expected_epoch)).fetchone()

    def claim_index_job(self, lease_seconds: int = 180) -> Optional[Dict[str,Any]]:
        with self._lock,self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("""SELECT * FROM memory_job WHERE namespace=? AND kind IN ('index_upsert','index_delete')
                               AND status IN ('READY','RETRY_WAIT','PROCESSING') AND julianday(next_run_at)<=julianday('now')
                               AND (status!='PROCESSING' OR julianday(lease_until)<=julianday('now'))
                               ORDER BY CASE WHEN kind='index_delete' THEN 0 ELSE 1 END,created_at LIMIT 20""",(self.namespace,)).fetchall()
            for row in rows:
                job=dict(row)
                try: envelope=json.loads(row['result'] or '{}')
                except (TypeError,ValueError): envelope={}
                if job['kind']=='index_upsert':
                    episode=self._index_episode(db,job['user_id'],envelope.get('episode_id',''),job['expected_epoch'],envelope.get('index_version',1))
                    if not episode:
                        db.execute("UPDATE memory_job SET status='CANCELLED',claim_token=NULL,lease_until=NULL,error_code='STALE_EPISODE' WHERE namespace=? AND job_id=?",(self.namespace,job['job_id']))
                        continue
                    job['index_document']=dict(episode)
                    job['document_id']=_doc_id(dict(episode))
                    job['metadata']=_episode_metadata(dict(episode))
                elif envelope.get('document_id'):
                    job['document_id']=envelope['document_id']
                elif type(job['expected_epoch']) is not int:
                    db.execute("UPDATE memory_job SET status='QUARANTINED',error_code='INVALID_DELETE_BOUNDARY' WHERE namespace=? AND job_id=?",(self.namespace,job['job_id']))
                    continue
                token=secrets.token_urlsafe(24);until=self._db_now(db,max(1,lease_seconds))
                db.execute("UPDATE memory_job SET status='PROCESSING',claim_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE namespace=? AND job_id=?",(token,until,self.namespace,job['job_id']))
                if job['kind']=='index_upsert':
                    envelope['document_id']=job['document_id']
                    db.execute("UPDATE memory_job SET result=? WHERE namespace=? AND job_id=?",(json.dumps(envelope),self.namespace,job['job_id']))
                    now=self._db_now(db)
                    db.execute("INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,status,expected_epoch,claim_token,lease_until,next_run_at,result,created_at) VALUES(?,?,?,?,?,'PROCESSING',?,?,?,?,?,?)",(self.namespace,job['user_id'],secrets.token_hex(16),'index_write_receipt',f"index-write:{job['job_id']}:{token}",job['expected_epoch'],token,until,now,json.dumps({'document_id':job['document_id']}),now))
                job.update(claim_token=token,lease_until=until,status='PROCESSING',attempt_count=job['attempt_count']+1)
                db.commit();return job
            db.commit();return None

    def validate_index_job(self, job: Dict[str,Any]) -> bool:
        """Recheck immediately before network I/O, separately from claiming."""
        with self._lock,self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT 1 FROM memory_job WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')",(self.namespace,job['job_id'],job['claim_token'])).fetchone()
            valid=bool(row)
            if valid and job['kind']=='index_upsert':
                episode=job['index_document']
                valid=bool(self._index_episode(db,job['user_id'],episode['episode_id'],job['expected_epoch'],episode['index_version']))
                if not valid:
                    db.execute("UPDATE memory_job SET status='CANCELLED',lease_until=NULL,error_code='STALE_EPISODE' WHERE namespace=? AND job_id=?",(self.namespace,job['job_id']))
            if not valid and job['kind']=='index_upsert':
                self._ack_index_attempt(db,job)
            db.commit();return valid

    def _compensate_index_write(self, db, job: Dict[str,Any]) -> None:
        document_id=job.get('document_id')
        if not document_id:
            return
        now=self._db_now(db)
        # The old document ID is immutable; never delete a newer scope epoch.
        db.execute("INSERT OR IGNORE INTO memory_job(namespace,user_id,job_id,kind,dedup_key,expected_epoch,next_run_at,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(self.namespace,job['user_id'],secrets.token_hex(16),'index_delete',f'compensate:{document_id}:{job["claim_token"]}',job.get('expected_epoch'),now,json.dumps({'document_id':document_id}),now))

    def _ack_index_attempt(self, db, job: Dict[str,Any]) -> None:
        db.execute("UPDATE memory_job SET status='WRITTEN',error_code=NULL,lease_until=NULL WHERE namespace=? AND dedup_key=? AND claim_token=?",(self.namespace,f"index-write:{job['job_id']}:{job['claim_token']}",job['claim_token']))
        db.execute("UPDATE memory_job SET error_code='STALE_WRITE_ACKNOWLEDGED',lease_until=NULL WHERE namespace=? AND job_id=? AND claim_token=? AND error_code='INDEX_WRITE_UNACKNOWLEDGED'",(self.namespace,job['job_id'],job['claim_token']))

    def finish_index_job(self, job: Dict[str,Any]) -> bool:
        with self._lock,self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM memory_job WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')",(self.namespace,job['job_id'],job['claim_token'])).fetchone()
            valid=True
            if job['kind']=='index_upsert':
                episode=job['index_document']
                valid=bool(self._index_episode(db,job['user_id'],episode['episode_id'],job['expected_epoch'],episode['index_version']))
                if not valid:
                    self._compensate_index_write(db,job)
                # Compensating work is durable before acknowledging the in-flight
                # tombstone, including callbacks from an older reclaimed token.
                self._ack_index_attempt(db,job)
            if row:
                db.execute("UPDATE memory_job SET status=?,lease_until=NULL,error_code=? WHERE namespace=? AND job_id=?",('WRITTEN' if valid else 'CANCELLED',None if valid else 'STALE_EPISODE',self.namespace,job['job_id']))
            db.commit();return bool(row and valid)

    def fail_index_job(self, job_id: str, claim_token: str, error_code: str = 'INDEX_UNAVAILABLE') -> bool:
        with self._lock,self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT attempt_count FROM memory_job WHERE namespace=? AND job_id=? AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')",(self.namespace,job_id,claim_token)).fetchone()
            if not row:
                db.commit();return False
            delay=min(300,2**min(row['attempt_count'],8))
            db.execute("UPDATE memory_job SET status='RETRY_WAIT',lease_until=NULL,error_code=?,next_run_at=? WHERE namespace=? AND job_id=?",(error_code[:80],self._db_now(db,delay),self.namespace,job_id))
            db.commit();return True

    def index_cleanup_progress(self, user_id: str) -> Dict[str,Any]:
        with self._connection() as db:
            db.execute('BEGIN')
            rows=db.execute("SELECT status,COUNT(*) AS n FROM memory_job WHERE namespace=? AND user_id=? AND kind='index_delete' GROUP BY status",(self.namespace,user_id)).fetchall()
            counts={row['status']:row['n'] for row in rows}
            unacknowledged=db.execute("SELECT COUNT(DISTINCT claim_token) FROM memory_job WHERE namespace=? AND user_id=? AND error_code='INDEX_WRITE_UNACKNOWLEDGED'",(self.namespace,user_id)).fetchone()[0]
            pending=sum(n for status,n in counts.items() if status!='WRITTEN')+unacknowledged
            return dict(index_cleanup='PENDING' if pending else 'COMPLETED' if counts else 'NOT_REQUESTED',pending=pending,completed=counts.get('WRITTEN',0),unacknowledged_writes=unacknowledged)


class EpisodicIndexService:
    def __init__(self, repository, collection: Any = None,
                 client_factory: Optional[Callable[[],Any]] = None):
        self.repository=repository
        self._collection=collection
        self._client_factory=client_factory
        self._task:Optional[asyncio.Task]=None
        self._closed=False
        self._collection_lock = threading.Lock()
        self._search_task: Optional[asyncio.Task] = None

    def _get_collection(self):
        with self._collection_lock:
            if self._collection is None:
                if self._client_factory is None:
                    raise RuntimeError('Episode vector index is not configured')
                self._collection = self._client_factory().get_or_create_collection(name=episode_collection_name(self.repository.namespace))
            return self._collection

    def _write(self, job: Dict[str,Any]) -> None:
        collection=self._get_collection()
        if job['kind']=='index_upsert':
            collection.upsert(ids=[job['document_id']],documents=[job['index_document']['summary']],metadatas=[job['metadata']])
        elif job.get('document_id'):
            collection.delete(ids=[job['document_id']])
        else:
            collection.delete(where={'$and':[{'namespace':self.repository.namespace},{'user_id':job['user_id']},{'erase_epoch':{'$lt':job['expected_epoch']}}]})

    async def _process(self, job: Dict[str,Any]) -> None:
        try:
            if not await asyncio.to_thread(self.repository.validate_index_job,job):
                return
            await asyncio.to_thread(self._write,job)
            await asyncio.to_thread(self.repository.finish_index_job,job)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Episode index job %s failed: %s',job['job_id'],type(error).__name__)
            await asyncio.to_thread(self.repository.fail_index_job,job['job_id'],job['claim_token'],type(error).__name__)

    @staticmethod
    def _done(task: asyncio.Task) -> None:
        if not task.cancelled():
            error=task.exception()
            if error is not None:
                logger.error('Episode index outcome could not be persisted: %s',type(error).__name__)

    async def tick(self) -> None:
        if self._closed or self._task and not self._task.done():
            return
        job=await asyncio.to_thread(self.repository.claim_index_job)
        if job is not None:
            self._task=asyncio.create_task(self._process(job),name=f"memory-index-{job['job_id']}")
            self._task.add_done_callback(self._done)

    def _search(self, user_id: str, query: str, limit: int) -> List[str]:
        with self.repository._connection() as db:
            scope=db.execute("SELECT long_term_enabled,migration_state FROM memory_scope WHERE namespace=? AND user_id=?",(self.repository.namespace,user_id)).fetchone()
            if not scope or not scope['long_term_enabled'] or scope['migration_state']=='IMPORTING':
                return []
        result=self._get_collection().query(query_texts=[query],n_results=max(1,min(20,limit)),where={'$and':[{'namespace':self.repository.namespace},{'user_id':user_id}]},include=[])
        ids=result.get('ids',[])
        return [value for value in ids[0] if isinstance(value,str)] if ids else []

    async def search_candidates(self, user_id: str, query: str, limit: int = 6) -> List[str]:
        if user_id.strip().casefold() in {'anonymous','demo'} or not self.repository.long_term_enabled or not query.strip():
            return []
        if self._search_task is not None and not self._search_task.done():
            return []
        try:
            self._search_task = asyncio.create_task(asyncio.to_thread(self._search, user_id, query, limit))
            self._search_task.add_done_callback(self._done)
            return await asyncio.wait_for(asyncio.shield(self._search_task), timeout=1.5)
        except Exception as error:
            logger.warning('Episode retrieval unavailable: %s',type(error).__name__)
            return []

    async def close(self) -> None:
        self._closed=True
        if self._task is not None:
            await asyncio.gather(self._task,return_exceptions=True)
