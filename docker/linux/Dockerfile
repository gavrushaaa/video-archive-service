FROM quay.io/pypa/manylinux_2_28_x86_64@sha256:1e7b8460ad8fb3fb0fbe1cd818845159ac896b4fd17452d56e892c291f3b97a8

ARG PYTHON_VERSION=3.12.15
ARG PYTHON_SHA256=c2c4321961fab0fb999d66e0cecf521c2ab3994c7992873ea99e306c1094fd5a

RUN dnf install -y \
        openssl-devel \
        bzip2-devel \
        libffi-devel \
        zlib-devel \
        readline-devel \
        sqlite-devel \
        xz-devel \
    && dnf clean all \
    && cd /tmp \
    && curl -fsSLO "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
    && echo "${PYTHON_SHA256}  Python-${PYTHON_VERSION}.tar.xz" | sha256sum -c - \
    && tar -xf "Python-${PYTHON_VERSION}.tar.xz" \
    && cd "Python-${PYTHON_VERSION}" \
    && ./configure --prefix=/opt/cpython --enable-shared --with-ensurepip=install \
    && make -j2 \
    && make install \
    && echo /opt/cpython/lib > /etc/ld.so.conf.d/cpython.conf \
    && ldconfig \
    && cd / \
    && rm -rf "/tmp/Python-${PYTHON_VERSION}" "/tmp/Python-${PYTHON_VERSION}.tar.xz"

RUN dnf install -y \
        libX11 \
        libxcb \
        xcb-util \
        xcb-util-cursor \
        xcb-util-image \
        xcb-util-keysyms \
        xcb-util-renderutil \
        xcb-util-wm \
        libxkbcommon \
        libxkbcommon-x11 \
        libXrender \
        libXext \
        libXi \
        libXfixes \
        libXcursor \
        libXrandr \
        libXinerama \
        libXcomposite \
        libXdamage \
        libXtst \
        mesa-libGL \
        alsa-lib \
        pulseaudio-libs \
        dbus-libs \
        fontconfig \
        freetype \
    && dnf clean all

ENV PATH="/opt/cpython/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/cpython/lib"
WORKDIR /io
