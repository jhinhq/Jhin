# Default sandbox job image (plan 14, section 39 SANDBOX_DEFAULT_IMAGE).
#
# Deliberately lean: enough for repository work (git), scripting (bash,
# python3, node), and the network/security integration tests (curl). Jobs
# run as the non-root user below with a read-only root filesystem; the only
# writable paths are /workspace (ephemeral volume or tmpfs) and /tmp (tmpfs).
#
# Policy scope patterns (grant scope key "image") control which images an
# agent may request; teams can build richer toolchain images and allow them
# per agent.

FROM debian:bookworm-slim

# readline-common's first-install script uses a metadata-preserving copy when
# /etc/inputrc is absent. Rootless overlay2 rejects that metadata operation, so
# create the exact destination first and later populate only its contents.
RUN install -m 0644 /dev/null /etc/inputrc \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        nodejs \
        python3 \
    && cat /usr/share/readline/inputrc > /etc/inputrc \
    && chmod 0644 /etc/inputrc \
    && cmp -s /usr/share/readline/inputrc /etc/inputrc \
    && test "$(stat -c '%u:%g:%a' /etc/inputrc)" = "0:0:644" \
    && rm -rf /var/lib/apt/lists/*

# uid/gid 1000 matches the runner's enforced "User": "1000:1000" (plan 14.3).
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown 1000:1000 /workspace

USER 1000:1000
WORKDIR /workspace
CMD ["bash"]
