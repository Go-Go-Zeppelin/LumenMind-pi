"""Synthetic offline imports; tests never read environment files or live stores."""
import json
import sqlite3
from copy import deepcopy

import pytest

from memory.migrate import (LegacyImporter, MigrationError, apply_snapshot,
    backup_database, main, plan_report, plan_snapshot, requeue_job)
from memory.repository import ConflictError, MemoryRepository


def message(role, content, at):
    return {"role": role, "content": content, "timestamp": f"2026-01-01T00:{at:02d}:00+00:00"}


def snapshot():
    return {"schema_version": 1, "scopes": [{"user_id": "alice", "conversations": [
        {"conv_id": "old-a", "messages": [message("assistant", "orphan assistant", 0),
            message("user", "first user", 1), message("assistant", "first reply", 2),
            message("user", "incomplete user", 4)], "summary": "legacy summary, audit only"},
        {"conv_id": "old-b", "messages": [message("user", "second conversation", 3),
            message("assistant", "second reply", 5)]}],
        "profile": {"preferences": ["邮件联系", "邮件联系", 7], "entities": {"产品": ["键盘"]}},
        "profiles": [{"conv_id": "old-a", "timestamp": "2026-01-02T00:00:00+00:00", "profile": {"preferences": ["should not override user profile"]}}]}]}


@pytest.fixture
def repo(tmp_path):
    return MemoryRepository(str(tmp_path / "memory.sqlite3"))


