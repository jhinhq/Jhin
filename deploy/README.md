# Release Compose bundle

`compose.release.yaml` is the pull-based production manifest for Jhin. It
has no `build` keys, no fake or development services, and publishes only the
web entry point (bound to loopback by default so a TLS reverse proxy fronts
it). `compose.release.rootful.yaml` and `compose.release.rootless.yaml` are
the two Docker-socket overlays for `sandbox-runner`; use exactly one.

The release workflow (`.github/workflows/release.yml`) renders this
directory into `jhin-<version>-compose.tar.gz` with
`scripts/render_release_bundle.py`, replacing every `${JHIN_VERSION}` image
tag with the exact digest from `image-lock.json`, and adds
`config/nats.conf`, `MANIFEST.SHA256`, `VERIFY.md`, `LICENSE`, `README.md`,
`SECURITY.md`, and `docs/deployment.md`.

## Running from this directory (tagged images, not digest-pinned)

```bash
cd deploy
cp .env.release.example .env       # fill in every REQUIRED / GENERATED value
mkdir -p config && cp ../config/nats.conf config/
export SANDBOX_DOCKER_GID="$(stat -c %g /var/run/docker.sock)"   # rootful example
docker compose -f compose.release.yaml -f compose.release.rootful.yaml pull
docker compose -f compose.release.yaml -f compose.release.rootful.yaml up -d --wait --wait-timeout 300
docker compose -f compose.release.yaml run --rm --no-deps api jhin-db-migrate
```

Set `SANDBOX_RUNNER_IMAGE=ghcr.io/teachmetech/jhin-sandbox-runner:<version>`
and `SANDBOX_DEFAULT_IMAGE=ghcr.io/teachmetech/jhin-sandbox:<version>` in
`.env`, and pull the sandbox job image on the daemon the runner uses
(`docker pull "$SANDBOX_DEFAULT_IMAGE"`).

Prefer the rendered bundle from a GitHub Release for production: it pins
every image by digest and ships checksums and Sigstore bundles. The full
procedure, reverse-proxy configuration, backups, and upgrades are in
[docs/deployment.md](../docs/deployment.md).
