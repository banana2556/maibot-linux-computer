#!/bin/sh
# 容器主进程：启动虚拟显示器、窗口管理器和 noVNC，之后常驻，由 docker run --init 回收僵尸进程。
rm -f /tmp/.X0-lock
Xvfb :0 -screen 0 "${SCREEN:-1280x800}x24" -nolisten tcp &
until xdpyinfo >/dev/null 2>&1; do sleep 0.2; done
openbox &
# VNC 只监听容器内回环，对外由 websockify 提供；宿主机端口映射只绑定 127.0.0.1。
x11vnc -display :0 -forever -shared -nopw -localhost -rfbport 5900 -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
exec sleep infinity
