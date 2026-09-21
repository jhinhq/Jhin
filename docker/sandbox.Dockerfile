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

FROM node:25-bookworm-slim

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
        python3 \
        python3-venv \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
    && cat /usr/share/readline/inputrc > /etc/inputrc \
    && chmod 0644 /etc/inputrc \
    && cmp -s /usr/share/readline/inputrc /etc/inputrc \
    && test "$(stat -c '%u:%g:%a' /etc/inputrc)" = "0:0:644" \
    && rm -rf /var/lib/apt/lists/*

# Maintained document-generation runtimes, available offline in every sandbox.
RUN python3 -m venv /opt/documents \
    && /opt/documents/bin/pip install --no-cache-dir \
       'python-docx>=1.1,<2' 'openpyxl>=3.1,<4' 'python-pptx>=1,<2' \
       'pypdf>=6,<7' 'reportlab>=4,<5' 'websocket-client>=1.8,<2' \
    && /opt/documents/bin/python -c "import docx,openpyxl,pptx,pypdf,reportlab,websocket"
ENV PATH="/opt/documents/bin:${PATH}" \
    npm_config_cache=/tmp/npm-cache

# node's UID/GID 1000 matches the runner's enforced user (plan 14.3).
RUN groupmod --new-name sandbox node \
    && usermod --login sandbox --home /home/sandbox --move-home --shell /bin/bash node \
    && mkdir -p /workspace \
    && chown 1000:1000 /workspace

USER 1000:1000
WORKDIR /workspace
CMD ["bash"]
