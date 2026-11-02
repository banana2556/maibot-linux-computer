FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 DISPLAY=:0

# 桌面（Xvfb + openbox）、输入与截图（xdotool + Pillow）、浏览器（Chromium + CDP）、人工查看（noVNC）
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils openbox xdotool xterm \
        python3 python3-pil python3-websocket \
        chromium fonts-noto-cjk \
        x11vnc novnc websockify \
        ca-certificates curl wget git sudo procps less nano unzip \
    && rm -rf /var/lib/apt/lists/*

COPY browser entrypoint.sh /usr/local/bin/
COPY guest.py /opt/mai/guest.py
RUN chmod +x /usr/local/bin/browser /usr/local/bin/entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
