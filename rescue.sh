#!/bin/bash
# seriald 串口自愈脚本 —— 供 seriald 通过 `sudo -n` 调用。
#
# 为什么做成独立脚本 + sudoers 白名单,而不是给 seriald 通用 sudo:
#   web 服务不该持有通用提权能力。白名单只放行这个 USB 重新绑定脚本。
#
# 为什么必须装在 root 拥有的目录(如 /usr/local/sbin):
#   若脚本对调用者(qiu)可写,ta 改一行内容就等于拿到完整 root,sudoers 白名单形同虚设。
#   安装时务必 root:root 0755,且所在目录不可被 qiu 写。
#
# 用法(参数受控,不做任何别的事):
#   seriald-rescue.sh <usb设备名> [rebind]  软拔插(内核层 unbind/bind,设备名可能变)
set -u

DEV="${1:-}"
MODE="${2:-rebind}"

# shell 的 glob 中 * 能吞掉斜杠;旧写法会放行 3-4/../../... 并以 root 写任意路径。
# 必须对整个参数做锚定校验,只接受内核的 USB 设备名(形如 3-4.2.2)。
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || ! [[ "$DEV" =~ ^[0-9]+-[0-9]+(\.[0-9]+)*$ ]]; then
  echo "拒绝:设备名不合法 '$DEV'" >&2
  exit 2
fi
case "$MODE" in rebind) ;; *) echo "拒绝:模式只能是 rebind" >&2; exit 2 ;; esac

D="/sys/bus/usb/devices/$DEV"
[ -d "$D" ] || { echo "不存在:$D" >&2; exit 3; }

rc=0
if echo "$DEV" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null; then
  echo "unbind: ok"
else
  echo "unbind: 失败" >&2; rc=1
fi
sleep 1
if echo "$DEV" > /sys/bus/usb/drivers/usb/bind 2>/dev/null; then
  echo "bind: ok"
else
  echo "bind: 失败" >&2; rc=1
fi
exit $rc
