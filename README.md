# ci

Build tooling for the randomcontainers images. This repository holds the `rc` command, the reusable workflow that every package and combo repository calls to build and publish its images, and the reconciler that moves packages to new upstream releases and rebuilds images when their base or members change.

```
tick.yml, every 15 minutes
  |
  v
reconcile.yml (rc reconcile)
  |-- commit "Update yt-dlp to 2026.09.20" ------> yt-dlp, ffmpeg, ...     (package repositories)
  |-- commit generated files --------------------> yt-dlp-ffmpeg, ...      (combo repositories)
  |-- dispatch build.yml as "Build <want>" ------> either of them
  |-- status/catalog.json, tracking issue
  |
package or combo repository: .github/workflows/build.yml
  |   uses randomcontainers/ci/.github/workflows/build.yml@v1
  v
ghcr.io/randomcontainers/<name>
```

| Path | What it is |
|---|---|
| `distros.yml` | Base images. `default` is the distro behind `latest`, `slim` and the plain version tags. |
| `packages.yml` | Package repositories that are built and published. Add one only after reviewing it. |
| `.github/workflows/build.yml` | The reusable build workflow. |
| `.github/workflows/tick.yml` | Starts `reconcile.yml` every 15 minutes. |
| `.github/workflows/reconcile.yml` | Runs `rc reconcile` and publishes its results. |
| `status/catalog.json` | Machine-readable list of the published images with their variants and tags, written by the reconciler. |
| `setup-rc/` | Composite action that installs `rc` from the same commit as the workflow. |
| `src/rc/` | The `rc` command. |
| `src/rc/templates/combo/` | Files of the generated combo repositories. |
| `tests/` | Unit tests, package fixtures and the expected combo Dockerfiles. |
| `docs/` | [How it works](docs/architecture.md) and [adding a package](docs/adding-a-package.md). |

## Using the build workflow

Each package repository has this `.github/workflows/build.yml`:

```yaml
name: Build

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
    inputs:
      default-only:
        description: Rebuild only the default image on top of the published slim image
        type: boolean
        default: false
      want:
        description: Request id set by the reconciler
        type: string
        default: ""

run-name: ${{ inputs.want != '' && format('Build {0}', inputs.want) || '' }}

concurrency:
  group: ${{ github.event_name == 'pull_request' && format('pr-{0}', github.ref) || 'publish' }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions: {}

jobs:
  build:
    uses: randomcontainers/ci/.github/workflows/build.yml@v1
    permissions:
      contents: write # source release
      packages: write # push images
      id-token: write # sign the attestation
      attestations: write # store the attestation
      artifact-metadata: write # record the image
      security-events: write # upload scan results
    with:
      default-only: ${{ inputs.default-only == true }}
      want: ${{ inputs.want }}
```

Combo repositories use the same file with only the `workflow_dispatch` trigger and `default-only: false`. `rc render --repo-files` writes it for them, together with `combo.yml`, the Dockerfiles, the README, `LICENSE`, `.hadolint.yaml` and `.github/zizmor.yml`.

A run has five jobs:

1. `plan` runs hadolint on the repository's `Dockerfile.*` with its `.hadolint.yaml`. It then reads `package.yml` (or `combo.yml`), resolves each base image and each member's `slim-<distro>` image to an index digest once, and decides what to build. The plan is uploaded as the `plan` artifact, which the `build`, `merge` and `release` jobs download. A default image whose members cannot be pulled anonymously, or were built on another distro release, is skipped with a notice. A combo that fails the `rc validate --catalog` checks against the listed packages is never built: in a combo repository the plan fails, and in a package repository only the slim images are built, with an error annotation.
2. `build` runs per distro and platform on native runners (`ubuntu-24.04`, `ubuntu-24.04-arm`). It builds the slim and default targets with `docker buildx bake`, loads them, runs `rc check-image` and `rc test-image`, and only then logs in and pushes by digest with provenance and an SBOM.
3. `merge` creates the multi-platform index and tags per distro and flavour, and attests it. It publishes only from `main`, only if the run's commit is still the head of `main`, and never moves a floating tag to an older version.
4. `release` attaches the verified upstream sources to the `v<version>` release once the images are published, for packages with `source-release: true`: the tarball or an archive of the pinned git commit, and every extra artifact. It uploads only the files the release does not have yet.
5. `scan` runs Grype on the published images and uploads the results to code scanning. It never fails the run.

Pull requests build and test both images on both platforms but push nothing.

Every job that runs `rc` (`plan`, `build`, `merge`, `release`) checks out this repository at `job.workflow_sha` into `.rc/` and installs `rc` from there, so the workflow and `rc` always come from the same commit. PyYAML, the only dependency, is installed from `requirements.txt` with `--require-hashes`.

## The reconciler

`reconcile.yml` runs `rc reconcile` from `main` of this repository. Each pass reads every listed `package.yml` and compares it with its upstream (PyPI, GitHub, GitLab, a Forgejo server such as Codeberg, or a download page) and with what is published on ghcr.io. It then:

