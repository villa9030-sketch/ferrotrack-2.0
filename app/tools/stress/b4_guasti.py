# -*- coding: utf-8 -*-
"""Blocco 4: guasti simulati.

a) Lantek finto lento / in errore / irraggiungibile / in timeout durante "Manda a Lantek";
b) server ucciso (come un black-out) a meta' di un import e a meta' di un invio a Lantek:
   deve ripartire, i dati devono essere integri e lo stato chiaro;
c) disegni rovinati o strani: DXF vuoto, spazzatura binaria, DXF enorme, DXF in pollici,
   DWG vero (ODA) e DWG finto;
d) upload interrotto a meta';
e) backup: fatto dall'app (sulla copia), ripristinato in un'ALTRA copia con
   tools/ripristina_backup.py, controllo integrita' e conteggi.
I file di prova si creano nella cartella della prova.
"""
import json
import os
import random
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime

from comune import APP, AUTOCAD, PYTHON, Client

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def _modo(ctx, **m):
    with open(os.path.join(ctx.run_dir, 'lantek_modo.json'), 'w', encoding='utf-8') as f:
        json.dump(m or {'modo': 'normale'}, f)


def _conta_tabelle(db):
    c = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
    try:
        tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
        return {t: c.execute('select count(*) from "%s"' % t).fetchone()[0] for t in tabs}
    finally:
        c.close()


def _integrita(db):
    c = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
    try:
        return c.execute('pragma integrity_check').fetchone()[0]
    finally:
        c.close()


