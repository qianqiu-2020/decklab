"""Serial archive writer; owns text state but never opens or reads a UART."""
import codecs
import os
import re
import threading
import time

from .config import ANSI, DATE_FMT


class SerialLog:
    def __init__(self, directory, opener=None):
        self.directory = directory
        self.opener = opener if opener is not None else self.open
        self.log_lock = threading.RLock()
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.log_pending = ''
        self.logf = None
        self.logpath = None
        self.log_error = ''

    def open(self):
        with self.log_lock:
            path = os.path.join(self.directory(), 'serial-%s.log' % time.strftime(DATE_FMT))
            if self.logf and self.logpath == path:
                return
            if self.logf:
                self.logf.close()
                self.logf = None
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath = path
            self.log_error = ''

    def text(self, text):
        with self.log_lock:
            try:
                self.opener()
                stamp = time.strftime('%Y-%m-%d %H:%M:%S')
                for raw in text.split('\n'):
                    line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                    if line:
                        # File-transfer shell echo and SD_DATA responses can contain
                        # whole file chunks. Keep the action visible, not the payload.
                        line = re.sub(r'(printf %s )[A-Za-z0-9+/=]{80,}( \| (?:base64 -d))',
                                      r'\1[base64 omitted]\2', line)
                        line = re.sub(r'(SD_DATA=)[A-Za-z0-9+/=]{80,}',
                                      r'\1[base64 omitted]', line)
                        self.logf.write('%s  %s\n' % (stamp, line))
            except (OSError, ValueError) as error:
                # 磁盘满/目录不可写不能杀死唯一采集线程,内存扇出仍要继续。
                self.log_error = str(error)
                if self.logf:
                    try:
                        self.logf.close()
                    except OSError:
                        pass
                self.logf = None

    def consume(self, data, received=True):
        with self.log_lock:
            if not received:
                self.text(data.decode('utf-8', 'replace'))
                return None
            text = self.decoder.decode(data)
            self.log_pending += text
            lines = self.log_pending.split('\n')
            self.log_pending = lines.pop()
            if lines:
                self.text('\n'.join(lines))
            if len(self.log_pending) > 8192:
                self.text(self.log_pending)
                self.log_pending = ''
            return text

    def close(self):
        with self.log_lock:
            self.log_pending += self.decoder.decode(b'', final=True)
            if self.log_pending:
                self.text(self.log_pending)
            self.log_pending = ''
            self.decoder.reset()
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None

