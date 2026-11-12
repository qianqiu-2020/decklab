import os
import re
import sys

from .web_settings import WebSettings, load_environment

# Keep the launch environment and configuration anchor stable across restarts,
# even when the Web configuration selects a different runtime data directory.
LAUNCH_ENVIRONMENT = dict(os.environ)
_launch_base = os.environ.get('SERIALD_BASE', os.path.join(os.path.dirname(sys.executable), 'data')
                              if sys.platform == 'win32' else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS_PATH = os.path.join(_launch_base, 'web-settings.json')
SETTINGS = WebSettings(SETTINGS_PATH, LAUNCH_ENVIRONMENT)
load_environment(SETTINGS_PATH)


def read_ui_language(base):
    """Installer default; browser preferences override this host-wide hint."""
    preferred = os.environ.get('SERIALD_UI_LANGUAGE', 'auto')
    if preferred in ('en', 'zh-CN'):
        return preferred
    try:
        with open(os.path.join(base, 'ui-language.txt'), encoding='utf-8-sig') as source:
            value = source.read(32).strip()
        return value if value in ('en', 'zh-CN') else ''
    except (OSError, UnicodeError):
        return ''

BASE = os.environ.get('SERIALD_BASE', os.path.join(os.path.dirname(sys.executable), 'data')
                      if sys.platform == 'win32' else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 静态根(页面/前端库)与数据根分开 —— 打成 deb 后代码在 /usr/lib/seriald,
# 包安装时通过环境配置到 /data/seriald-data；源码运行时默认留在当前仓库根目录，不依赖固定的检出位置。
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

def _integer_setting(name, default, minimum, maximum):
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r'[0-9]+', value) or not minimum <= int(value) <= maximum:
        raise ValueError('invalid setting: ' + name)
    return int(value)


SCROLLBACK = _integer_setting('SERIALD_SCROLLBACK', 256 * 1024, 4096, 16 * 1024 ** 2)
SSH_LIMIT = _integer_setting('SERIALD_SSH_LIMIT', 8, 1, 128)
SSH_IDLE = _integer_setting('SERIALD_SSH_IDLE', 1800, 60, 86400)
SHARE_SECONDS = _integer_setting('SERIALD_SHARE_SECONDS', 86400, 60, 30 * 86400)
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
