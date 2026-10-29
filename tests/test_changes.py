from wis2node import changes as ch


def test_add_modify_delete(repo):
    repo.write("metadata/a.json", {"id": "a"})
    repo.write("metadata/b.json", {"id": "b"})
    base = repo.commit()
    repo.write("metadata/a.json", {"id": "a", "v": 2})
    repo.remove("metadata/b.json")
    repo.write("metadata/c.json", {"id": "c"})
    head = repo.commit()
    got = set(ch.changed_records(base, head, cwd=str(repo.path)))
    assert got == {ch.Change("M", "metadata/a.json"), ch.Change("D", "metadata/b.json"),
                   ch.Change("A", "metadata/c.json")}


def test_rename_is_delete_plus_add(repo):
    repo.write("metadata/old.json", {"id": "x"})
    base = repo.commit()
    repo.git("mv", "metadata/old.json", "metadata/new.json")
    head = repo.commit()
    got = set(ch.changed_records(base, head, cwd=str(repo.path)))
    assert got == {ch.Change("D", "metadata/old.json"), ch.Change("A", "metadata/new.json")}


def test_zero_base_means_all_added(repo):
    repo.write("metadata/a.json", {"id": "a"})
    head = repo.commit()
    assert ch.changed_records(ch.ZERO_SHA, head, cwd=str(repo.path)) == [ch.Change("A", "metadata/a.json")]


def test_non_record_paths_ignored_and_reported(repo):
    base = repo.commit()
    repo.write("metadata/sub/x.json", {"id": "x"})
    repo.write("metadata/notes.txt", "hello")
    repo.write("README.md", "x")
    head = repo.commit()
    assert ch.changed_records(base, head, cwd=str(repo.path)) == []
    assert sorted(ch.other_paths(base, head, cwd=str(repo.path))) == ["metadata/notes.txt", "metadata/sub/x.json"]


def test_bad_revision_raises(repo):
    import pytest
    with pytest.raises(ch.GitError):
        ch.changed_records("deadbeef", "HEAD", cwd=str(repo.path))
