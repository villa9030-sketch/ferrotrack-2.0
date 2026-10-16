# -*- coding: utf-8 -*-
"""Pezzi comuni della prova notturna: percorsi, client HTTP misurato,
registro dei risultati e dei problemi.

Tutto quello che si scrive va SOLO nella cartella della prova
(banco_motore/stress/<data_ora>/) o dentro ferrotrack-stress/.
"""
import http.cookiejar
import json
import os
import random
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime

QUI = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(QUI))                 # ferrotrack-stress/app
WORKTREE = os.path.dirname(APP)                              # ferrotrack-stress
RADICE = os.path.dirname(WORKTREE)                           # schedulatore-laser-stefano-sviluppo
PRODUZIONE_APP = os.path.join(RADICE, 'ferrotrack-2.0', 'app')
PROD_DB = os.path.join(PRODUZIONE_APP, 'database', 'scheduler.db')
PROD_UPLOADS = os.path.join(PRODUZIONE_APP, 'uploads')
PROD_CONFIG = os.path.join(PRODUZIONE_APP, 'app_config.json')
PROD_DXF_CACHE = os.path.join(PRODUZIONE_APP, 'database', 'dxf_cache.db')
STRESS_UPLOADS = os.path.join(APP, 'uploads')
CARTELLA_RUNS = os.path.join(RADICE, 'banco_motore', 'stress')
PYTHON = os.path.join(PRODUZIONE_APP, '.venv', 'Scripts', 'python.exe')
AUTOCAD = os.path.join(os.path.expanduser('~'), 'Desktop', 'AUTOCAD')
DISEGNI_LANTEK = os.path.join(os.path.expanduser('~'), 'Desktop', 'DISEGNI LANTEK')
ODA_DIR = r'C:\Program Files\ODA\ODAFileConverter 27.9.0'

PORTE = list(range(5060, 5070))
PIN_ADMIN = os.environ.get('FT_PIN_ADMIN') or '5807'
os.environ['FT_PIN_ADMIN'] = PIN_ADMIN

if APP not in sys.path:
    sys.path.insert(0, APP)

GRAVITA = ('BLOCCANTE', 'GRAVE', 'MEDIO', 'LIEVE')


def priorita_bassa(pid=None):
    """Processo a priorita' BELOW_NORMAL (come tools/banco_lantek.py)."""
    try:
        import psutil
        p = psutil.Process(pid or os.getpid())
        if os.name == 'nt':
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:
        pass


def ora():
    return datetime.now().strftime('%H:%M:%S')