class Guasti:
    def __init__(self, ctx):
        import b2_flussi
        self.ctx, self.reg = ctx, ctx.reg
        self.f = b2_flussi.Flussi(ctx)
        self.dir = os.path.join(ctx.run_dir, 'file_guasti')
        os.makedirs(self.dir, exist_ok=True)

    def ok(self, cond, desc, grav='MEDIO', det='', passi='', chiave=None, prova=None):
        return self.reg.controllo(bool(cond), desc, grav, det, passi, prova, chiave=chiave)

    def nuovi_client(self):
        import b2_flussi
        self.f = b2_flussi.Flussi(self.ctx)

    def riavvia(self, motivo):
        srv = self.ctx.server
        t0 = time.time()
        srv.uccidi()
        try:
            secondi = srv.avvia()
        except Exception as e:
            self.reg.problema('BLOCCANTE', 'Il server non riparte dopo un arresto brusco (%s)' % motivo, str(e),
                              'uccidere il processo del server durante %s e riavviarlo' % motivo,
                              chiave=('riavvio', motivo))
            raise
        self.ok(secondi < 60, 'Il server riparte in meno di un minuto dopo un arresto brusco', 'MEDIO',
                '%.0f s' % secondi, chiave=('riavvio_lento',))
        integ = _integrita(self.ctx.db)
        self.ok(integ == 'ok', 'Database integro dopo l\'arresto brusco (%s)' % motivo, 'BLOCCANTE', integ,
                'uccidere il server durante %s' % motivo, chiave=('integrita', motivo))
        self.reg.campione(rss_mb=srv.rss_mb(), evento='riavvio ' + motivo)
        self.nuovi_client()
        return time.time() - t0

    def ordine_con_pezzi(self):
        for _ in range(4):
            oid, _t = self.f.da_pacchetto()
            if oid:
                self.f.las.post('/api/orders/%s/smistamento' % oid, {'va_tagliato': True})
                c, an = self.f.las.get('/api/orders/%s/laser?fresco=1' % oid, timeout=600)
                inv = (an.get('invio') or {}) if isinstance(an, dict) else {}
                cod = [x if isinstance(x, str) else x.get('codice') for x in inv.get('codici') or []]
                nuovi = [x if isinstance(x, str) else x.get('codice') for x in inv.get('nuovi') or []]
                if cod or nuovi:
                    return oid, {'codici': cod, 'nuovi': nuovi}
        return None, None

    # --------------------------------------------------------------- a) Lantek
    def lantek_guasto(self):
        for modo in ('lento', 'errore', 'irraggiungibile', 'timeout'):
            oid, corpo = self.ordine_con_pezzi()
            if not oid:
                self.reg.nota('Lantek %s: nessun ordine con pezzi da mandare' % modo)
                continue
            _modo(self.ctx, modo=modo, secondi=15)
            risposta = {}
            th = threading.Thread(target=lambda: risposta.update(
                r=self.f.las.post('/api/orders/%s/lantek-invia' % oid, corpo, timeout=300)))
            t0 = time.time()
            th.start()
            # mentre Lantek "pensa", le altre postazioni devono andare
            time.sleep(2)
            t1 = time.perf_counter()
            c, _ = self.f.uff.get('/api/orders?aperti=1')
            ms = (time.perf_counter() - t1) * 1000
            self.ok(c == 200 and ms < 3000, 'Le altre postazioni rispondono mentre Lantek e\' lento/guasto', 'GRAVE',
                    'GET /api/orders?aperti=1 in %.0f ms (Lantek %s)' % (ms, modo), chiave=('lantek_blocca', modo))
            th.join(320)
            c, d = risposta.get('r', (0, None))
            dur = time.time() - t0
            if modo == 'lento':
                self.ok(c == 200, 'Manda a Lantek con Lantek lento (15 s) va a buon fine', 'MEDIO',
                        'HTTP %s in %.0f s: %s' % (c, dur, str(d)[:200]), chiave=('lantek_lento',))
            else:
                self.ok(c in (502, 503, 409) and isinstance(d, dict) and d.get('error'),
                        'Manda a Lantek con Lantek "%s": errore chiaro, non 500' % modo, 'GRAVE',
                        'HTTP %s %s' % (c, str(d)[:300]),
                        'Lantek finto in modo %s, poi Manda a Lantek' % modo, chiave=('lantek_err', modo))
                # lo stato dell'ordine non deve dire "in Lantek"
                c2, lz = self.f.las.get('/api/laser/ordini?ordine=%s' % oid)
                stato = ((lz or {}).get('ordini') or {}).get(oid, {}).get('stato') if isinstance(lz, dict) else None
                self.ok(stato != 'in_lantek', 'Dopo un invio fallito l\'ordine NON risulta in Lantek', 'GRAVE',
                        'stato laser: %s (Lantek %s)' % (stato, modo), chiave=('lantek_stato', modo))
                self.reg.nota('Lantek %s: risposta "%s", stato laser %s' % (modo, (d or {}).get('error') if isinstance(d, dict) else d, stato))
                # riprova a Lantek tornato normale
                _modo(self.ctx, modo='normale')
                c3, an = self.f.las.get('/api/orders/%s/laser?fresco=1' % oid, timeout=600)
                inv = (an.get('invio') or {}) if isinstance(an, dict) else {}
                corpo2 = {'codici': [x if isinstance(x, str) else x.get('codice') for x in inv.get('codici') or []],
                          'nuovi': [x if isinstance(x, str) else x.get('codice') for x in inv.get('nuovi') or []]}
                if corpo2['codici'] or corpo2['nuovi']:
                    c4, d4 = self.f.las.post('/api/orders/%s/lantek-invia' % oid, corpo2, timeout=300)
                    self.ok(c4 == 200, 'Dopo il guasto di Lantek si puo\' rimandare', 'GRAVE',
                            'HTTP %s %s' % (c4, str(d4)[:200]), chiave=('lantek_riprova', modo))
            _modo(self.ctx, modo='normale')
            self.f.audit(oid, ['LANTEK_INVIA'])

    # ------------------------------------------------------ b) server ucciso
    def ucciso_durante_import(self):
        prima = _conta_tabelle(self.ctx.db)
        fonti = sorted(self.f.deca, key=lambda x: -len(x[1]))[:3] or self.f.altri[:3]
        if not fonti:
            return
        cliente, files = fonti[0]
        parti = [('files', os.path.basename(p), open(p, 'rb').read()) for p in files]
        esito = {}
        th = threading.Thread(target=lambda: esito.update(
            r=self.f.uff.multipart('/api/orders/da-pacchetto/analizza', files=parti, timeout=120,
                                   atteso_500_ok=True, registra=False)))
        th.start()
        time.sleep(1.5)
        self.riavvia('import di un pacchetto (%d file)' % len(parti))
        th.join(130)
        dopo = _conta_tabelle(self.ctx.db)
        self.ok(dopo.get('orders') == prima.get('orders'),
                'Un import interrotto da un arresto non lascia ordini a meta\'', 'GRAVE',
                'ordini prima %s, dopo %s' % (prima.get('orders'), dopo.get('orders')), chiave=('import_kill',))
        oid, tipo = self.f.da_pacchetto()
        self.ok(oid, 'Dopo il riavvio si importa di nuovo un pacchetto', 'GRAVE', '', chiave=('import_dopo',))

    def ucciso_durante_lantek(self):
        oid, corpo = self.ordine_con_pezzi()
        if not oid:
            self.reg.nota('arresto durante Lantek: nessun ordine con pezzi')
            return
        _modo(self.ctx, modo='lento', secondi=25)
        th = threading.Thread(target=lambda: self.f.las.post('/api/orders/%s/lantek-invia' % oid, corpo,
                                                             timeout=60, registra=False))
        th.start()
        time.sleep(4)
        _modo(self.ctx, modo='normale')
        self.riavvia('invio a Lantek')
        th.join(70)
        c, o = self.f.uff.get('/api/orders/%s' % oid)
        self.ok(c == 200, 'Ordine leggibile dopo un arresto durante l\'invio a Lantek', 'BLOCCANTE', 'HTTP %s' % c,
                chiave=('lantek_kill_leggi',))
        c, lz = self.f.las.get('/api/laser/ordini?ordine=%s&fresco=1' % oid)
        st = ((lz or {}).get('ordini') or {}).get(oid, {}) if isinstance(lz, dict) else {}
        self.reg.nota('dopo arresto durante invio a Lantek lo stato laser e\': %s (n_invio %s)'
                      % (st.get('stato'), st.get('n_invio')))
        c, an = self.f.las.get('/api/orders/%s/laser?fresco=1' % oid, timeout=600)
        inv = (an.get('invio') or {}) if isinstance(an, dict) else {}
        corpo2 = {'codici': [x if isinstance(x, str) else x.get('codice') for x in inv.get('codici') or []],
                  'nuovi': [x if isinstance(x, str) else x.get('codice') for x in inv.get('nuovi') or []]}
        if corpo2['codici'] or corpo2['nuovi']:
            c, d = self.f.las.post('/api/orders/%s/lantek-invia' % oid, corpo2, timeout=300)
            self.ok(c == 200, 'Dopo l\'arresto l\'invio a Lantek si puo\' rifare', 'GRAVE',
                    'HTTP %s %s' % (c, str(d)[:200]), chiave=('lantek_kill_rifai',))

    # ----------------------------------------------------- c) disegni strani
    def disegni_strani(self):
        import ezdxf
        casi = {}
        casi['vuoto.dxf'] = b''
        casi['spazzatura.dxf'] = os.urandom(200000)
        casi['testo_a_caso.dxf'] = ('0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\nabc\n' * 50).encode()
        # DXF in pollici
        doc = ezdxf.new('R2018')
        doc.header['$INSUNITS'] = 1
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True)
        msp.add_circle((1, 1), 0.25)
        p = os.path.join(self.dir, 'pollici.dxf')
        doc.saveas(p)
        casi['pollici.dxf'] = open(p, 'rb').read()
        # DXF enorme (~ decine di MB)
        n = 20000 if self.ctx.profilo == 'notte' else 2000
        doc = ezdxf.new('R2018')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3000, 0), (3000, 1500), (0, 1500)], close=True)
        rnd = random.Random(1)
        for i in range(n):
            x, y = rnd.uniform(10, 2990), rnd.uniform(10, 1490)
            msp.add_circle((x, y), rnd.uniform(1, 4))
        p = os.path.join(self.dir, 'enorme.dxf')
        doc.saveas(p)
        casi['enorme.dxf'] = open(p, 'rb').read()
        casi['finto.dwg'] = b'AC1032' + os.urandom(50000)
        # un DWG vero (ODA), sola lettura dall'archivio
        try:
            for radice, _, files in os.walk(os.path.join(AUTOCAD, 'TAGLIO')):
                dwg = [f for f in files if f.lower().endswith('.dwg') and os.path.getsize(os.path.join(radice, f)) < 3e6]
                if dwg:
                    pp = os.path.join(radice, dwg[0])
                    with open(pp, 'rb') as fh:
                        casi['vero_' + dwg[0]] = fh.read()
                    break
        except OSError:
            pass
        bloccato = False
        for nome, dati in casi.items():
            if datetime.now() > self.ctx.scadenza and nome != 'enorme.dxf':
                self.reg.nota('disegni strani: tempo finito prima di %s' % nome)
                continue
            t0 = time.time()
            parti = [('files', nome, dati)]
            if self.f.pdf:
                pdf = self.f.pdf[0]
                parti.append(('files', os.path.basename(pdf).split('_', 1)[-1], open(pdf, 'rb').read()))
            c, d = self.f.uff.multipart('/api/orders/da-pacchetto/analizza', files=parti, timeout=300)
            dur = time.time() - t0
            if c == 0:
                bloccato = True
            self.ok(c < 500, 'Disegno strano "%s": niente errore 500' % nome, 'GRAVE',
                    'HTTP %s %s' % (c, str(d)[:300]), 'caricare un pacchetto con il solo file %s' % nome,
                    chiave=('strano', nome))
            self.ok(dur < 280, 'Disegno strano "%s": nessuna risposta in 5 minuti (il server resta occupato)' % nome,
                    'GRAVE',
                    '%.0f s' % dur, chiave=('strano_lento', nome))
            if isinstance(d, dict):
                self.reg.nota('%s: HTTP %s, %s, %.1f s, righe %s' % (nome, c, d.get('codice') or d.get('error') or 'ok',
                                                                     dur, len(d.get('righe') or [])))
            c2, _ = self.f.uff.get('/api/health')
            self.ok(c2 == 200, 'Server vivo dopo il disegno "%s"' % nome, 'BLOCCANTE', 'HTTP %s' % c2,
                    chiave=('strano_vivo', nome))
            if c2 != 200 and not self.ctx.server.vivo():
                self.riavvia('dopo disegno %s' % nome)
        if bloccato:
            # il lavoro rimasto appeso consumerebbe CPU tutta la notte: si riparte puliti
            self.reg.nota("disegni strani: un'analisi non ha finito, riavvio del server per liberare la CPU")
            self.riavvia('dopo un disegno che non finisce')

    # ------------------------------------------------------ d) upload a meta'
    def upload_interrotto(self):
        porta = self.ctx.server.porta
        confine = 'ftstress' + uuid.uuid4().hex
        corpo_tot = 5_000_000
        for frazione in (0.1, 0.5, 0.95):
            try:
                s = socket.create_connection(('127.0.0.1', porta), timeout=10)
                testa = ('POST /api/orders/da-pacchetto/analizza HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                         'Content-Type: multipart/form-data; boundary=%s\r\nContent-Length: %d\r\n'
                         'Cookie: %s\r\n\r\n' % (porta, confine, corpo_tot,
                                                 '; '.join('%s=%s' % (k.name, k.value) for k in self.f.uff.jar)))
                s.sendall(testa.encode())
                s.sendall(('--%s\r\nContent-Disposition: form-data; name="files"; filename="x.dxf"\r\n'
                           'Content-Type: application/octet-stream\r\n\r\n' % confine).encode())
                s.sendall(b'0' * int(corpo_tot * frazione))
                s.close()
            except Exception as e:
                self.reg.nota('upload interrotto %.0f%%: %s' % (frazione * 100, e))
            time.sleep(1)
            c, _ = self.f.uff.get('/api/health')
            self.ok(c == 200, 'Server vivo dopo un upload interrotto al %d%%' % (frazione * 100), 'BLOCCANTE',
                    'HTTP %s' % c, chiave=('upload_int', frazione))
        oid, _ = self.f.da_pdf()
        self.ok(oid, 'Dopo gli upload interrotti un nuovo ordine si carica', 'GRAVE', '', chiave=('upload_dopo',))

    # ------------------------------------------------------------- e) backup
    def backup(self):
        adm = self.f.adm
        adm.eleva()
        conta = _conta_tabelle(self.ctx.db)
        c, d = adm.post('/api/admin/backup', {})
        if not self.ok(c == 200 and d.get('success'), 'Backup dalla pagina admin (sulla copia)', 'GRAVE',
                       'HTTP %s %s' % (c, str(d)[:300]), 'POST /api/admin/backup', chiave=('backup',)):
            return
        bdir = os.path.join(self.ctx.run_dir, 'backup_app')
        trovati = [os.path.join(r, f) for r, _, fs in os.walk(bdir) for f in fs if f.endswith('.db')]
        self.ok(trovati, 'Il backup e\' finito nella cartella della prova', 'BLOCCANTE',
                'nessun .db in %s (backup_path %s)' % (bdir, d.get('backup_path')), chiave=('backup_dove',))
        if not trovati:
            return
        ripr = os.path.join(self.ctx.run_dir, 'ripristino')
        os.makedirs(ripr, exist_ok=True)
        dest = os.path.join(ripr, 'scheduler.db')
        sqlite3.connect(dest).close()
        nome = os.path.basename(max(trovati, key=os.path.getmtime))
        env = dict(os.environ, PYTHONIOENCODING='utf-8', TEMP=os.path.join(self.ctx.run_dir, 'tmp'),
                   TMP=os.path.join(self.ctx.run_dir, 'tmp'), FERROTRACK_DB=dest)
        r = subprocess.run([PYTHON, os.path.join(APP, 'tools', 'ripristina_backup.py'), nome, '--db', dest,
                            '--backup-dir', bdir, '--porta', '0', '--si'], capture_output=True, text=True,
                           errors='replace', cwd=APP, env=env, timeout=600,
                           creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
        self.reg.nota('ripristino: uscita %s %s' % (r.returncode, (r.stdout + r.stderr)[-300:].replace('\n', ' ')))
        if not self.ok(r.returncode == 0, 'Ripristino del backup in un\'altra copia', 'GRAVE',
                       (r.stdout + r.stderr)[-500:], 'tools/ripristina_backup.py <file> --db <copia> --backup-dir ...',
                       chiave=('ripristino',)):
            return
        integ = _integrita(dest)
        self.ok(integ == 'ok', 'Il database ripristinato e\' integro', 'BLOCCANTE', integ, chiave=('ripr_integ',))
        dopo = _conta_tabelle(dest)
        diversi = {t: (n, dopo.get(t)) for t, n in conta.items() if dopo.get(t) != n and t not in ('audit_log',)}
        self.ok(not diversi, 'Il database ripristinato ha gli stessi conteggi della copia al momento del backup',
                'GRAVE', str(diversi)[:400], chiave=('ripr_conta',))


def esegui(ctx):
    g = Guasti(ctx)
    passi = [g.backup, g.upload_interrotto, g.ucciso_durante_import, g.ucciso_durante_lantek, g.lantek_guasto,
             g.disegni_strani]
    for p in passi:
        if datetime.now() > ctx.scadenza:
            ctx.reg.nota('guasti: tempo finito prima di %s' % p.__name__)
            continue
        try:
            p()
        except Exception as e:
            ctx.reg.eccezione('b4 %s' % p.__name__, e)
            _modo(ctx, modo='normale')
            if not ctx.server.vivo():
                try:
                    ctx.server.uccidi()
                    ctx.server.avvia()
                    g.nuovi_client()
                except Exception as e2:
                    ctx.reg.eccezione('b4 riavvio', e2)
    _modo(ctx, modo='normale')
