FROM debian:bookworm-slim

ARG ANKI_VERSION=25.02.7
# Pull AnkiConnect from sourcehut (FooSoft's GitHub repo is archived; the
# maintained source moved to git.sr.ht/~foosoft/anki-connect). The sourcehut
# blob endpoint serves raw file content. Pinned to a specific commit so the
# build is reproducible — bump intentionally, not silently on rebuild.
ARG ANKICONNECT_REV=de6e6e1b8aaf4ae195eb1d1ff6db5409b99b2a3e

ENV DEBIAN_FRONTEND=noninteractive \
    ANKI_BASE=/data \
    DISPLAY=:99 \
    QT_QPA_PLATFORM=xcb \
    QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu" \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl zstd xz-utils \
      xvfb x11-utils \
      libgl1 libegl1 libdbus-1-3 libnss3 libxcomposite1 libxdamage1 \
      libxrandr2 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 \
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
      libxcb-render-util0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 \
      libxcb-xinerama0 libxcb-xkb1 libxcb1 libxi6 libxtst6 \
      libfontconfig1 libfreetype6 libasound2 libpulse0 \
      libnspr4 libxml2 libxslt1.1 libsecret-1-0 \
      fonts-dejavu-core fonts-noto-core \
      python3 python3-pip jq patch \
      xdotool scrot net-tools procps \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/anki

RUN curl -fsSL -o anki.tar.zst \
      "https://github.com/ankitects/anki/releases/download/${ANKI_VERSION}/anki-${ANKI_VERSION}-linux-qt6.tar.zst" \
    && tar --use-compress-program=unzstd -xf anki.tar.zst --strip-components=1 \
    && rm anki.tar.zst \
    && ls /opt/anki/

ENV PATH="/opt/anki:${PATH}"

COPY patches /opt/anki-addon-patches

RUN mkdir -p /opt/anki-addon-src \
    && curl -fsSL -o /tmp/ac.tar.gz \
       "https://git.sr.ht/~foosoft/anki-connect/archive/${ANKICONNECT_REV}.tar.gz" \
    && tar xzf /tmp/ac.tar.gz -C /tmp/ \
    && cd "/tmp/anki-connect-${ANKICONNECT_REV}" \
    && for p in /opt/anki-addon-patches/*.patch; do \
         echo "applying $p" && patch -p1 --no-backup-if-mismatch < "$p" || exit 1; \
       done \
    && cd / \
    && mv "/tmp/anki-connect-${ANKICONNECT_REV}/plugin" /opt/anki-addon-src/AnkiConnect \
    && rm -rf /tmp/ac.tar.gz "/tmp/anki-connect-${ANKICONNECT_REV}" \
    && sed -i \
       -e "s|f'Deck \"{note\['deckName'\]}\" not found'|f\"Deck \\\\\"{note['deckName']}\\\\\" not found\"|" \
       -e "s|f'Model \"{note\['modelName'\]}\" not found'|f\"Model \\\\\"{note['modelName']}\\\\\" not found\"|" \
       /opt/anki-addon-src/AnkiConnect/__init__.py \
    && for f in /opt/anki-addon-src/AnkiConnect/*.py; do \
         python3 -c "import py_compile, sys; py_compile.compile('$f', doraise=True)" || exit 1; \
       done \
    && echo "addon files compile OK"

COPY ankiconnect-config.json /opt/anki-addon-runtime-config.json
COPY seedlogin-src /opt/seedlogin-src
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["anki", "-b", "/data", "-l", "en", "-p", "User 1"]