- commits a new upstream version to the package repository, after the release has passed its cooldown, its files are published and, on PyPI, its provenance names the expected publisher. Tarballs are downloaded once and their sha256 pinned in `package.yml`, a git tag is resolved to the commit it points to and that commit is pinned, and Python lock files are regenerated with uv.
- re-locks the Python dependencies of a package once a week and commits the result when it changed.
- creates missing combo repositories and commits their generated files when they changed. It only writes to a combo repository whose `combo.yml` names the same owner.
- dispatches `build.yml` where the published image differs from what it should be: another version or commit, a new base image or member image, or an image older than 7 days.
- writes `status/catalog.json` and keeps one tracking issue in this repository up to date with anything that needs a person.

`rc reconcile --dry-run` prints what a pass would do and changes nothing. [docs/architecture.md](docs/architecture.md) describes each step.

## rc

```
rc validate [PATH ...] [--files] [--catalog]            check package.yml and combo.yml files
rc plan --source DIR                                    write the build plan and job outputs
rc render --package-file FILE [--repo-files DIR]        write the combo Dockerfile or a combo repository
rc bake --plan FILE --distro D --platform P --mode M    write docker-bake.json for one build leg
rc check-image --plan FILE --target T --distro D        check a built image against the image contract
rc test-image --plan FILE --target T --distro D         run the package tests in a built image
rc tags --package-file FILE                             print the tags each image gets
rc digests ...                                          record pushed digests for the merge job
rc merge ...                                            decide the tags and write the imagetools arguments
rc source-release ...                                   download and verify the upstream sources
rc reconcile [--dry-run] [--output-dir DIR]             update packages and combos, dispatch due builds
rc apply-commits FILE                                   make the commits rc reconcile planned
rc setup-repos FILE                                     make the repository changes rc reconcile asked for
rc catalog [--output FILE] [--empty]                    write the catalog of published images
rc lock FILE [--requirement REQ]                        write a hash-locked requirements file with uv
```

`rc <command> --help` lists every option. Member `package.yml` files are read from the `main` branch of each package repository. For a member taken from a published image, `rc plan` reads the `package.yml` at the commit in the image's `org.opencontainers.image.revision` label, or `main` with a notice when the label is missing. With `--packages-dir DIR`, every definition comes from `DIR/<name>/package.yml`.

`rc reconcile` and `rc catalog` read public data without a token. In the workflow they get `RC_GITHUB_TOKEN` (reads and dispatches) and `RC_CI_TOKEN` (this repository). A dry run against the real services, with the package repositories checked out next to this one:

```sh
rc reconcile --dry-run --packages-dir ..
```

The reconciler never gets a token that can write to a repository. `rc reconcile --commit-file FILE` writes the commits it plans to `FILE`, and `rc apply-commits FILE` makes them with `RC_WRITE_TOKEN`, which `reconcile.yml` mints only when the file exists and only for the repositories it names. In the same way, `rc reconcile --setup-file FILE` writes the combo repositories to create and the topics to set, and `rc setup-repos FILE` makes those changes with `RC_ADMIN_TOKEN`, the only token with the Administration permission.

`rc plan` and `rc validate --files` also check the package repository itself: a `Dockerfile.<distro>` for every distro that declares `ARG BASE_IMAGE` and `ARG VERSION` (plus `ARG SOURCE_SHA256` for tarball builds, `ARG SOURCE_COMMIT` for git tag builds and `ARG <NAME>_SHA256` for each extra artifact the distro uses) and ends in a stage named `slim`, and any requirements file a combo installs. They also compare the `ARG BASE_IMAGE` defaults and the distro releases named in `README.md` with `distros.yml`: `rc validate --files` reports a difference as an error, `rc plan` as a warning. `rc validate --catalog` needs `--packages-dir`.

## Building an image locally

From a directory that holds checkouts of the package repositories:

```sh
rc plan --source ffmpeg --packages-dir . --offline --output-file plan.json
rc bake --plan plan.json --distro alpine --platform linux/amd64 --mode load \
  --source-dir ffmpeg --jobs 4 --output .rc-build/bake.json
docker buildx bake --file .rc-build/bake.json build
rc check-image --plan plan.json --target slim --distro alpine
rc test-image --plan plan.json --target slim --distro alpine
```

`--offline` skips the registries: bases stay as plain tags, and combo members come from local images passed as `--member-image ffmpeg=local/ffmpeg:slim-{distro}`.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/pip install --no-deps -e .   # puts rc on the venv's PATH
```

The tests use an in-memory registry, GitHub and PyPI and never touch the network. They read the catalog in `tests/fixtures/ci` and the packages in `tests/fixtures/packages`, not `packages.yml`, so listing a new package does not change them. `requirements.txt`, `requirements-dev.txt` and `requirements-reconcile.txt` (the uv that the reconciler runs) are generated from the `.in` files with the `uv pip compile` command at the top of each file. If a change to `rc render` is intended, update the files in `tests/golden/`.

## License

MIT, see [LICENSE](LICENSE). This covers the files in this repository, not the software inside the images.
