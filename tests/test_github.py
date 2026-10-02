import json

import pytest

from fakegithub import FakeGitHub
from rc.errors import RcError
from rc.github import GitHub, GitHubError, blob_sha
from rc.http import Response


def test_blob_sha_matches_git():
    # git hash-object of "hello\n"
    assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_commit_uses_git_data_api():
    fake = FakeGitHub()
    head = fake.add_repo("x", {"a.txt": b"a\n", "b.txt": b"b\n"})
    gh = GitHub(fake, "token")
    sha = gh.commit("randomcontainers/x", head, {"b.txt": b"B\n", "dir/c.txt": b"c\n"}, "Change b")
    assert fake.heads["randomcontainers/x"] == sha
    assert fake.files("randomcontainers/x") == {"a.txt": b"a\n", "b.txt": b"B\n", "dir/c.txt": b"c\n"}
    methods = [(m, u.split("/repos/randomcontainers/x")[1]) for m, u, _, _ in fake.requests]
    assert methods[-1] == ("PATCH", "/git/refs/heads/main")
    assert json.loads(fake.requests[-1][3]) == {"sha": sha, "force": False}
    assert all(h["Authorization"] == "Bearer token" for _, _, h, _ in fake.requests)


def test_commit_refuses_odd_paths():
    fake = FakeGitHub()
    head = fake.add_repo("x", {"a": b""})
    with pytest.raises(RcError):
        GitHub(fake).commit("randomcontainers/x", head, {"../a": b""}, "m")


def test_raw_reads_never_send_the_token():
    fake = FakeGitHub()
    head = fake.add_repo("x", {"package.yml": b"name: x\n"})
    gh = GitHub(fake, "secret")
    assert gh.raw("randomcontainers/x", head, "package.yml") == b"name: x\n"
    assert gh.raw("randomcontainers/x", head, "missing.yml") is None
    assert all("Authorization" not in h for _, u, h, _ in fake.requests if "raw.githubusercontent.com" in u)


class Pages:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, body=None):
        self.calls.append(url)
        if "page=2" in url:
            return Response(200, {}, json.dumps({"repositories": [{"full_name": "o/b", "topics": ["x"]}]}).encode())
        link = '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
        return Response(200, {"link": link}, json.dumps({"repositories": [{"full_name": "o/a"}]}).encode())


def test_pagination_follows_link_headers():
    transport = Pages()
    assert GitHub(transport).installation_repositories() == {"o/a": None, "o/b": ["x"]}
    assert len(transport.calls) == 2


def test_errors_carry_status():
    class Denied:
        def request(self, method, url, headers, body=None):
            return Response(403, {}, b'{"message": "API rate limit exceeded"}')

    with pytest.raises(GitHubError, match="HTTP 403: API rate limit exceeded") as info:
        GitHub(Denied()).head("o/r")
    assert info.value.status == 403


def test_missing_repository_and_branch():
    fake = FakeGitHub()
    gh = GitHub(fake)
    assert gh.head("randomcontainers/none") is None
    assert gh.repository("randomcontainers/none") is None
    with pytest.raises(RcError):
        gh.head("bad name/x")


def test_jobs_of_a_run():
    fake = FakeGitHub()
    fake.add_repo("x", {".github/workflows/build.yml": b"name: Build\n"})
    run = fake.add_run("randomcontainers/x", "Build", published=("slim alpine",))
    jobs = GitHub(fake).jobs("randomcontainers/x", run["id"])
    assert [j.name for j in jobs] == ["build / Plan", "build / Publish slim alpine"]
    assert jobs[1].conclusion == "success" and jobs[1].steps["Create index"] == "success"
    assert "filter=latest" in fake.requests[-1][1]


def test_new_repository_and_topics():
    fake = FakeGitHub()
    gh = GitHub(fake, "token")
    gh.create_repository("randomcontainers", "a-b", "A with B.")
    gh.set_topics("randomcontainers/a-b", ["randomcontainers", "Not A Topic", "a"])
    assert fake.created[0]["visibility"] == "public" and fake.created[0]["auto_init"] is True
    assert fake.topics["randomcontainers/a-b"] == ["randomcontainers", "a"]
    assert "homepage" not in fake.created[0] and gh.repository("randomcontainers/a-b")["description"] == "A with B."