# ---------------------------------------------------------------------------
# Registro: risultati, problemi, misure
# ---------------------------------------------------------------------------
class Registro:
    def __init__(self, cartella):
        self.cartella = cartella
        self.lock = threading.Lock()
        self.blocchi = {}          # nome -> {'stato','inizio','fine','controlli_ok','controlli_ko','note'}
        self.problemi = []         # dict
        self._chiavi_problemi = {}  # dedup: chiave -> indice
        self.tempi = {}            # endpoint (metodo + percorso normalizzato) -> [ms]
        self.codici = {}           # endpoint -> {codice: n}
        self.serie = []            # [{t, rss_mb, db_mb, ...}]
        self.log_f = open(os.path.join(cartella, 'harness.log'), 'a', encoding='utf-8')
        self.blocco_attuale = None
        os.makedirs(os.path.join(cartella, 'prove'), exist_ok=True)

    def log(self, *parti):
        riga = '[%s] %s' % (ora(), ' '.join(str(p) for p in parti))
        with self.lock:
            self.log_f.write(riga + '\n')
            self.log_f.flush()
        try:
            print(riga, flush=True)
        except Exception:
            pass

    # --- blocchi
    def inizio_blocco(self, nome):
        with self.lock:
            self.blocchi[nome] = {'stato': 'in corso', 'inizio': datetime.now().isoformat(timespec='seconds'),
                                  'fine': None, 'ok': 0, 'ko': 0, 'note': []}
            self.blocco_attuale = nome
        self.log('=== BLOCCO', nome, 'inizio')

    def fine_blocco(self, nome, stato=None, nota=None):
        with self.lock:
            b = self.blocchi.setdefault(nome, {'ok': 0, 'ko': 0, 'note': [], 'inizio': None})
            b['fine'] = datetime.now().isoformat(timespec='seconds')
            if stato:
                b['stato'] = stato
            elif b.get('stato') == 'in corso':
                b['stato'] = 'completato'
            if nota:
                b['note'].append(nota)
        self.log('=== BLOCCO', nome, 'fine:', self.blocchi[nome]['stato'], nota or '')
        self.salva()

    def nota(self, testo, blocco=None):
        with self.lock:
            b = self.blocchi.get(blocco or self.blocco_attuale)
            if b is not None:
                b['note'].append(testo)
        self.log('NOTA', testo)

    def controllo(self, ok, descrizione, gravita='MEDIO', dettagli='', passi='', prova=None, blocco=None,
                  chiave=None):
        """Un controllo: se fallisce diventa un problema (con dedup per chiave)."""
        blocco = blocco or self.blocco_attuale
        with self.lock:
            b = self.blocchi.get(blocco)
            if b is not None:
                b['ok' if ok else 'ko'] += 1
        if not ok:
            self.problema(gravita, descrizione, dettagli, passi, prova, blocco, chiave)
        return ok

    def problema(self, gravita, descrizione, dettagli='', passi='', prova=None, blocco=None, chiave=None):
        blocco = blocco or self.blocco_attuale
        chiave = chiave or (blocco, descrizione)
        file_prova = None
        if prova is not None:
            file_prova = os.path.join('prove', 'p_%s.json' % uuid.uuid4().hex[:10])
            try:
                with open(os.path.join(self.cartella, file_prova), 'w', encoding='utf-8') as f:
                    json.dump(prova, f, ensure_ascii=False, indent=1, default=str)
            except Exception:
                file_prova = None
        with self.lock:
            if chiave in self._chiavi_problemi:
                p = self.problemi[self._chiavi_problemi[chiave]]
                p['volte'] += 1
                if len(p['esempi']) < 5 and dettagli:
                    p['esempi'].append(str(dettagli)[:400])
                return
            self._chiavi_problemi[chiave] = len(self.problemi)
            self.problemi.append({'gravita': gravita, 'blocco': blocco, 'descrizione': descrizione,
                                  'dettagli': str(dettagli)[:1500], 'passi': passi, 'prova': file_prova,
                                  'volte': 1, 'esempi': [], 'quando': datetime.now().isoformat(timespec='seconds')})
        self.log('PROBLEMA [%s]' % gravita, descrizione, '|', str(dettagli)[:300])

    def eccezione(self, dove, e, gravita='MEDIO', harness=True):
        """Un'eccezione nel banco stesso (non nell'app)."""
        tb = traceback.format_exc()
        if harness:
            self.problema('LIEVE', 'Errore del banco di prova (non dell\'app) in %s' % dove,
                          '%s: %s' % (type(e).__name__, e), prova={'traceback': tb},
                          chiave=('harness', dove, type(e).__name__))
        else:
            self.problema(gravita, dove, '%s: %s' % (type(e).__name__, e), prova={'traceback': tb})

    # --- misure
    def misura(self, chiave, ms, codice):
        with self.lock:
            self.tempi.setdefault(chiave, []).append(ms)
            c = self.codici.setdefault(chiave, {})
            c[codice] = c.get(codice, 0) + 1

    def campione(self, **dati):
        dati['t'] = datetime.now().isoformat(timespec='seconds')
        with self.lock:
            self.serie.append(dati)

    def salva(self):
        try:
            with self.lock:
                dati = {'blocchi': self.blocchi, 'problemi': self.problemi,
                        'prestazioni': prestazioni(self.tempi, self.codici), 'serie': self.serie}
            tmp = os.path.join(self.cartella, 'risultati.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(dati, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, os.path.join(self.cartella, 'risultati.json'))
        except Exception as e:
            self.log('salvataggio risultati non riuscito', e)


def percentile(valori, p):
    if not valori:
        return None
    v = sorted(valori)
    k = max(0, min(len(v) - 1, int(round((p / 100.0) * (len(v) - 1)))))
    return v[k]


def prestazioni(tempi, codici):
    out = {}
    for k, v in tempi.items():
        c = codici.get(k, {})
        err = sum(n for cod, n in c.items() if cod == 0 or cod >= 500)
        out[k] = {'n': len(v), 'p50_ms': percentile(v, 50), 'p95_ms': percentile(v, 95),
                  'max_ms': max(v) if v else None, 'errori_5xx_o_rete': err, 'codici': c}
    return out


# ---------------------------------------------------------------------------
# Client HTTP con cookie, misurato
# ---------------------------------------------------------------------------
import re as _re
_RX_ID = _re.compile(r'/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+|[0-9a-f]{24,})(?=/|$)')


def normalizza(percorso):
    p = percorso.split('?')[0]
    return _RX_ID.sub('/<id>', p)


class Client:
    """Un "dispositivo": cookie suoi, ogni chiamata misurata e registrata.
    Le risposte 500 e gli errori di rete diventano problemi da sole."""

    def __init__(self, base, reg: Registro, nome='client', stazione=None, entra=True, timeout=120,
                 persona_n=0):
        self.base = base.rstrip('/')
        self.reg = reg
        self.nome = nome
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.stazione = stazione
        self._pin = None          # PIN della persona entrata (per rientrare)
        self._ultimo_grezzo = b''
        self.rientri = 0          # quante volte ha rimesso il PIN (fine giornata alle 3)
        if stazione:
            self.registra(stazione, entra, persona_n)

    def registra(self, stazione, entra=True, persona_n=0):
        st = 'ufficio' if stazione == 'admin' else stazione
        c, d = self.post('/api/accesso/registra', {'stazione': st, 'pin': PIN_ADMIN, 'nome': 'Stress ' + st},
                         registra=False)
        if c != 200:
            raise RuntimeError('registrazione %s: %s %s' % (st, c, d))
        if st in UFFICI and entra:
            pin = persona(self.base, self.reg, st, persona_n)
            c, d = self.post('/api/accesso/entra', {'pin': pin}, registra=False)
            if c != 200:
                raise RuntimeError('ingresso %s: %s %s' % (st, c, d))
            self._pin = pin
        if stazione == 'admin':
            self.eleva()

    def eleva(self):
        c, d = self.post('/api/accesso/admin', {'pin': PIN_ADMIN}, registra=False, _rinnovo=False)
        return c == 200

    def _rientra(self, codice, d):
        """La sessione d'ufficio scade alle 3 di notte (fine giornata) e
        l'admin dopo 15 minuti: l'app chiede di nuovo il PIN (serve_pin /
        serve_pin_admin), ed e' giusto cosi'. Come farebbe la persona alla
        postazione, si rimette il PIN. True se si puo' ripetere la chiamata."""
        cod = d.get('codice') if isinstance(d, dict) else None
        if codice == 401 and cod == 'serve_pin' and self._pin:
            c, _ = self.post('/api/accesso/entra', {'pin': self._pin}, registra=False, _rinnovo=False)
            ok = c == 200
        elif codice == 403 and cod == 'serve_pin_admin' and self.stazione == 'admin':
            ok = self.eleva()
        else:
            return False
        if ok:
            self.rientri += 1
            self.reg.log('%s: PIN rimesso dopo %s (sessione scaduta: comportamento giusto)' % (self.nome, cod))
        return ok

    def chiama(self, metodo, percorso, corpo=None, dati=None, intestazioni=None, registra=True,
               atteso_500_ok=False, timeout=None, grezzo=False, _rinnovo=True):
        if _rinnovo:
            # sessione scaduta (serve_pin): si rimette il PIN e si ripete UNA volta
            codice, d = self.chiama(metodo, percorso, corpo, dati, intestazioni, registra,
                                    atteso_500_ok, timeout, False, False)
            if codice in (401, 403) and self._rientra(codice, d):
                codice, d = self.chiama(metodo, percorso, corpo, dati, intestazioni, registra,
                                        atteso_500_ok, timeout, False, False)
            return (codice, self._ultimo_grezzo) if grezzo else (codice, d)
        h = {}
        if corpo is not None:
            dati = json.dumps(corpo).encode()
            h['Content-Type'] = 'application/json'
        h.update(intestazioni or {})
        req = urllib.request.Request(self.base + percorso, data=dati, method=metodo, headers=h)
        t0 = time.perf_counter()
        codice, testo = 0, b''
        try:
            with self.op.open(req, timeout=timeout or self.timeout) as r:
                testo = r.read()
                codice = r.status
        except urllib.error.HTTPError as e:
            try:
                testo = e.read()
            except Exception:
                testo = b''
            codice = e.code
        except Exception as e:
            testo = ('%s: %s' % (type(e).__name__, e)).encode()
            codice = 0
        ms = (time.perf_counter() - t0) * 1000
        chiave = '%s %s' % (metodo, normalizza(percorso))
        self._ultimo_grezzo = testo
        if not (codice in (401, 403) and self._rinnovabile(testo)):
            # "rimetti il PIN" non e' un tempo dell'app: si misura la ripetuta
            self.reg.misura(chiave, ms, codice)
        if grezzo:
            return codice, testo
        try:
            d = json.loads(testo.decode('utf-8') or 'null')
        except Exception:
            d = testo[:2000].decode('utf-8', 'replace') if isinstance(testo, bytes) else testo
        if registra and not atteso_500_ok:
            if codice >= 500 and codice not in (502, 503):
                self.reg.problema('GRAVE', 'Errore 500 del server su %s' % chiave,
                                  'HTTP %s: %s' % (codice, str(d)[:600]),
                                  passi='%s %s (stazione %s)' % (metodo, percorso, self.stazione),
                                  prova={'metodo': metodo, 'percorso': percorso, 'corpo': corpo,
                                         'codice': codice, 'risposta': d},
                                  chiave=('500', chiave))
            elif codice == 0:
                self.reg.problema('GRAVE', 'Il server non risponde (errore di rete/timeout) su %s' % chiave,
                                  str(d)[:300], passi='%s %s' % (metodo, percorso),
                                  chiave=('rete', chiave))
            if isinstance(d, str) and 'database is locked' in d.lower() or (
                    isinstance(d, dict) and 'database is locked' in str(d.get('error', '')).lower()):
                self.reg.problema('GRAVE', 'Database bloccato (database is locked)', str(d)[:400],
                                  passi='%s %s' % (metodo, percorso), chiave=('locked', chiave))
        return codice, d

    def _rinnovabile(self, testo):
        t = testo or b''
        return ((b'"serve_pin"' in t and bool(self._pin))
                or (b'"serve_pin_admin"' in t and self.stazione == 'admin'))

    def get(self, p, **kw):
        return self.chiama('GET', p, **kw)

    def post(self, p, corpo=None, **kw):
        return self.chiama('POST', p, {} if corpo is None and 'dati' not in kw else corpo, **kw)

    def put(self, p, corpo=None, **kw):
        return self.chiama('PUT', p, {} if corpo is None else corpo, **kw)

    def delete(self, p, **kw):
        return self.chiama('DELETE', p, **kw)

    def multipart(self, percorso, campi=None, files=None, **kw):
        """files: [(campo, nome_file, bytes)]"""
        confine = '----ftstress' + uuid.uuid4().hex
        parti = []
        for k, v in (campi or {}).items():
            parti.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                          % (confine, k, v)).encode('utf-8'))
        for campo, nome, contenuto in (files or []):
            parti.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                          'Content-Type: application/octet-stream\r\n\r\n' % (confine, campo, nome)).encode('utf-8'))
            parti.append(contenuto)
            parti.append(b'\r\n')
        parti.append(('--%s--\r\n' % confine).encode())
        dati = b''.join(parti)
        return self.chiama('POST', percorso, dati=dati,
                           intestazioni={'Content-Type': 'multipart/form-data; boundary=' + confine}, **kw)


