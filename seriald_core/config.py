import os
import re
import sys

BASE = os.environ.get('SERIALD_BASE', os.path.join(os.path.dirname(sys.executable), 'data')
                      if sys.platform == 'win32' else '/data/seriald')
# 静态根(页面/前端库)与数据根分开 —— 打成 deb 后代码在 /usr/lib/seriald,
# 包安装时通过环境配置到 /data/seriald-data；源码运行时默认留在仓库 /data/seriald。
STATIC = os.environ.get('SERIALD_STATIC') or (getattr(sys, '_MEIPASS', None)
         or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_ROOT = os.path.join(BASE, 'logs')
SOCKET = os.environ.get('SERIALD_SOCKET', os.path.join(BASE, 'seriald.sock'))
HANDOFF = os.environ.get('SERIALD_HANDOFF', os.path.join(BASE, 'collector-handoff.json'))
AUTH_TOKEN = os.environ.get('SERIALD_AUTH_TOKEN', '')
BY_PATH = os.environ.get('SERIALD_BY_PATH', 'Windows COM' if sys.platform == 'win32'
                         else '/dev/serial/by-path')
# 自愈脚本的位置。必须由 root 拥有且调用者不可写,否则 sudoers 白名单等于通用提权。
# 打包后由 dpkg 装到 /usr/libexec/seriald/rescue.sh(天然满足这个前提)。
RESCUE_BIN = os.environ.get('SERIALD_RESCUE_BIN', '/usr/local/sbin/seriald-rescue.sh')

SCROLLBACK = 256 * 1024          # 每设备内存缓冲上限(页面实时视图用)
SSH_LIMIT = 8                   # 防止遗留标签页无限创建 ssh 子进程
SSH_IDLE = 30 * 60
DATE_FMT = '%Y%m%d'

# 从控制台流里嗅探 hostname 的几种常见形态
HOST_PATTERNS = [
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) login:\s*$'),      # getty 提示符
    re.compile(r'\broot@([A-Za-z0-9][A-Za-z0-9._-]{1,62}):'),          # shell 提示符
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) kernel:'),         # 内核日志转发
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) systemd\['),       # systemd 日志转发
    re.compile(r'^Welcome to .*?\(GNU/Linux .*?\)'),                   # 只用于识别"已启动"
]
ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)'
                  r'|\x1b[=>()][A-Za-z0-9]?|\x1bP.*?\x1b\\')
