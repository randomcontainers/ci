# $name

$summary

$intro It contains $title, on $distro_names, for `linux/amd64` and `linux/arm64`.$agpl

These are unofficial builds, not affiliated with or endorsed by the upstream projects. Report problems with the image in [$org/$owner](https://github.com/$org/$owner/issues) and problems with a tool itself in that tool's own issue tracker.

## Quick start

```sh
$quick_start
```

The same images can also be pulled as `$alias/$name`. The examples in the [$owner README](https://github.com/$org/$owner#readme) work with this image too.

## Tags

`<version>` is the $owner_title version.

| Tags | Base |
|---|---|
$tag_rows

`latest` and `<version>` are the $default_distro images. The images are currently built on $distro_list. Tags without a distro version move to the next distro release when the project does; tags ending in $distro_suffixes stay on that release and are no longer rebuilt once the project moves to the next one.

Only the tags of the $owner_title version currently in [$owner's package.yml](https://github.com/$org/$owner/blob/main/package.yml) are rebuilt, exact-version tags included, so pin a digest when you need the same bytes every time. Tags of older versions stay as they were last built. The published tags and their digests are listed on the [package page](https://github.com/orgs/$org/packages/container/package/$name).

## Platforms

`linux/amd64` and `linux/arm64`, both built natively on GitHub-hosted runners.

## Files and permissions

The working directory is `/work`. The image runs as UID 1000, and any other UID works too: `HOME` is then `/`, and caches go to `/cache`, which anyone can write to. How to get output files owned by you depends on how you run containers:

| Runtime | Flag |
|---|---|
| Docker on Linux (rootful), GitHub Actions | `--user "$$(id -u):$$(id -g)"` |
| Rootless Podman | `--userns=keep-id` |
| Rootless Docker | `--user 0:0` (root in the container is your user on the host) |
| Docker Desktop on macOS or Windows | none, file ownership is mapped for you |

## Contents

| Package | License | Repository |
|---|---|---|
$member_rows

${extras}## Verifying

Each image has a build provenance attestation from this repository's GitHub Actions run. The run uses the shared build workflow of [$org/ci](https://github.com/$org/ci), which signs the attestation, so name both repositories:

```sh
gh attestation verify oci://$registry/$org/$name:latest \
  --repo $org/$name --signer-repo $org/ci
```

Images from `$registry/$org/$owner` are built in [$org/$owner](https://github.com/$org/$owner), so verify them with `--repo $org/$owner --signer-repo $org/ci`.

## Updates

The images are rebuilt when a new image of a package above is published, for example after an upstream release. They are also rebuilt when the base image changes and at least every 7 days, so distro security fixes reach the current tags. The package repositories describe how each upstream release is picked up.

## Licenses

The image contents are licensed under $license. The version of each package is in `$meta/<package>/version` and its license files are in `$meta/<package>/licenses/`.

${sources}Ubuntu and Alpine packages keep their own licenses. The SBOM of each platform image lists them with their versions:

```sh
docker buildx imagetools inspect $registry/$org/$name:latest --format '{{ json .SBOM }}'
```

The files in this repository are available under the MIT license, see [LICENSE](LICENSE).

## Requesting a tool

To suggest another tool, use the [Request a tool](https://github.com/$org/.github/issues/new?template=tool-request.yml) form.

## This repository

The files here are generated from the `combos` section of `package.yml` in [$org/$owner](https://github.com/$org/$owner). Open issues and pull requests there.
