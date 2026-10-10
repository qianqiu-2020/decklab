"""Serial descriptor IO and bounded USB recovery."""
import errno
import os
import re
import select
import subprocess
try:
    import termios
except ImportError:  # Windows uses the single pySerial COM handle in WindowsManager.
    termios = None
import time

from .config import RESCUE_BIN

def open_serial(dev, flush=False, identity=None):
    """115200 8N1,无流控,raw,不回显。只有采集器调这个(唯一读者)。

    默认保留内核里已经排队的字节;重连时 flush 会截断板子的启动现场。
    """
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if identity is not None and (s.st_dev, s.st_ino, s.st_rdev) != identity:
            raise OSError(errno.ENODEV, '串口已重新枚举')
        a = termios.tcgetattr(fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = termios.B115200
        a[5] = termios.B115200
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        # Manager 在旧采集器彻底退出后才允许进入这里;运行中的读者绝不重新配置。
        termios.tcsetattr(fd, termios.TCSANOW, a)
        if flush:
            termios.tcflush(fd, termios.TCIFLUSH)
        return fd
    except BaseException:
        os.close(fd)                     # tcgetattr/tcsetattr 失败也必须归还 fd
        raise


def write_all(fd, data, timeout=5.0, cancel=None):
    """非阻塞 fd 也会短写;串行补齐且有截止时间,不持有输出缓冲锁。"""
    pending = memoryview(data)
    deadline = time.monotonic() + timeout
    while pending:
        if cancel is not None and cancel.is_set():
            raise OSError(errno.ECANCELED, '会话已停止')
        left = deadline - time.monotonic()
        if left <= 0:
            raise OSError(errno.ETIMEDOUT, '写入超时,可能已发送部分内容')
        try:
            n = os.write(fd, pending)
            if n <= 0:
                raise OSError(errno.EIO, '写入未取得进展')
            pending = pending[n:]
        except InterruptedError:
            continue
        except BlockingIOError:
            try:
                select.select([], [fd], [], min(left, 0.1))
            except InterruptedError:
                continue


def write_serial(dev, data, identity=None, cancel=None):
    """铁律 2/3:只写不读、不碰 termios。"""
    try:
        fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        return False, str(e)
    try:
        if identity is not None:
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino, s.st_rdev) != identity:
                raise OSError(errno.ENODEV, '串口已重新枚举,拒绝写入')
        write_all(fd, data.encode('utf-8', 'replace'), cancel=cancel)
        return True, ''
    except OSError as e:
        return False, str(e)
    finally:
        os.close(fd)


def usb_path_of(dev):
    """从 /dev/ttyUSBx 反查它挂在哪个 USB 设备上(如 3-4.2.2)。

    注意要取**最深**那一层:路径里有集线器各级(usb3/3-4/3-4.2/3-4.2.2/3-4.2.2:1.0),
    匹配到第一个会得到集线器 3-4 —— 对它做 unbind 等于拔掉整个集线器。
    """
    try:
        p = os.path.realpath('/sys/class/tty/%s/device' % os.path.basename(dev))
    except OSError:
        return None
    hits = re.findall(r'/(\d+-[\d.]+)(?::[\d.]+)?/', p + '/')
    return hits[-1] if hits else None


def try_rescue(dev, mode='rebind'):
    """通过 USB unbind/bind 重新接入串口。

    执行路径:
      * seriald 以 root 跑  -> 直接写 /sys(最简,无需任何配置)
      * 否则               -> sudo -n 调白名单脚本(需用户一次性配置 sudoers);
                              -n 保证不会卡在密码提示,sudoers 没配就直接失败并如实上报
    白名单脚本必须由 root 拥有且不可被调用者改写,否则等于通用提权。
    """
    if os.name == 'nt':
        return False, 'Windows 不支持 Linux USB 自愈'
    name = usb_path_of(dev)
    if not name:
        return False, '找不到 %s 对应的 USB 设备路径' % dev
    if mode != 'rebind' or not re.fullmatch(r'[0-9]+-[0-9]+(?:\.[0-9]+)*', name):
        return False, 'USB 路径或自愈模式不合法'
    steps = []
    failed = False

    if os.geteuid() != 0:
        if not os.path.exists(RESCUE_BIN):
            return False, '自愈脚本未安装(%s)，请检查软件包或服务配置' % RESCUE_BIN
        try:
            p = subprocess.run(['sudo', '-n', RESCUE_BIN, name, mode],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return False, '调用自愈脚本失败: %s' % e
        out = (p.stdout + ' ' + p.stderr).strip().replace('\n', '; ')
        ok = p.returncode == 0
        if not ok and 'password' in out.lower():
            out = 'sudo 需要密码(白名单未配置): ' + out
        return ok, '%s [%s]' % (out or '无输出', name)

    # root 直连:不依赖脚本
    for act in ('unbind', 'bind'):
        if act == 'bind':
            time.sleep(1)
        try:
            with open('/sys/bus/usb/drivers/usb/%s' % act, 'w') as f:
                f.write(name)
            steps.append(act)
        except OSError as e:
            failed = True
            steps.append('%s 失败: %s' % (act, e.strerror or e))
    # 一步成功不代表整次 rebind 成功;与特权脚本的退出码语义一致。
    ok = not failed
    return ok, '%s [%s]' % ('; '.join(steps), name)
