import json

import pytest

from fakegithub import FakeGitHub
from rc import reposetup
from rc.errors import RcError
from rc.github import GitHub


def test_requests_round_trip(tmp_path):
    requests = [reposetup.create("a-b", "A with B."), reposetup.topics("a-b", ["randomcontainers", "a", "b"])]
    path = tmp_path / "setup.json"
    reposetup.dump(requests, path)
    assert reposetup.load(path) == requests
    assert requests[0] == {"kind": "create", "name": "a-b", "description": "A with B."}


@pytest.mark.parametrize(
    "request_",
    [
        {"kind": "delete", "name": "a-b"},
        {**reposetup.create("a-b", "A with B."), "homepage": "https://example.org/"},
        {**reposetup.create("a-b", "A with B."), "private": True},
        reposetup.create("a-b", "A\nwith B."),
        reposetup.create("a-b", "x" * 161),
        reposetup.create("ci", "Reserved."),
        reposetup.create("../a", "Path."),
        reposetup.topics("a-b", ["Not A Topic"]),
        reposetup.topics("a-b", []),
        reposetup.topics("a-b", [f"t{i}" for i in range(21)]),
        "create a-b",
    ],
)
def test_unexpected_requests_are_refused(tmp_path, request_):
    path = tmp_path / "setup.json"
    path.write_text(json.dumps([request_]))
    with pytest.raises(RcError):
        reposetup.load(path)


def test_one_failed_request_does_not_stop_the_others():
    fake = FakeGitHub()
    fake.add_repo("randomcontainers/a-b", {"README.md": b"# a-b\n"})
    requests = [reposetup.create("a-b", "A with B."), reposetup.create("c-d", "C with D."), reposetup.topics("c-d", ["c", "d"])]
    problems = reposetup.apply(requests, GitHub(fake, "token"), "randomcontainers")
    assert len(problems) == 1 and problems[0].startswith("create randomcontainers/a-b:")
    assert [c["name"] for c in fake.created] == ["c-d"] and fake.topics["randomcontainers/c-d"] == ["c", "d"]