def test_dry_run_reports_only_counts_and_never_creates_destination(tmp_path, capsys):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(snapshot()), encoding="utf-8")
    database = tmp_path / "never-created.sqlite3"
    assert main(["--database", str(database), "--namespace", "server-ns", "--snapshot", str(export)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run" and result["namespace"] == "server-ns"
    assert result["scopes"][0]["turns"] == 3
    assert not database.exists()
    assert "first user" not in str(result) and "邮件联系" not in str(result)


def test_apply_orders_cross_conversation_sources_pairs_only_real_user_and_is_idempotent(repo):
    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    importer = LegacyImporter(repo)
    first = importer.import_scope(plan)
    replay = importer.import_scope(plan)
    assert first["status"] == "LEGACY_IMPORTED" and first["replayed"] is False
    assert replay["replayed"] is True and replay["fingerprint"] == first["fingerprint"]
    with sqlite3.connect(repo.path) as db:
        turns = db.execute("SELECT user_text,status,memory_eligible FROM memory_turn ORDER BY source_seq").fetchall()
        assert turns == [("first user", "COMPLETED", 0), ("second conversation", "COMPLETED", 0), ("incomplete user", "INTERRUPTED", 0)]
        assert db.execute("SELECT source_seq FROM memory_turn ORDER BY source_seq").fetchall() == [(1,), (2,), (3,)]
        assert db.execute("SELECT next_source_seq,extract_cursor,migration_state FROM memory_scope").fetchone() == (4, 3, "LEGACY_IMPORTED")
        assert db.execute("SELECT COUNT(*) FROM memory_episode").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_turn WHERE user_text='orphan assistant'").fetchone()[0] == 0
    assert repo.begin_turn("alice", "new-c", "fresh", "fresh user").source_seq == 4
    # An exact import replay after new traffic never overwrites it.
    assert importer.import_scope(plan)["replayed"] is True


def test_legacy_profile_is_candidate_only_and_user_level_wins(repo):
    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    LegacyImporter(repo).import_scope(plan)
    with sqlite3.connect(repo.path) as db:
        facts = db.execute("SELECT content,origin,status,provenance_state FROM memory_fact ORDER BY content").fetchall()
        assert len(facts) == 3
        assert all(item[1:] == ("LEGACY_IMPORT", "CANDIDATE", "LEGACY_UNVERIFIED") for item in facts)
        assert "should not override user profile" not in str(facts)
        assert db.execute("SELECT COUNT(*) FROM memory_fact_source").fetchone()[0] == 0
    assert repo.list_facts("alice")["facts"] == []
    context = repo.get_snapshot("alice", "old-a")
    assert context.summary in ({}, "") and context.facts == []
    assert context.summary_generation == 0
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT content,prompt_version,covered_through_turn FROM memory_summary").fetchone() == ("legacy summary, audit only", "LEGACY_IMPORT", 0)


def test_latest_legacy_profile_selected_only_when_user_level_absent():
    value = snapshot(); scope = value["scopes"][0]; scope.pop("profile")
    scope["profiles"].append({"conv_id": "old-b", "timestamp": "2026-01-03T00:00:00Z", "profile": 'Some legacy prose {"preferences":["latest",9],"entities":{}}'})
    plan = plan_snapshot(value, "server")
    assert [item["content"] for item in plan["scopes"][0]["candidate_facts"]] == ["latest", "9"]
    assert plan_report(plan)["scopes"][0]["profile_source"] == "LATEST_CONVERSATION"
    # An explicitly empty user profile is still authoritative legacy selection.
    scope["profile"] = {"preferences": [], "entities": {}}
    assert plan_snapshot(value, "server")["scopes"][0]["candidate_facts"] == []


def test_timestamp_ties_preserve_original_list_position_and_never_fake_orphan_user():
    value = {"schema_version": 1, "scopes": [{"user_id": "u", "conversations": [{"conv_id": "c", "messages": [
        message("user", "one", 0), message("user", "two", 0), message("assistant", "two reply", 0), message("assistant", "orphan", 0)]}]}]}
    plan = plan_snapshot(value, "server")["scopes"][0]
    assert [(turn["user_text"], turn["status"]) for turn in plan["turns"]] == [("one", "INTERRUPTED"), ("two", "COMPLETED")]
    assert plan["skipped_non_user_messages"] == 1
    assert len({turn["request_id"] for turn in plan["turns"]}) == 2


def test_live_traffic_and_different_fingerprint_import_are_rejected(repo):
    live = repo.begin_turn("alice", "live", "live", "new traffic")
    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    with pytest.raises(ConflictError):
        LegacyImporter(repo).import_scope(plan)
    repo.finish_turn("alice", "live", "live", live.lease_token)
    value = snapshot(); value["scopes"][0]["user_id"] = "bob"
    bob = plan_snapshot(value, repo.namespace)["scopes"][0]
    LegacyImporter(repo).import_scope(bob)
    changed = deepcopy(value); changed["scopes"][0]["conversations"][0]["messages"][1]["content"] = "changed export"
    with pytest.raises(ConflictError):
        LegacyImporter(repo).import_scope(plan_snapshot(changed, repo.namespace)["scopes"][0])


def test_import_failure_rolls_back_all_bodies_leaves_lock_and_exact_retry_can_resume(repo, monkeypatch):
    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    importer = LegacyImporter(repo)
    original = importer._write_scope

    def fail_after_write(db, scope):
        original(db, scope)
        raise RuntimeError("simulated disk failure before commit")

    monkeypatch.setattr(importer, "_write_scope", fail_after_write)
    with pytest.raises(RuntimeError):
        importer.import_scope(plan)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT migration_state,next_source_seq FROM memory_scope").fetchone() == ("IMPORTING", 1)
        for table in ("memory_turn", "memory_fact", "memory_summary", "memory_session"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(ConflictError):
        repo.begin_turn("alice", "live", "r1", "blocked while importing")
    monkeypatch.setattr(importer, "_write_scope", original)
    assert importer.import_scope(plan)["status"] == "LEGACY_IMPORTED"


def test_deleted_and_forgotten_scope_cannot_be_repopulated_even_from_exact_old_export(repo):
    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    importer = LegacyImporter(repo)
    importer.import_scope(plan)
    repo.delete_all("alice")
    with pytest.raises(ConflictError):
        importer.import_scope(plan)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_fact").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_turn WHERE user_text!=''").fetchone()[0] == 0


def test_backup_is_sqlite_consistent_and_precedes_new_import(repo, tmp_path):
    with sqlite3.connect(repo.path) as db:
        db.execute("CREATE TABLE operator_marker(value TEXT)")
        db.execute("INSERT INTO operator_marker VALUES('pre-import')")
    result = apply_snapshot(snapshot(), repo.path, repo.namespace, tmp_path / "backups")
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT value FROM operator_marker").fetchone()[0] == "pre-import"
        assert backup.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 0
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 3


def test_backup_failure_prevents_any_import(tmp_path, monkeypatch):
    path = tmp_path / "not-created.sqlite3"
    import memory.migrate as migrate

    def fail_backup(*args):
        raise OSError("backup unavailable")

    monkeypatch.setattr(migrate, "backup_database", fail_backup)
    with pytest.raises(OSError):
        migrate.apply_snapshot(snapshot(), path, "server", tmp_path / "backups")
    assert not path.exists()


def test_requeue_preserves_fixed_range_and_versions_and_audits_operator_action(repo):
    start = repo.begin_turn("alice", "c", "r1", "hello")
    repo.finish_turn("alice", "c", "r1", start.lease_token)
    repo.schedule_due_fact_jobs(user_id="alice", force=True)
    job = repo.claim_fact_job()
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE memory_job SET status='QUARANTINED',failure_count=3 WHERE job_id=?", (job["job_id"],))
    result = requeue_job(repo, "alice", job["job_id"])
    assert result["status"] == "REQUEUED"
    with sqlite3.connect(repo.path) as db:
        row = db.execute("SELECT status,failure_count,from_seq,to_seq,expected_cursor,expected_epoch,expected_revision FROM memory_job WHERE job_id=?", (job["job_id"],)).fetchone()
        assert row == ("RETRY_WAIT", 0, job["from_seq"], job["to_seq"], job["expected_cursor"], job["expected_epoch"], job["expected_revision"])
        assert db.execute("SELECT COUNT(*) FROM memory_job WHERE kind='maintenance_requeue' AND status='WRITTEN'").fetchone()[0] == 1
        assert db.execute("SELECT extract_cursor FROM memory_scope").fetchone()[0] == 0
    with pytest.raises(ConflictError):
        requeue_job(repo, "other-user", job["job_id"])


def test_cli_apply_requires_backup_directory_and_requeue_requires_scope(tmp_path):
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "db"), "--namespace", "n", "--snapshot", "x", "--apply"])
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "db"), "--namespace", "n", "--requeue-job", "job"])


