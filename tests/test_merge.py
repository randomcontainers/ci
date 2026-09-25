import pytest

from conftest import SHA, make_planner, publish_bases
from rc import merge
from rc.errors import RcError
from rc.plan import find_target
from rc.registry import Registry

AMD = "sha256:" + "1" * 64
ARM = "sha256:" + "2" * 64
IMAGE = "ghcr.io/randomcontainers/ffmpeg"


@pytest.fixture
def ffmpeg_plan(distros, listed, packages, fake):
    publish_bases(fake)
    return make_planner(distros, listed, fake, "randomcontainers/ffmpeg").plan_package(packages["ffmpeg"])


def digests(tmp_path, flavour="slim", arches=("amd64", "arm64")):
    values = {"amd64": AMD, "arm64": ARM}
    for arch in arches:
        (tmp_path / f"{flavour}-{arch}").write_text(values[arch] + "\n")
    return tmp_path


def decide(plan, tmp_path, fake, *, head=SHA, ref="refs/heads/main", event="push", distro="ubuntu", arches=("amd64", "arm64")):
    target = find_target(plan, "slim", distro)
    return merge.decide(
        plan,
        target,
        digests(tmp_path, arches=arches),
        event_name=event,
        ref=ref,
        sha=SHA,
        head=lambda: head,
        registry=Registry(fake, {"ghcr.io": ("bot", "token")}),
    )


def test_first_publish_moves_every_tag(ffmpeg_plan, tmp_path, fake):
    d = decide(ffmpeg_plan, tmp_path, fake)
    assert not d.skip and d.held == []
    target = find_target(ffmpeg_plan, "slim", "ubuntu")
    assert d.tags == target["tags"]
    assert d.digests == [AMD, ARM]
    assert d.ref == f"{IMAGE}:9.0.2-slim-ubuntu26.04"
    args = d.args
    assert args[args.index("--tag") + 1] == f"{IMAGE}:slim"
    assert f"index:org.opencontainers.image.version=9.0.2" in args
    assert args[-2:] == [f"{IMAGE}@{AMD}", f"{IMAGE}@{ARM}"]
    assert sum(1 for a in args if a.startswith(f"{IMAGE}@")) == 2


def test_incomplete_platforms_skip(ffmpeg_plan, tmp_path, fake):
    d = decide(ffmpeg_plan, tmp_path, fake, arches=("amd64",))
    assert d.skip and "linux/arm64" in d.reason and d.args == []


def test_only_main_publishes(ffmpeg_plan, tmp_path, fake):
    assert decide(ffmpeg_plan, tmp_path, fake, ref="refs/heads/feature").skip
    assert decide(ffmpeg_plan, tmp_path, fake, event="pull_request").skip


def test_main_moved_on(ffmpeg_plan, tmp_path, fake):
    d = decide(ffmpeg_plan, tmp_path, fake, head="b" * 40)
    assert d.skip and "moved on" in d.reason


def test_floating_tags_never_move_backwards(ffmpeg_plan, tmp_path, fake):
    newer = {"org.opencontainers.image.version": "9.1.0"}
    older = {"org.opencontainers.image.version": "8.1.3"}
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "slim", annotations=newer)
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "9-slim", labels=newer)
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "latest", annotations=older)
    d = decide(ffmpeg_plan, tmp_path, fake)
    assert not d.skip
    assert "slim" not in d.tags and "9-slim" not in d.tags
    assert d.held == ["slim (at 9.1.0)", "9-slim (at 9.1.0)"]
    assert "latest" in d.tags
    # version tags are always written
    assert "9.0.2-slim" in d.tags and "9.0.2-slim-ubuntu26.04" in d.tags


def test_same_version_rebuild_moves_tags(ffmpeg_plan, tmp_path, fake):
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "slim", annotations={"org.opencontainers.image.version": "9.0.2"})
    d = decide(ffmpeg_plan, tmp_path, fake)
    assert "slim" in d.tags and d.held == []


def test_unreadable_tag_fails(ffmpeg_plan, tmp_path, fake):
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "slim")
    fake.private.add(("ghcr.io", "randomcontainers/ffmpeg"))
    target = find_target(ffmpeg_plan, "slim", "ubuntu")
    with pytest.raises(RcError, match="cannot read"):
        merge.decide(
            ffmpeg_plan, target, digests(tmp_path), event_name="push", ref="refs/heads/main", sha=SHA,
            head=lambda: SHA, registry=Registry(fake),
        )


def test_bad_digest_file(ffmpeg_plan, tmp_path, fake):
    (tmp_path / "slim-amd64").write_text("sha256:nothex\n")
    (tmp_path / "slim-arm64").write_text(ARM)
    target = find_target(ffmpeg_plan, "slim", "ubuntu")
    with pytest.raises(RcError, match="does not hold"):
        merge.decide(ffmpeg_plan, target, tmp_path, event_name="push", ref="refs/heads/main", sha=SHA,
                     head=lambda: SHA, registry=Registry(fake))


def test_main_head(fake):
    fake.head_sha = "f" * 40
    assert merge.main_head(fake, "randomcontainers/ffmpeg", "token") == "f" * 40
    method, url, headers = fake.requests[-1]
    assert url == "https://api.github.com/repos/randomcontainers/ffmpeg/commits/main"
    assert headers["Accept"] == "application/vnd.github.sha"
    with pytest.raises(RcError):
        merge.main_head(fake, "not a repo", None)


def test_annotation_values_cannot_break_lines():
    with pytest.raises(RcError):
        merge.imagetools_args(IMAGE, ["x"], {"org.opencontainers.image.description": "a\nb"}, [AMD])
