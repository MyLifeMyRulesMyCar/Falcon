# Step 1 — Rebuild ffmpeg for M0 (HTTPS/TLS + rkmpp)

This document records the ffmpeg rebuild performed to satisfy the M0 ingest requirements on the RK3576 board.

## Goal

Enable ffmpeg to:

1. Open HTTPS/HLS streams (the default test URL and most third-party feeds).
2. Decode H.264/HEVC/VP8/VP9 using the Rockchip MPP hardware decoder (`h264_rkmpp`, etc.).

## Before the rebuild

```bash
$ ffmpeg -hwaccels
Hardware acceleration methods:
drm

$ ffmpeg -i https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8 -t 1 -f null -
https or dtls protocol not found, recompile FFmpeg with openssl, gnutls or securetransport enabled.
```

The installed ffmpeg (8.1.2, built `--enable-rkmpp --enable-libdrm`) had rkmpp *decoders* but no TLS support, so the default HLS test asset could not be opened.

## Rebuild procedure

Source tree used: `/home/radxa/falcon/ffmpeg-src/ffmpeg-8.1.2`

### 1. Install OpenSSL development headers

TLS support requires `libssl-dev`:

```bash
sudo apt update
sudo apt install -y libssl-dev
```

### 2. Configure

```bash
cd /home/radxa/falcon/ffmpeg-src/ffmpeg-8.1.2
./configure \
    --enable-rkmpp \
    --enable-libdrm \
    --enable-openssl \
    --enable-version3 \
    --prefix=/usr/local \
    --disable-doc \
    --disable-debug
```

Log written to: `/home/radxa/falcon/ffmpeg-src/configure-8-openssl.log`

Expected lines in configure output:

```text
External libraries:
iconv                   openssl                 zlib
...
Enabled protocols:
... https                 tls ...
```

### 3. Build

```bash
make -j$(nproc)
```

Log written to: `/home/radxa/falcon/ffmpeg-src/build-8-openssl.log`

### 4. Install

```bash
sudo make install
```

Log written to: `/home/radxa/falcon/ffmpeg-src/install-8-openssl.log`

## After the rebuild

```bash
$ ffmpeg -version | head -2
ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 12 (Debian 12.2.0-14+deb12u1)
configuration: --enable-rkmpp --enable-libdrm --enable-openssl --enable-version3 --prefix=/usr/local --disable-doc --disable-debug

$ ffmpeg -protocols | grep -E "https|tls"
  https
  tls

$ ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height,r_frame_rate \
    -of json https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8
{
    "streams": [
        {
            "width": 1280,
            "height": 720,
            "r_frame_rate": "60/1"
        }
    ]
}
```

## Important caveat: `-hwaccel rkmpp` vs `-hwaccel drm`

Upstream ffmpeg (both 7.1.5 and 8.1.2) **does not register `rkmpp` as a `hwaccel` method**. It only exposes `h264_rkmpp`, `hevc_rkmpp`, `vp8_rkmpp`, and `vp9_rkmpp` as decoders. Therefore:

```bash
ffmpeg -hwaccel rkmpp -i <url> ...
# Unrecognized hwaccel: rkmpp.
```

The correct way to invoke hardware decode on this board with this ffmpeg build is:

```bash
ffmpeg -init_hw_device drm:/dev/dri/renderD128 -hwaccel drm \
    -i https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8 \
    -f rawvideo -pix_fmt bgr24 -vsync 0 pipe:1
```

This uses the DRM hardware device context, auto-selects the rkmpp decoder for the input codec, and lets ffmpeg convert the decoded frames to BGR24 on the CPU for the output pipe.

Verified working:

```bash
timeout 15 ffmpeg -init_hw_device drm:/dev/dri/renderD128 -hwaccel drm \
    -i https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8 \
    -f rawvideo -pix_fmt bgr24 -vsync 0 -frames:v 60 -f null -
# frame=   60 ... speed= 3.1x
```

## Required device nodes

Ensure these exist and the running user has access:

```bash
ls -l /dev/dri/renderD128   # owned by group render
ls -l /dev/mpp_service      # owned by group video
```

The `radxa` user on this board is in both `video` and `render` groups.

## Rollback

If the new build causes problems, the previous `/usr/local/bin/ffmpeg` binary was the same 8.1.2 source built without openssl. To restore it, rebuild from the same source tree without `--enable-openssl` and reinstall, or keep a copy of the old binary before installing:

```bash
sudo cp /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg.bak.$(date +%s)
```
