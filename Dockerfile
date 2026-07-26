
# --------------------------------------------------------------------------------------------------------------
# CM 解析ツールをビルドするステージ
# KonomiTV 本体とは独立したステージに分離し、各ツールの更新時も参照コミットだけを差し替えられるようにする
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04 AS cm-analyzer-builder

ENV DEBIAN_FRONTEND=noninteractive

ARG DTVINDEX_REF=c2917d6ecfa5125968d1116874ca2544f17f6ad0
ARG CHAPTER_EXE_REF=b0687fa12b4d72b3bf94b71640ca0a847d07dcef
ARG LOGOFRAME_REF=4785553a5b38a7513606e236bab8e50d9c01ad12
ARG JOIN_LOGO_SCP_REF=4107b3e0e1a798287603b76b8d6d734c9c01396a
ARG TARGETARCH
ARG FFMPEG_TAG=autobuild-2026-05-31-13-22
ARG FFMPEG_MAJOR_VERSION=7.1
ARG FFMPEG_VERSION=7.1.4-7-gadcf20da26

# chapter_exe / logoframe のビルドに必要なパッケージをインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git patchelf pkg-config xz-utils && \
    rm -rf /var/lib/apt/lists/*

# KonomiTV の thirdparty/FFmpeg と同一の BtbN shared 版 SDK をビルド時だけ利用する
# 最終イメージでは SDK をコピーせず、既存の thirdparty/FFmpeg 共有ライブラリをそのまま共用する
WORKDIR /opt/ffmpeg-sdk/
RUN case "${TARGETARCH}" in \
        amd64) cm_ffmpeg_arch='linux64' ;; \
        arm64) cm_ffmpeg_arch='linuxarm64' ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    cm_ffmpeg_archive="ffmpeg-n${FFMPEG_VERSION}-${cm_ffmpeg_arch}-gpl-shared-${FFMPEG_MAJOR_VERSION}" && \
    curl -fsSL \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_TAG}/${cm_ffmpeg_archive}.tar.xz" \
        -o /tmp/ffmpeg-sdk.tar.xz && \
    tar -xJf /tmp/ffmpeg-sdk.tar.xz --strip-components=1 && \
    rm -f /tmp/ffmpeg-sdk.tar.xz

ENV PKG_CONFIG_PATH=/opt/ffmpeg-sdk/lib/pkgconfig
ENV LD_LIBRARY_PATH=/opt/ffmpeg-sdk/lib

# 外部ツールは KonomiTV のソースツリーへ取り込まず、固定コミットから再現可能な形でビルドする
COPY ./docker/cm-analyzer/dtvindex-trim-chapters.patch /tmp/dtvindex-trim-chapters.patch
WORKDIR /src/
RUN git clone https://github.com/tobitti0/dtvindex.git dtvindex && \
    git -C dtvindex checkout "${DTVINDEX_REF}" && \
    git -C dtvindex apply --unidiff-zero /tmp/dtvindex-trim-chapters.patch && \
    git clone https://github.com/tobitti0/chapter_exe.git chapter_exe && \
    git -C chapter_exe checkout "${CHAPTER_EXE_REF}" && \
    git clone https://github.com/tobitti0/logoframe.git logoframe && \
    git -C logoframe checkout "${LOGOFRAME_REF}" && \
    git clone https://github.com/yobibi/join_logo_scp.git join_logo_scp && \
    git -C join_logo_scp checkout "${JOIN_LOGO_SCP_REF}"

# AviSynth には依存せず、TS を dtvindex / FFmpeg 経由で直接解析する
RUN make -C /src/dtvindex && \
    make -C /src/chapter_exe/src \
        DTVINDEX_DIR=/src/dtvindex WITH_AVISYNTH=no WITH_DTVINDEX=yes && \
    make -C /src/logoframe/src \
        DTVINDEX_DIR=/src/dtvindex WITH_AVISYNTH=no WITH_DTVINDEX=yes && \
    make -C /src/join_logo_scp/src

# 実行ファイルと解析設定だけを最終イメージへ渡す
RUN install -d /opt/cm-analyzer && \
    install -m 0755 /src/dtvindex/build/dtvindex /opt/cm-analyzer/dtvindex && \
    install -m 0755 /src/chapter_exe/src/chapter_exe /opt/cm-analyzer/chapter_exe && \
    install -m 0755 /src/logoframe/src/logoframe /opt/cm-analyzer/logoframe && \
    install -m 0755 /src/join_logo_scp/src/join_logo_scp /opt/cm-analyzer/join_logo_scp && \
    install -m 0644 /src/join_logo_scp/JL/JL_標準.txt /opt/cm-analyzer/JL_標準.txt && \
    install -m 0644 /src/logoframe/logoframe.ini /opt/cm-analyzer/logoframe.ini && \
    patchelf --force-rpath --set-rpath '$ORIGIN/../FFmpeg' \
        /opt/cm-analyzer/dtvindex \
        /opt/cm-analyzer/chapter_exe \
        /opt/cm-analyzer/logoframe

# --------------------------------------------------------------------------------------------------------------
# サードパーティーライブラリのダウンロードを行うステージ
# Docker のマルチステージビルドを使い、最終的な Docker イメージのサイズを抑え、ビルドキャッシュを効かせる
# --------------------------------------------------------------------------------------------------------------

# 念のため最終イメージに合わせて Ubuntu 22.04 LTS にしておく
## 中間イメージなので、サイズは（ビルドするマシンのディスク容量以外は）気にしなくて良い
FROM ubuntu:22.04 AS thirdparty-downloader

# apt-get に対話的に設定確認されないための設定
ENV DEBIAN_FRONTEND=noninteractive

# ダウンロード・展開に必要なパッケージのインストール
RUN apt-get update && apt-get install -y --no-install-recommends aria2 ca-certificates unzip xz-utils

# サードパーティーライブラリをダウンロード
## サードパーティーライブラリは変更が少ないので、先にダウンロード処理を実行してビルドキャッシュを効かせる
WORKDIR /
## リリース版用
RUN aria2c -x10 https://github.com/tsukumijima/KonomiTV/releases/download/v0.14.1/thirdparty-linux.tar.xz
RUN tar xvf thirdparty-linux.tar.xz
## 開発版 (0.xx.x-dev) 用
# RUN aria2c -x10 https://nightly.link/tsukumijima/KonomiTV/actions/runs/27093421017/thirdparty-linux.tar.xz.zip
# RUN unzip thirdparty-linux.tar.xz.zip && tar xvf thirdparty-linux.tar.xz

# --------------------------------------------------------------------------------------------------------------
# クライアントをビルドするステージ
# クライアントのビルド成果物 (dist) は Git に含まれているが、万が一ビルドし忘れたりや開発ブランチでの利便性を考慮してビルドしておく
# --------------------------------------------------------------------------------------------------------------

FROM node:20.16.0 AS client-builder

# 依存パッケージリスト (package.json/yarn.lock) だけをコピー
WORKDIR /code/client/
COPY ./client/package.json ./client/yarn.lock /code/client/

# 依存パッケージを yarn でインストール
RUN yarn install --frozen-lockfile

# クライアントのソースコードをコピー
COPY ./client/ /code/client/

# クライアントをビルド
# /code/client/dist/ に成果物が作成される
RUN yarn build

# --------------------------------------------------------------------------------------------------------------
# メインのステージ
# ここで作成された実行時イメージが docker compose up -d で起動される
# --------------------------------------------------------------------------------------------------------------

# Ubuntu 22.04 LTS (with CUDA) をベースイメージとして利用
## NVEncC の動作には CUDA ライブラリが必要なため、CUDA 付きのイメージを使う
## RTX 5090 (Blackwell) 世代をサポートする最低バージョンである CUDA 12.8.0 を指定している
## cuda:x.x.x-runtime 系イメージだと NVEncC で使わない余計なライブラリが付属して重いので、base イメージを使う
FROM nvidia/cuda:12.8.0-base-ubuntu22.04

# タイムゾーンを東京に設定
ENV TZ=Asia/Tokyo

# apt-get に対話的に設定を確認されないための設定
ENV DEBIAN_FRONTEND=noninteractive

# サードパーティーライブラリの依存パッケージをインストール
## libfontconfig1, libfreetype6, libfribidi0: フォント関連のライブラリ (なぜ必要だったか忘れたが多分ないと動かない)
## QSVEncC: Intel Media VA Driver (non-free 版), Intel 版 OpenCL が必要
## NVEncC: runtime 版には含まれているが base 版には含まれていない cuda-nvrtc-12-8, libnpp-12-8 をインストールする
## VCEEncC: AMDGPU-PRO Driver (proprietary 版) に含まれる AMD AMF (Advanced Media Framework), AMD 版 OpenCL が必要
## Zendriver: Twitter GraphQL API を叩くために必要な Google Chrome とサイズ小さめの日本語フォントをインストールする
## ref: https://github.com/rigaya/QSVEnc/blob/master/Install.ja.md
## ref: https://github.com/rigaya/VCEEnc/blob/master/Install.ja.md
RUN apt-get update && \
    # リポジトリ追加に必要な最低限のパッケージをインストール
    apt-get install -y --no-install-recommends ca-certificates curl git gpg tzdata && \
    # Intel GPU リポジトリ
    curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key | gpg --yes --dearmor --output /usr/share/keyrings/intel-graphics-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics-keyring.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified' > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    # AMD / ROCm リポジトリ
    curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --yes --dearmor --output /usr/share/keyrings/rocm-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy main' > /etc/apt/sources.list.d/amdgpu.list && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy proprietary' > /etc/apt/sources.list.d/amdgpu-proprietary.list && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/rocm/apt/6.4.4 jammy main' > /etc/apt/sources.list.d/rocm.list && \
    # Google Chrome リポジトリ
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --yes --dearmor --output /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list && \
    # リポジトリを更新し、この時点で利用可能なパッケージをアップグレード
    apt-get update && apt-get upgrade -y && \
    # 必要なパッケージをインストール
    apt-get install -y --no-install-recommends \
        # フォント関連のライブラリ
        libfontconfig1 libfreetype6 libfribidi0 \
        # Intel GPU 関連のライブラリ
        intel-media-va-driver-non-free intel-opencl-icd libigfxcmrt7 libmfx1 libmfxgen1 libva-drm2 libva-x11-2 \
        # NVIDIA GPU 関連のライブラリ
        cuda-nvrtc-12-8 libnpp-12-8 \
        # AMD GPU 関連のライブラリ
        amf-amdgpu-pro libamdenc-amdgpu-pro libdrm2-amdgpu ocl-icd-libopencl1 rocm-opencl-runtime vulkan-amdgpu-pro \
        # Zendriver 用に Google Chrome と日本語フォントをインストール
        google-chrome-stable fonts-vlgothic && \
    # 実行時イメージなので RUN の最後に掃除する
    apt-get -y autoremove && \
    apt-get -y clean && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /tmp/*

# ダウンロードしておいたサードパーティーライブラリをコピー
WORKDIR /code/server/
COPY --from=thirdparty-downloader /thirdparty/ /code/server/thirdparty/

# ビルドしておいた CM 解析ツールをコピー
COPY --from=cm-analyzer-builder /opt/cm-analyzer/ /code/server/thirdparty/CMAnalyzer/

# Poetry の依存パッケージリストだけをコピー
COPY ./server/pyproject.toml ./server/poetry.lock ./server/poetry.toml /code/server/

# 依存パッケージを poetry でインストール
## 仮想環境 (.venv) をプロジェクト直下に作成する
RUN /code/server/thirdparty/Python/bin/python -m poetry env use /code/server/thirdparty/Python/bin/python && \
    /code/server/thirdparty/Python/bin/python -m poetry install --only main --no-root

# サーバーのソースコードをコピー
COPY ./server/ /code/server/

# クライアントのビルド成果物 (dist) だけをコピー
COPY --from=client-builder /code/client/dist/ /code/client/dist/

# config.example.yaml をコピー
COPY ./config.example.yaml /code/config.example.yaml

# KonomiTV サーバーを起動
ENTRYPOINT ["/code/server/.venv/bin/python", "KonomiTV.py"]
