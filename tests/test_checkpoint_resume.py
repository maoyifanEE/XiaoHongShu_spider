from xhs_profile_exporter.checkpoint import Checkpoint


def _touch(path, mtime):
    path.touch()
    import os

    os.utime(path, (mtime, mtime))


def test_successful_checkpoint_not_resumable(tmp_path):
    cp = Checkpoint(run_id="run1", creator_id="creator")
    cp.mark_complete()
    cp.save(tmp_path)
    assert Checkpoint.load_latest(tmp_path, "creator") is None


def test_new_success_checkpoint_blocks_older_safe_stop_resume(tmp_path):
    old_cp = Checkpoint(run_id="run1", creator_id="creator", completed_note_ids=["a"])
    old_cp.mark_safe_stop("MAX_CONSECUTIVE_ERRORS")
    old_path = old_cp.save(tmp_path)

    new_cp = Checkpoint(run_id="run2", creator_id="creator", completed_note_ids=["a", "b"])
    new_cp.mark_complete()
    new_path = new_cp.save(tmp_path)

    _touch(old_path, 100)
    _touch(new_path, 200)
    assert Checkpoint.load_latest(tmp_path, "creator") is None


def test_incomplete_checkpoint_resumable(tmp_path):
    cp = Checkpoint(run_id="run1", creator_id="creator", completed_note_ids=["a"])
    cp.mark_safe_stop("MAX_CONSECUTIVE_ERRORS")
    cp.save(tmp_path)
    loaded = Checkpoint.load_latest(tmp_path, "creator")
    assert loaded is not None
    assert loaded.completed_note_ids == ["a"]
    assert loaded.is_resumable is True


def test_resume_completed_ids_accumulate_without_transient_urls(tmp_path):
    cp = Checkpoint(run_id="run1", creator_id="creator", completed_note_ids=[f"{idx:024x}" for idx in range(50)])
    cp.mark_safe_stop("USER_INTERRUPTED")
    cp.save(tmp_path)
    loaded = Checkpoint.load_latest(tmp_path, "creator")
    assert loaded is not None
    loaded.run_id = "run2"
    loaded.completed_note_ids = sorted(set(loaded.completed_note_ids) | {f"{idx:024x}" for idx in range(50, 60)})
    path = loaded.save(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert len(loaded.completed_note_ids) == 60
    assert "xsec_token=" not in text
    assert "access_url" not in text
    assert "token=" not in text
