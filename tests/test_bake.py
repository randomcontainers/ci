import json

import pytest

from conftest import make_planner, publish_bases, publish_slim
from rc import bake
from rc.errors import RcError

DIGEST = "sha256:" + "c" * 64


@pytest.fixture
def ytdlp_plan(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2")
    return make_planner(distros, listed, fake, "randomcontainers/yt-dlp").plan_package(packages["yt-dlp"])


def test_load_document(ytdlp_plan):
    doc, files = bake.bake_document(ytdlp_plan, "alpine", "linux/arm64", "load", tag_prefix="rclocal/", jobs=4)
    assert doc["group"] == {"build": {"targets": ["slim", "default"]}}
    slim, default = doc["target"]["slim"], doc["target"]["default"]
    assert slim["context"] == "src" and slim["dockerfile"] == "Dockerfile.alpine" and slim["target"] == "slim"
    assert slim["platforms"] == ["linux/arm64"]
    assert slim["args"]["JOBS"] == "4" and slim["args"]["SOURCE_DATE_EPOCH"] == ytdlp_plan["epoch"]
    assert slim["args"]["BASE_IMAGE"].startswith("alpine:3.24@sha256:")
    assert slim["tags"] == ["rclocal/yt-dlp:slim-alpine"]
    assert slim["output"] == ["type=docker"]
    assert slim["attest"] == ["type=provenance,disabled=true"]
    assert default["context"] == ".rc-build/alpine-default"
    assert default["contexts"]["yt-dlp"] == "target:slim"
    assert default["contexts"]["ffmpeg"].startswith("docker-image://ghcr.io/randomcontainers/ffmpeg@sha256:")
    assert default["labels"]["com.randomcontainers.variant"] == "default"
    assert list(files) == [".rc-build/alpine-default/Dockerfile"]
    assert "apk add --no-cache deno" in files[".rc-build/alpine-default/Dockerfile"]
    json.dumps(doc)


def test_push_document(ytdlp_plan):
    doc, _ = bake.bake_document(ytdlp_plan, "ubuntu", "linux/amd64", "push")
    for spec in doc["target"].values():
        assert spec["output"] == [
            "type=image,name=ghcr.io/randomcontainers/yt-dlp,push-by-digest=true,name-canonical=true,push=true"
        ]
        assert spec["attest"] == ["type=provenance,mode=max", "type=sbom"]
        assert "tags" not in spec


def test_same_inputs_for_load_and_push(ytdlp_plan):
    load, _ = bake.bake_document(ytdlp_plan, "ubuntu", "linux/amd64", "load")
    push, _ = bake.bake_document(ytdlp_plan, "ubuntu", "linux/amd64", "push")
    for name in ("slim", "default"):
        for key in ("context", "dockerfile", "args", "labels", "platforms"):
            assert load["target"][name][key] == push["target"][name][key]


def test_rejects_unknown_platform_and_distro(ytdlp_plan):
    with pytest.raises(RcError):
        bake.bake_document(ytdlp_plan, "ubuntu", "linux/s390x", "load")
    with pytest.raises(RcError):
        bake.bake_document(ytdlp_plan, "debian", "linux/amd64", "load")


MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def pushed_index(amd64, arm64=None):
    manifests = [{"digest": amd64, "platform": {"os": "linux", "architecture": "amd64"}}]
    if arm64:
        manifests.append({"digest": arm64, "platform": {"os": "linux", "architecture": "arm64"}})
    manifests.append({"digest": "sha256:" + "0" * 64, "platform": {"os": "unknown", "architecture": "unknown"}})
    return {"manifests": manifests}


def test_record_digests(ytdlp_plan, tmp_path):
    slim_m, default_m = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    push = {
        "slim": {"containerimage.digest": DIGEST},
        "default": {"containerimage.digest": "sha256:" + "d" * 64},
    }
    load = {
        "slim": {"containerimage.descriptor": {"mediaType": MANIFEST, "digest": slim_m}},
        "default": {"containerimage.descriptor": {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": default_m}},
    }
    seen = []

    def inspect(ref):
        seen.append(ref)
        return pushed_index(slim_m)

    written, notes = bake.record_digests(ytdlp_plan, "ubuntu", "linux/amd64", push, load, tmp_path, inspect)
    assert sorted(p.name for p in written) == ["default-amd64", "slim-amd64"]
    assert (tmp_path / "slim-amd64").read_text().strip() == DIGEST
    assert seen == [f"ghcr.io/randomcontainers/yt-dlp@{DIGEST}"]
    assert notes == ["could not compare the tested and pushed default images"]


def test_record_digests_detects_a_different_image(ytdlp_plan, tmp_path):
    push = {"slim": {"containerimage.digest": DIGEST}, "default": {"containerimage.digest": DIGEST}}
    load = {"slim": {"containerimage.descriptor": {"mediaType": MANIFEST, "digest": "sha256:" + "9" * 64}}}
    with pytest.raises(RcError, match="not the image that was tested"):
        bake.record_digests(ytdlp_plan, "ubuntu", "linux/amd64", push, load, tmp_path, lambda ref: pushed_index("sha256:" + "8" * 64))


def test_record_digests_needs_a_digest(ytdlp_plan, tmp_path):
    with pytest.raises(RcError):
        bake.record_digests(ytdlp_plan, "ubuntu", "linux/amd64", {"slim": {}}, None, tmp_path)
