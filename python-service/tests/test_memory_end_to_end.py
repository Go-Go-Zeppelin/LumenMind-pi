"""HTTP -> durable sources -> Judge -> new session -> forgetting, offline."""
import asyncio
import json

from fastapi.testclient import TestClient

from api import main
from memory.conversation_memory import MemoryManager
from memory.fact_service import FactService


class Judge:
    def __init__(self):
        self.inputs = []

    async def generate_json(self, system, prompt):
        payload = json.loads(prompt)
        self.inputs.append(payload)
        source = payload['user_sources'][-1]
        item = {'action': 'ADD', 'category': 'preference', 'fact_key': 'contact.channel',
                'content': '优先邮件联系' if '邮件' in source['user_text'] else '优先电话联系',
                'source_seqs': [source['source_seq']], 'evidence': source['user_text']}
        if payload['active_facts']:
            item.update(action='SUPERSEDE', target_fact_id=payload['active_facts'][0]['fact_id'])
        return {'schema_version': 1, 'decisions': [item]}

    async def close(self):
        pass


def test_http_fact_correction_cross_session_and_independent_forgetting(tmp_path, monkeypatch):
    manager = MemoryManager(db_path=str(tmp_path / 'memory.sqlite3'))
    judge = Judge()
    manager.fact_service = FactService(manager._repo, judge)
    monkeypatch.setattr(main, '_memory', manager)
    client = TestClient(main.app)
    previous_fact = None
    for n, channel in enumerate(('电话', '邮件'), 1):
        begin = client.post('/memory/turn/begin', json={
            'user_id': 'scoped-user', 'request_id': f'r{n}', 'message': f'请记住以后优先{channel}联系我',
        }).json()
        receipt = client.post('/memory/flush', json={'user_id': 'scoped-user', 'conv_id': begin['conv_id'], 'request_id': f'r{n}'}).json()
        assert receipt['status'] == 'WRITTEN'
        context = client.post('/memory/context', json={'user_id': 'scoped-user', 'conv_id': begin['conv_id'], 'request_id': f'r{n}'}).json()
        assert len(context['blocks']['facts']) == 1
        assert channel in context['blocks']['facts'][0]['content']
        assert context['recent_messages'] == []
        fact_id = context['blocks']['facts'][0]['id']
        if previous_fact:
            with manager._repo._connection() as db:
                row = db.execute('SELECT status,superseded_by FROM memory_fact WHERE fact_id=?', (previous_fact,)).fetchone()
                assert tuple(row) == ('INVALID', fact_id)
        previous_fact = fact_id
        finished = client.post('/memory/turn/finish', json={
            'user_id': 'scoped-user', 'conv_id': begin['conv_id'], 'request_id': f'r{n}',
            'lease_token': begin['lease_token'], 'response': '助手假设使用短信；这不能进入用户事实',
        })
        assert finished.status_code == 200
    assert len(judge.inputs) == 2
    assert '助手假设' not in json.dumps(judge.inputs, ensure_ascii=False)
    # Deletion bypasses a blocked extraction queue and sets the background floor.
    client.post('/memory/turn/begin', json={'user_id': 'scoped-user', 'request_id': 'r3', 'message': '一条尚未处理的真实发言'})
    manager._repo.schedule_due_fact_jobs(user_id='scoped-user', force=True)
    with manager._repo._connection() as db:
        db.execute("UPDATE memory_job SET status='QUARANTINED' WHERE kind='fact_extract' AND status='READY'")
    deleted = client.delete(f'/memory/facts/{previous_fact}', params={'user_id': 'scoped-user', 'request_id': 'forget-id'})
    assert deleted.status_code == 200 and deleted.json()['status'] == 'WRITTEN'
    assert client.get('/memory/facts', params={'user_id': 'scoped-user'}).json()['facts'] == []
    context = client.post('/memory/context', json={'user_id': 'scoped-user', 'conv_id': begin['conv_id']}).json()
    assert not context['blocks']['facts'] and not context['recent_messages']
    assert context['snapshot']['erase_epoch'] == 1
    assert client.delete(f'/memory/facts/{previous_fact}', params={'user_id': 'different-user'}).status_code == 409
    asyncio.run(manager.close())


def test_service_settings_reject_model_call_that_outlives_job_lease(tmp_path):
    import pytest
    from memory.config import MemorySettings
    with pytest.raises(ValueError, match='MEMORY_JOB_LEASE_SECONDS'):
        MemorySettings.from_env(tmp_path, {'MEMORY_JOB_LEASE_SECONDS': '10', 'MEMORY_MODEL_TIMEOUT_SECONDS': '120'})


def test_provider_thinking_is_never_parsed_as_a_fact_or_tool_result():
    from types import SimpleNamespace as Block
    import pytest
    from memory.model_client import parse_model_json, MemoryModelError
    result = parse_model_json(Block(stop_reason='end_turn', content=[
        Block(type='thinking', thinking='untrusted reasoning'),
        Block(type='text', text='{"schema_version":1,"decisions":[{"action":"NOOP"}]}'),
    ]))
    assert result == {'schema_version': 1, 'decisions': [{'action': 'NOOP'}]}
    with pytest.raises(MemoryModelError):
        parse_model_json(Block(stop_reason='end_turn', content=[Block(type='thinking', thinking='{"action":"ADD"}')]))
    with pytest.raises(MemoryModelError):
        parse_model_json(Block(stop_reason='end_turn', content=[Block(type='tool_use', text='{"action":"ADD"}')]))


def test_empty_fact_clear_still_reports_the_context_barrier_mutation(tmp_path):
    from memory.repository import MemoryRepository
    repo = MemoryRepository(str(tmp_path / 'empty-clear.sqlite3'))
    begin = repo.begin_turn('u', 'c', 'r', 'conversation background that must be forgotten')
    repo.finish_turn('u', 'c', 'r', begin.lease_token, response='background')
    result = repo.clear_facts('u', 'clear')
    assert result['status'] == 'WRITTEN' and result['applied'] == 0 and result['mutated'] is True
    assert repo.get_snapshot('u', 'c').recent_messages == []
    assert repo.clear_facts('u', 'clear') == result