@pytest.mark.parametrize("change", [
    lambda value: value.update(namespace="client-injected"),
    lambda value: value["scopes"].append(deepcopy(value["scopes"][0])),
    lambda value: value["scopes"][0]["conversations"].append(deepcopy(value["scopes"][0]["conversations"][0])),
    lambda value: value["scopes"][0]["conversations"][0]["messages"][0].update(timestamp="bad"),
])
def test_snapshot_validation_rejects_ambiguous_input(change):
    value = snapshot(); change(value)
    with pytest.raises(MigrationError):
        plan_snapshot(value, "server")


def test_simultaneous_identical_imports_share_one_durable_result(repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    plan = plan_snapshot(snapshot(), repo.namespace)["scopes"][0]
    barrier = Barrier(2)

    class SimultaneousImporter(LegacyImporter):
        def _prepare(self, scope):
            result = super()._prepare(scope)
            barrier.wait(timeout=3)
            return result

    other = MemoryRepository(repo.path, repo.namespace)
    importers = [SimultaneousImporter(repo), SimultaneousImporter(other)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(importer.import_scope, plan) for importer in importers]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result["replayed"] for result in results) == [False, True]
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_turn").fetchone()[0] == 3


def test_sensitive_legacy_profile_candidates_are_filtered_and_only_count_is_audited(repo):
    value = snapshot()
    value["scopes"][0]["profile"] = {
        "preferences": ["safe preference", "password: synthetic-secret", "api_key=sk-syntheticCredential123456", 9],
        "entities": {"access_token": ["synthetic-private-token"], "product": ["keyboard"]},
    }
    plan = plan_snapshot(value, repo.namespace)
    scope = plan["scopes"][0]
    assert scope["skipped_sensitive_candidates"] == 3
    assert {item["content"] for item in scope["candidate_facts"]} == {"safe preference", "9", "product: keyboard"}
    report = plan_report(plan)
    assert report["scopes"][0]["skipped_sensitive_candidates"] == 3
    result = LegacyImporter(repo).import_scope(scope)
    assert result["skipped_sensitive_candidates"] == 3
    assert "synthetic-secret" not in str(result) and "synthetic-private-token" not in str(report)
    with sqlite3.connect(repo.path) as db:
        rows = db.execute("SELECT content FROM memory_fact").fetchall()
        assert "synthetic-secret" not in str(rows)
        assert "synthetic-private-token" not in str(rows)
        audit = db.execute("SELECT result FROM memory_job WHERE kind='legacy_import'").fetchone()[0]
        assert json.loads(audit)["skipped_sensitive_candidates"] == 3
        assert "syntheticCredential" not in audit
