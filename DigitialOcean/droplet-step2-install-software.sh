#!/bin/bash

set -e

cd ~

# Clone cheap-sports-multicam so we have the generate-ffmpeg.py script.
git clone https://github.com/bicknell/cheap-sports-multicam.git

# Install this as a separate step so we can use it in parallel.
apt-get install -y s3cmd

# Compile lastest ffmpeg with libx265.
# From https://trac.ffmpeg.org/wiki/CompilationGuide/Ubuntu
apt-get update -qq && sudo apt-get -y install \
  autoconf \
  automake \
  build-essential \
  cmake \
  git-core \
  libass-dev \
  libfreetype6-dev \
  libgnutls28-dev \
  libmp3lame-dev \
  libsdl2-dev \
  libtool \
  libva-dev \
  libvdpau-dev \
  libvorbis-dev \
  libxcb1-dev \
  libxcb-shm0-dev \
  libxcb-xfixes0-dev \
  meson \
  ninja-build \
  pkg-config \
  texinfo \
  wget \
  yasm \
  zlib1g-dev

apt install -y libunistring-dev libaom-dev libdav1d-dev

apt install -y nasm libx264-dev libx265-dev libnuma-dev libfdk-aac-dev

mkdir -p ~/ffmpeg_sources ~/bin
cd ~/ffmpeg_sources && \
wget -O ffmpeg-snapshot.tar.bz2 https://ffmpeg.org/releases/ffmpeg-snapshot.tar.bz2 && \
tar xjvf ffmpeg-snapshot.tar.bz2 && \
cd ffmpeg && \
PATH="$HOME/bin:$PATH" PKG_CONFIG_PATH="$HOME/ffmpeg_build/lib/pkgconfig" ./configure \
  --prefix="$HOME/ffmpeg_build" \
  --pkg-config-flags="--static" \
  --extra-cflags="-I$HOME/ffmpeg_build/include" \
  --extra-ldflags="-L$HOME/ffmpeg_build/lib" \
  --extra-libs="-lpthread -lm" \
  --ld="g++" \
  --bindir="$HOME/bin" \
  --enable-gpl \
  --enable-gnutls \
  --enable-libfdk-aac \
  --enable-libfreetype \
  --enable-libx264 \
  --enable-libx265 \
  --enable-nonfree && \
PATH="$HOME/bin:$PATH" make -j $(nproc) && \
make install

echo "Make sure to set PATH=\"\$HOME/bin:\$PATH\""
