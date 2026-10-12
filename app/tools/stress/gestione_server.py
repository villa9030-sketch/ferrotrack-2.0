# -*- coding: utf-8 -*-
"""Avvio, arresto, uccisione e misure del server di prova (porte 5060-5069)."""
import json
import os
import socket
import subprocess
import time
import urllib.request

from comune import PYTHON, QUI, APP

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def porta_libera(porta):
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1', porta)) != 0


class Server:
    def __init__(self, porta, run_dir, reg, threads=16):
        assert 5060 <= porta <= 5069
        self.porta = porta
        self.run_dir = run_dir
        self.reg = reg
        self.threads = threads
        self.proc = None
        self.base = 'http://127.0.0.1:%d' % porta
        self.avvii = 0

    def avvia(self, attesa=180):
        if not porta_libera(self.porta):
            raise RuntimeError('porta %d occupata' % self.porta)
        self.avvii += 1
        log = open(os.path.join(self.run_dir, 'server_%d_%d.log' % (self.porta, self.avvii)), 'w',
                   encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        tmp = os.path.join(self.run_dir, 'tmp')
        os.makedirs(tmp, exist_ok=True)
        env['TEMP'] = env['TMP'] = tmp
        self.proc = subprocess.Popen(
            [PYTHON, os.path.join(QUI, 'server_stress.py'), '--porta', str(self.porta), '--run', self.run_dir,
             '--threads', str(self.threads)],
            cwd=APP, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
        log.close()
        t0 = time.time()
        while time.time() - t0 < attesa:
            if self.proc.poll() is not None:
                raise RuntimeError('il server di prova si e\' fermato all\'avvio (vedi server_%d_%d.log)'
                                   % (self.porta, self.avvii))
            try:
                with urllib.request.urlopen(self.base + '/api/health', timeout=3) as r:
                    d = json.load(r)
                if d.get('istanza_di_prova') is not True:
                    self.uccidi()
                    raise SystemExit('ABORT: /api/health non dice istanza_di_prova: true')
                self.reg.log('server %d pronto in %.1f s' % (self.porta, time.time() - t0))
                return time.time() - t0
            except SystemExit:
                raise
            except Exception:
                time.sleep(0.5)
        raise RuntimeError('il server di prova non risponde dopo %d s' % attesa)

    def vivo(self):
        return self.proc is not None and self.proc.poll() is None

    def _processi(self):
        """Il lanciatore del venv e il vero python figlio."""
        import psutil
        p = psutil.Process(self.proc.pid)
        return [p] + p.children(recursive=True)

    def rss_mb(self):
        try:
            return sum(x.memory_info().rss for x in self._processi()) / 1e6
        except Exception:
            return None

    def cpu_s(self):
        try:
            tot = 0
            for x in self._processi():
                t = x.cpu_times()
                tot += t.user + t.system
            return tot
        except Exception:
            return None

    def uccidi(self):
        """Come un'interruzione di corrente: niente chiusura ordinata."""
        if self.proc and self.proc.poll() is None:
            try:
                for x in reversed(self._processi()):
                    try:
                        x.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            self.proc.kill()
            try:
                self.proc.wait(20)
            except Exception:
                pass
        self.proc = None
        for _ in range(40):
            if porta_libera(self.porta):
                break
            time.sleep(0.25)

    ferma = uccidi