def scegli(lista, n, seme=None):
    r = random.Random(seme)
    lista = list(lista)
    r.shuffle(lista)
    return lista[:n]


# ---------------------------------------------------------------------------
# Persone di prova (sulla COPIA): una per postazione d'ufficio e numero, PIN
# fisso, create una volta sola. Non si rimette mai il PIN: cambiarlo chiude
# le sessioni gia' aperte di quella persona.
# ---------------------------------------------------------------------------
UFFICI = ('ufficio', 'commerciale')
_PERSONE = {}
_LOCK_PERSONE = threading.Lock()


def _pin_valido(pin):
    if len(set(pin)) == 1:
        return False
    cifre = [int(c) for c in pin]
    return {b - a for a, b in zip(cifre, cifre[1:])} not in ({1}, {-1})


def persona(base, reg, stazione, n=0):
    chiave = (base, stazione, n)
    with _LOCK_PERSONE:
        if chiave in _PERSONE:
            return _PERSONE[chiave]
        adm = Client(base, reg, 'admin-persone')
        c, d = adm.post('/api/accesso/registra', {'stazione': 'ufficio', 'pin': PIN_ADMIN, 'nome': 'Stress persone'},
                        registra=False)
        if c != 200 or not adm.eleva():
            raise RuntimeError('admin di prova non disponibile (%s %s)' % (c, d))
        nome = 'Stress %s %d' % (stazione, n)
        ruolo = 'Amministrazione' if stazione == 'ufficio' else 'Commerciale'
        c, d = adm.get('/api/admin/persone', registra=False)
        for x in (d.get('persone') or []) if isinstance(d, dict) else []:
            if x.get('nome') == nome:
                adm.delete('/api/admin/persone/%s' % x['id'], registra=False)
        base_pin = 6000 + (sum(map(ord, stazione)) % 50) * 60 + n * 7
        for k in range(200):
            pin = '%05d' % ((base_pin + k * 131) % 100000)
            if not _pin_valido(pin):
                continue
            c, d = adm.post('/api/admin/persone', {'nome': nome, 'ruolo': ruolo, 'admin': False, 'pin': pin},
                            registra=False)
            if c == 201:
                _PERSONE[chiave] = pin
                return pin
            if c == 409 and isinstance(d, dict) and d.get('codice') == 'gia_presente':
                nome = nome + 'x'
        raise RuntimeError('persona di prova %s non creata: %s %s' % (nome, c, d))


def contesto_browser(ctx_b, base, reg, stazione, n=0):
    """Un contesto Playwright registrato come quella postazione (e dentro col PIN negli uffici)."""
    st = 'ufficio' if stazione == 'admin' else stazione
    r = ctx_b.request.post(base + '/api/accesso/registra',
                           data={'stazione': st, 'pin': PIN_ADMIN, 'nome': 'Stress browser ' + st})
    if r.status != 200:
        raise RuntimeError('registrazione browser %s: %s' % (st, r.status))
    if st in UFFICI:
        r = ctx_b.request.post(base + '/api/accesso/entra', data={'pin': persona(base, reg, st, n)})
        if r.status != 200:
            raise RuntimeError('ingresso browser %s: %s' % (st, r.status))
    if stazione == 'admin':
        ctx_b.request.post(base + '/api/accesso/admin', data={'pin': PIN_ADMIN})
