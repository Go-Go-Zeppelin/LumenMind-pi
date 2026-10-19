"""Frozen scope batches and atomic Judge commits. Model I/O happens elsewhere."""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Dict, Optional

from .context_builder import render_fact_block
from .fact_pipeline import FactDecisionError, validate_decisions


class FactRepositoryMixin:
    def schedule_due_fact_jobs(self, min_messages: int = 3, max_wait_seconds: int = 60,
                               max_messages: int = 20, max_chars: int = 12000,
                               user_id: Optional[str] = None, force: bool = False) -> int:
        if not self.long_term_enabled:
            return 0
        scheduled = 0
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scopes = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND long_term_enabled=1 AND migration_state!='IMPORTING' AND (? IS NULL OR user_id=?)", (self.namespace,user_id,user_id)).fetchall()
            for scope in scopes:
                uid = scope['user_id']
                if uid.strip().casefold() in {'anonymous','demo'}:
                    continue
                if db.execute("SELECT 1 FROM memory_job WHERE namespace=? AND user_id=? AND kind='fact_extract' AND status IN ('READY','PROCESSING','RETRY_WAIT','QUARANTINED')", (self.namespace,uid)).fetchone():
                    continue
                rows = db.execute("SELECT * FROM memory_turn WHERE namespace=? AND user_id=? AND source_seq>? ORDER BY source_seq LIMIT ?", (self.namespace,uid,scope['extract_cursor'],max(1,max_messages))).fetchall()
                if not rows:
                    continue
                age = db.execute("SELECT (julianday('now')-julianday(?))*86400", (rows[0]['received_at'],)).fetchone()[0]
                if not force and len(rows) < min_messages and (age or 0) < max_wait_seconds:
                    continue
                chosen = []; chars = 0
                for row in rows:
                    length = len(row['user_text']) if row['memory_eligible'] and not row['deleted_at'] else 0
                    if chosen and chars + length > max_chars:
                        break
                    chosen.append(row); chars += length
                now = self._db_now(db)
                db.execute("""INSERT INTO memory_job(namespace,user_id,job_id,kind,dedup_key,from_seq,to_seq,expected_revision,expected_cursor,expected_epoch,next_run_at,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (self.namespace,uid,secrets.token_hex(16),'fact_extract',f"batch:{scope['erase_epoch']}:{chosen[0]['source_seq']}:{chosen[-1]['source_seq']}",chosen[0]['source_seq'],chosen[-1]['source_seq'],scope['fact_revision'],scope['extract_cursor'],scope['erase_epoch'],now,now))
                if not any(row['memory_eligible'] and not row['deleted_at'] for row in chosen):
                    receipt = dict(status='NOOP',revision=scope['fact_revision'],covered_source_seq=chosen[-1]['source_seq'],applied=0,mutated=False,reason='POLICY_EXCLUDED')
                    db.execute("UPDATE memory_job SET status='NOOP',result=?,error_code='POLICY_EXCLUDED' WHERE namespace=? AND user_id=? AND dedup_key=?", (json.dumps(receipt),self.namespace,uid,f"batch:{scope['erase_epoch']}:{chosen[0]['source_seq']}:{chosen[-1]['source_seq']}"))
                    db.execute("UPDATE memory_scope SET extract_cursor=?,updated_at=? WHERE namespace=? AND user_id=?", (chosen[-1]['source_seq'],now,self.namespace,uid))
                scheduled += 1
            db.commit()
        return scheduled

    def claim_fact_job(self, user_id: Optional[str] = None, lease_seconds: int = 180) -> Optional[Dict[str,Any]]:
        if not self.long_term_enabled:
            return None
        with self._lock, self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT j.* FROM memory_job j JOIN memory_scope s ON j.namespace=s.namespace AND j.user_id=s.user_id
                                WHERE j.namespace=? AND j.kind='fact_extract' AND (? IS NULL OR j.user_id=?) AND s.long_term_enabled=1 AND s.migration_state!='IMPORTING'
                                AND j.status IN ('READY','RETRY_WAIT','PROCESSING') AND julianday(j.next_run_at)<=julianday('now')
                                AND (j.status!='PROCESSING' OR julianday(j.lease_until)<=julianday('now')) ORDER BY j.created_at LIMIT 1""", (self.namespace,user_id,user_id)).fetchone()
            if not row:
                db.commit(); return None
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,row['user_id'])).fetchone()
            if row['expected_epoch'] != scope['erase_epoch'] or row['expected_cursor'] != scope['extract_cursor']:
                db.execute("UPDATE memory_job SET status='CANCELLED',error_code='STALE_BASELINE' WHERE namespace=? AND job_id=?", (self.namespace,row['job_id']))
                db.commit(); return None
            token = secrets.token_urlsafe(24); until = self._db_now(db,max(1,lease_seconds))
            db.execute("UPDATE memory_job SET status='PROCESSING',claim_token=?,lease_until=?,attempt_count=attempt_count+1,expected_revision=? WHERE namespace=? AND job_id=?", (token,until,scope['fact_revision'],self.namespace,row['job_id']))
            job = dict(row); job.update(claim_token=token,lease_until=until,status='PROCESSING',attempt_count=row['attempt_count']+1,expected_revision=scope['fact_revision'])
            job['messages'] = [dict(x) for x in db.execute("SELECT source_seq,conv_id,user_text FROM memory_turn WHERE namespace=? AND user_id=? AND source_seq BETWEEN ? AND ? AND memory_eligible=1 AND deleted_at IS NULL ORDER BY source_seq", (self.namespace,row['user_id'],row['from_seq'],row['to_seq']))]
            job['current_facts'] = [dict(x) for x in db.execute("SELECT fact_id,category,fact_key,content,normalized_hash,revision FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE'", (self.namespace,row['user_id']))]
            db.commit(); return job

    def _claimed_fact_job(self, db, job_id, claim_token):
        return db.execute("SELECT * FROM memory_job WHERE namespace=? AND job_id=? AND kind='fact_extract' AND claim_token=? AND status='PROCESSING' AND julianday(lease_until)>julianday('now')", (self.namespace,job_id,claim_token)).fetchone()

    def apply_fact_result(self, job_id: str, claim_token: str, payload: Any) -> Dict[str,Any]:
        from .repository import CapacityError, ConflictError
        with self._lock, self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT status,result,claim_token FROM memory_job WHERE namespace=? AND job_id=?", (self.namespace,job_id)).fetchone()
            if old and old['status'] in {'WRITTEN','NOOP'} and old['claim_token'] == claim_token:
                db.commit(); return json.loads(old['result'])
            job = self._claimed_fact_job(db,job_id,claim_token)
            if not job:
                raise ConflictError('事实任务领取已失效')
            uid = job['user_id']
            scope = db.execute("SELECT * FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,uid)).fetchone()
            if not self.long_term_enabled or not scope['long_term_enabled'] or any(scope[k] != job[e] for k,e in [('fact_revision','expected_revision'),('extract_cursor','expected_cursor'),('erase_epoch','expected_epoch')]):
                raise ConflictError('事实任务基线已变化')
            source = {x['source_seq']: x['user_text'] for x in db.execute("SELECT source_seq,user_text FROM memory_turn WHERE namespace=? AND user_id=? AND source_seq BETWEEN ? AND ? AND memory_eligible=1 AND deleted_at IS NULL", (self.namespace,uid,job['from_seq'],job['to_seq']))}
            facts = {x['fact_id']:dict(x) for x in db.execute("SELECT * FROM memory_fact WHERE namespace=? AND user_id=? AND status='ACTIVE'", (self.namespace,uid))}
            decisions = validate_decisions(payload,source,set(facts))
            # Fold sequential writes to one key into the latest source decision.
            keyed = {}; final = []
            for decision in decisions:
                key = decision.get('fact_key') if decision['action'] in {'ADD','SUPERSEDE'} else None
                if key:
                    old_decision = keyed.get(key)
                    if old_decision and max(old_decision['source_seqs']) == max(decision['source_seqs']):
                        raise FactDecisionError('同一来源包含稳定键冲突')
                    if not old_decision or max(decision['source_seqs']) > max(old_decision['source_seqs']):
                        keyed[key] = decision
                else:
                    final.append(decision)
            final.extend(keyed.values())
            has_clear = any(x['action']=='CLEAR' for x in final)
            preview = {} if has_clear else dict(facts)
            inserted = []; invalidated = set(facts) if has_clear else set(); source_mutations = set()
            if has_clear:
                source_mutations.update(next(item['source_seqs'] for item in final if item['action']=='CLEAR'))
            for item in final:
                action = item['action']
                if action in {'NOOP','CLEAR'}:
                    continue
                target = item.get('target_fact_id')
                if action == 'SUPERSEDE':
                    previous = preview.get(target)
                    normalized = ' '.join(item['content'].strip().split()).casefold()
                    if previous and previous['normalized_hash'] == hashlib.sha256(normalized.encode()).hexdigest() and previous['category'] == item['category'] and previous.get('fact_key') == item.get('fact_key'):
                        continue
                if action in {'SUPERSEDE','RETRACT'}:
                    preview.pop(target,None); invalidated.add(target); source_mutations.update(item['source_seqs'])
                if action == 'RETRACT':
                    continue
                content = item['content'].strip(); digest = hashlib.sha256(' '.join(content.split()).casefold().encode()).hexdigest()
                same = next((x for x in preview.values() if x['normalized_hash']==digest),None)
                if same:
                    continue
                key = item.get('fact_key')
                conflicting = next((x for x in preview.values() if key and x.get('fact_key')==key),None)
                if conflicting:
                    raise FactDecisionError('ADD 与已有稳定键冲突；必须使用 SUPERSEDE')
                fid = f'fact-{secrets.token_hex(10)}'
                fact = dict(fact_id=fid,category=item['category'],fact_key=key,content=content,normalized_hash=digest)
                preview[fid]=fact; inserted.append((fact,item)); source_mutations.update(item['source_seqs'])
            if any(len(fact["content"]) > getattr(self, "fact_max_chars", 300) for fact in preview.values()):
                raise CapacityError("事实超过单条字符上限")
            if len(preview)>getattr(self, "fact_max_count", 50):
                raise CapacityError('长期事实已达到容量上限')
            if len(render_fact_block(list(preview.values())))>getattr(self, "fact_block_max_chars", 3000):
                raise CapacityError('长期事实渲染超过字符预算')
            rev = scope['fact_revision'] + int(bool(inserted or invalidated))
            now = self._db_now(db)
            barrier = has_clear or any(x['action']=='RETRACT' for x in final)
            if barrier:
                self._erase_barrier(db,uid,'JUDGE_FORGET')
            epoch = db.execute("SELECT erase_epoch FROM memory_scope WHERE namespace=? AND user_id=?", (self.namespace,uid)).fetchone()[0]
            for fid in invalidated:
                db.execute("UPDATE memory_fact SET status='INVALID',invalid_at=?,revision=? WHERE namespace=? AND user_id=? AND fact_id=?", (now,rev,self.namespace,uid,fid))
            for fact,item in inserted:
                db.execute("INSERT INTO memory_fact(namespace,user_id,fact_id,category,fact_key,content,normalized_hash,origin,status,valid_from,revision,erase_epoch) VALUES(?,?,?,?,?,?,?,'BACKGROUND','ACTIVE',?,?,?)", (self.namespace,uid,fact['fact_id'],fact['category'],fact['fact_key'],fact['content'],fact['normalized_hash'],now,rev,epoch))
                if item['action']=='SUPERSEDE':
                    db.execute("UPDATE memory_fact SET superseded_by=? WHERE namespace=? AND user_id=? AND fact_id=?", (fact['fact_id'],self.namespace,uid,item['target_fact_id']))
                for seq in set(item['source_seqs']):
                    db.execute("INSERT INTO memory_fact_source VALUES(?,?,?,?,?)", (self.namespace,uid,fact['fact_id'],seq,item['evidence']))
            covered = max(job['to_seq'],scope['next_source_seq']-1 if barrier else job['to_seq'])
            status = 'WRITTEN' if inserted or invalidated or barrier else 'NOOP'
            result = dict(status=status,revision=rev,covered_source_seq=covered,applied=len(inserted)+len(invalidated),mutated=bool(inserted or invalidated or barrier),job_id=job_id,source_mutations=sorted(source_mutations))
            db.execute("UPDATE memory_scope SET fact_revision=?,extract_cursor=?,updated_at=? WHERE namespace=? AND user_id=?", (rev,covered,now,self.namespace,uid))
            db.execute("UPDATE memory_job SET status=?,result=?,lease_until=NULL,claim_token=? WHERE namespace=? AND job_id=?", (status,json.dumps(result),claim_token,self.namespace,job_id))
            db.commit(); return result

    def fail_fact_job(self, job_id: str, claim_token: str, error: str, conflict: bool = False) -> Dict[str,Any]:
        with self._lock, self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._claimed_fact_job(db,job_id,claim_token)
            if not job:
                db.commit(); return {'status':'STALE'}
            failures = job['failure_count'] + int(not conflict)
            status = 'QUARANTINED' if failures >= 3 else 'RETRY_WAIT'
            db.execute("UPDATE memory_job SET status=?,failure_count=?,error_code=?,next_run_at=?,lease_until=NULL WHERE namespace=? AND job_id=?", (status,failures,error[:120],self._db_now(db,min(300,2 ** max(1,failures))),self.namespace,job_id))
            db.commit(); return dict(status=status,job_id=job_id,attempt_count=job['attempt_count'],failure_count=failures)

    def lookup_fact_receipt(self, user_id: str, request_id: str) -> Dict[str,Any]:
        with self._connection() as db:
            db.execute('BEGIN')
            turn = db.execute("SELECT source_seq FROM memory_turn WHERE namespace=? AND user_id=? AND request_id=?", (self.namespace,user_id,request_id)).fetchone()
            if not turn:
                return {'status':'PENDING','queued':False}
            row = db.execute("SELECT * FROM memory_job WHERE namespace=? AND user_id=? AND kind='fact_extract' AND from_seq<=? AND to_seq>=? ORDER BY created_at DESC LIMIT 1", (self.namespace,user_id,turn['source_seq'],turn['source_seq'])).fetchone()
            if not row:
                return {'status':'PENDING','queued':False}
            if row['result']:
                result = json.loads(row['result'])
                if 'source_mutations' in result and turn['source_seq'] not in result['source_mutations']:
                    result.update(status='NOOP',applied=0)
                return result
            return dict(status=row['status'],job_id=row['job_id'],queued=True,error_code=row['error_code'])
