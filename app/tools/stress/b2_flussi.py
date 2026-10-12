# -*- coding: utf-8 -*-
"""Blocco 2: ordini dall'inizio alla fine, con file VERI dell'archivio (sola lettura).

Percorsi d'ingresso (a rotazione, clienti misti):
  - pdf        : l'ufficio carica solo il PDF dell'ordine (PDF veri dagli uploads della copia)
  - pacchetto  : PDF + DXF di una cartella ordine DECA (Desktop\\AUTOCAD\\TAGLIO\\DECA\\ORDINI\\<ordine>)
                 o di un altro cliente
  - dwg        : pacchetto con DWG (conversione ODA, senza finestre)
  - preventivo : preventivo creato, mandato e accettato -> ordine
Poi il laser (smistamento, controllo con giusto/scelgo/V/P/N, spessori bloccati
-> /dati, Manda a Lantek con lo stub, taglio fatto), il tablet d'officina
(segnalazioni), la chiusura (lavoro finito, DDT, consegna, fattura).
A ogni passo i controlli (invarianti).
"""
import glob
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from comune import APP, AUTOCAD, STRESS_UPLOADS, Client

DECA_ORDINI = os.path.join(AUTOCAD, 'TAGLIO', 'DECA', 'ORDINI')
TAGLIO = os.path.join(AUTOCAD, 'TAGLIO')
MAX_FILE = 24
MAX_MB = 25


def _leggi(p):
    with open(p, 'rb') as f:
        return f.read()


def sorgenti(seme=7):
    """Le fonti dei pacchetti: [(cliente, [file])]. Solo lettura."""
    r = random.Random(seme)
    out = []
    try:
        cart = [d for d in os.listdir(DECA_ORDINI) if os.path.isdir(os.path.join(DECA_ORDINI, d))]
        r.shuffle(cart)
        for d in cart[:120]:
            pp = os.path.join(DECA_ORDINI, d)
            fs = [os.path.join(pp, f) for f in os.listdir(pp) if f.lower().endswith(('.dxf', '.pdf', '.dwg'))]
            dxf = [f for f in fs if f.lower().endswith('.dxf')]
            if 1 <= len(dxf) <= MAX_FILE and sum(os.path.getsize(f) for f in fs) < MAX_MB * 1e6:
                out.append(('DECA', fs))
    except OSError:
        pass
    altri = []
    try:
        for c in os.listdir(TAGLIO):
            pp = os.path.join(TAGLIO, c)
            if not os.path.isdir(pp) or c.upper() in ('DECA',):
                continue
            fs = [os.path.join(pp, f) for f in os.listdir(pp)
                  if f.lower().endswith(('.dxf', '.pdf', '.dwg')) and os.path.isfile(os.path.join(pp, f))]
            dxf = [f for f in fs if f.lower().endswith(('.dxf', '.dwg'))]
            if dxf:
                r.shuffle(dxf)
                scelti = dxf[:6]
                basi = {os.path.splitext(os.path.basename(f))[0].lower() for f in scelti}
                pdf = [f for f in fs if f.lower().endswith('.pdf')
                       and os.path.splitext(os.path.basename(f))[0].lower() in basi]
                tutti = scelti + pdf
                if sum(os.path.getsize(f) for f in tutti) < MAX_MB * 1e6:
                    altri.append((c, tutti))
    except OSError:
        pass
    r.shuffle(altri)
    return out, altri


_PDF_INIZIALI = []


def pdf_ordini():
    # solo i PDF che c'erano all'inizio (non quelli caricati dalla prova)
    if not _PDF_INIZIALI:
        _PDF_INIZIALI.extend(sorted(glob.glob(os.path.join(STRESS_UPLOADS, 'pdfs', '*.pdf'))))
    return list(_PDF_INIZIALI)


class Flussi:
    def __init__(self, ctx):
        self.ctx = ctx
        self.reg = ctx.reg
        base = ctx.server.base
        self.uff = Client(base, ctx.reg, 'b2-ufficio', 'ufficio')
        self.las = Client(base, ctx.reg, 'b2-laser', 'laser')
        self.com = Client(base, ctx.reg, 'b2-commerciale', 'commerciale')
        self.rep = Client(base, ctx.reg, 'b2-reparto', 'reparto')
        self.adm = Client(base, ctx.reg, 'b2-admin', 'admin')
        self.deca, self.altri = sorgenti()
        self.pdf = pdf_ordini()
        self.rnd = random.Random()
        self.creati = []
        self.reg.nota('fonti: %d cartelle DECA, %d altri clienti, %d PDF d\'ordine'
                      % (len(self.deca), len(self.altri), len(self.pdf)))

    # ------------------------------------------------------------------ util
    def ok(self, cond, desc, grav='MEDIO', det='', passi='', prova=None, chiave=None):
        return self.reg.controllo(bool(cond), desc, grav, det, passi, prova, chiave=chiave)

    def numero(self, prefisso='ST'):
        return '%s%s' % (prefisso, uuid.uuid4().hex[:6].upper())

    def consegna(self):
        return (date.today() + timedelta(days=self.rnd.randint(5, 40))).isoformat()

    # --------------------------------------------------------------- ingressi
    def da_pdf(self):
        if not self.pdf:
            return None, 'pdf'
        p = self.rnd.choice(self.pdf)
        nome = os.path.basename(p).split('_', 1)[-1]
        c, d = self.uff.multipart('/api/extract-pdf-data', files=[('file', nome, _leggi(p))])
        self.ok(c == 200 and isinstance(d, dict) and d.get('success'), 'Lettura del PDF d\'ordine',
                'MEDIO', 'HTTP %s %s' % (c, str(d)[:300]), 'POST /api/extract-pdf-data (PDF %s)' % nome,
                chiave=('pdf_lettura',))
        dati = (d.get('data') or {}) if isinstance(d, dict) else {}
        corpo = {'numero_ordine': self.numero('STPDF'), 'cliente': dati.get('cliente') or 'CLIENTE PROVA',
                 'data_consegna': self.consegna(), 'note': 'prova notturna',
                 'pdf_filename': dati.get('pdf_filename') or nome}
        if dati.get('valore_ordine') is not None:
            corpo['valore_ordine'] = dati['valore_ordine']
        c, d = self.uff.post('/api/orders', corpo)
        self.ok(c == 201 and d.get('order_id'), 'Creazione ordine da PDF', 'GRAVE',
                'HTTP %s %s' % (c, str(d)[:300]), 'POST /api/orders dopo extract-pdf-data', chiave=('pdf_crea',))
        return (d.get('order_id') if isinstance(d, dict) else None), 'pdf'

    def da_pacchetto(self, dwg=False):
        fonti = self.altri if dwg else (self.deca + self.altri[:max(1, len(self.altri) // 3)])
        if dwg:
            fonti = [f for f in self.altri if any(x.lower().endswith('.dwg') for x in f[1])] or fonti
        if not fonti:
            return None, 'pacchetto'
        cliente, files = self.rnd.choice(fonti)
        if dwg:
            files = [f for f in files if f.lower().endswith(('.dwg', '.pdf'))][:4] or files
        parti = [('files', os.path.basename(f), _leggi(f)) for f in files]
        senza_pdf = not any(f.lower().endswith('.pdf') for f in files)
        if self.pdf and (senza_pdf or self.rnd.random() < 0.5):
            p = self.rnd.choice(self.pdf)
            parti.append(('files', os.path.basename(p).split('_', 1)[-1], _leggi(p)))
        tipo = 'dwg' if dwg else 'pacchetto'
        c, d = self.uff.multipart('/api/orders/da-pacchetto/analizza', files=parti, timeout=600)
        if not self.ok(c == 200 and isinstance(d, dict) and d.get('success'),
                       'Analisi del pacchetto (%s)' % tipo, 'GRAVE',
                       'HTTP %s %s' % (c, str(d)[:300]),
                       'POST /api/orders/da-pacchetto/analizza con %d file di %s (%s)'
                       % (len(parti), cliente, ', '.join(os.path.basename(f) for f in files[:5])),
                       chiave=('pac_analizza', tipo, c)):
            return None, tipo
        righe = d.get('righe') or []
        rr = []
        for r in righe:
            q = r.get('quantita') or self.rnd.randint(1, 20)
            rr.append({'id': r['id'], 'quantita': q, 'materiale': r.get('materiale') or 'S235',
                       'spessore_mm': r.get('spessore_mm') if self.rnd.random() < 0.8 else None})
        corpo = {'numero_ordine': self.numero('STPAC'), 'cliente': d.get('cliente') or cliente,
                 'data_consegna': self.consegna(), 'note': 'prova notturna', 'righe': rr, 'forza': True}
        c, e = self.uff.post('/api/orders/da-pacchetto/%s/conferma' % d['pacchetto_id'], corpo, timeout=600)
        if not self.ok(c == 201 and e.get('order_id'), 'Conferma del pacchetto -> ordine (%s)' % tipo, 'GRAVE',
                       'HTTP %s %s' % (c, str(e)[:300]), 'conferma pacchetto di %s con %d righe' % (cliente, len(rr)),
                       chiave=('pac_conferma', tipo)):
            return None, tipo
        oid = e['order_id']
        n_attesi = sum(1 for r in rr if r['quantita'])
        self.ok(e.get('n_pezzi') in (None, n_attesi), 'Righe del pacchetto = pezzi dell\'ordine', 'MEDIO',
                'righe confermate %d, pezzi nell\'ordine %s' % (n_attesi, e.get('n_pezzi')),
                'conferma pacchetto %s' % cliente, chiave=('pac_conta',))
        # doppio clic sulla conferma: non deve creare un secondo ordine
        c2, e2 = self.uff.post('/api/orders/da-pacchetto/%s/conferma' % d['pacchetto_id'], corpo)
        self.ok(c2 == 409 and (e2.get('codice') == 'gia_confermato' or e2.get('order_id') in (None, oid)),
                'Il pacchetto confermato due volte non crea un secondo ordine', 'GRAVE',
                'seconda conferma: HTTP %s %s' % (c2, str(e2)[:200]), 'confermare due volte lo stesso pacchetto',
                chiave=('pac_doppio',))
        self._quantita_pacchetto = (oid, {r['id']: r['quantita'] for r in rr})
        return oid, tipo

    def da_preventivo(self):
        cli = self.rnd.choice(['DECA', 'TODEMA', 'CLIENTE PROVA'])
        c, d = self.com.post('/api/preventivi', {'cliente': cli, 'quantita': 1, 'note': 'prova notturna',
                                                 'numero_ordine_cliente': self.numero('STPRE')})
        if not self.ok(c == 201, 'Creazione preventivo', 'GRAVE', 'HTTP %s %s' % (c, str(d)[:300]),
                       'POST /api/preventivi', chiave=('prev_crea',)):
            return None, 'preventivo'
        pid = d['preventivo']['id']
        art = [{'codice': 'ST-%s-%d' % (pid[:4], i), 'quantita': self.rnd.randint(1, 30),
                'costo_base_stimato': round(self.rnd.uniform(5, 200), 2), 'spessore_mm': self.rnd.choice([2, 3, 5]),
                'materiale': 'S235'} for i in range(self.rnd.randint(1, 5))]
        c, d = self.com.put('/api/preventivi/%s/articoli' % pid, {'articoli': art})
        self.ok(c == 200, 'Articoli del preventivo salvati', 'GRAVE', 'HTTP %s %s' % (c, str(d)[:300]),
                'PUT /api/preventivi/<id>/articoli', chiave=('prev_art',))
        c, d = self.com.post('/api/preventivi/%s/invia' % pid, {})
        self.ok(c == 200, 'Preventivo mandato', 'MEDIO', 'HTTP %s %s' % (c, str(d)[:300]),
                'POST /api/preventivi/<id>/invia', chiave=('prev_invia',))
        c, d = self.com.post('/api/preventivi/%s/accetta' % pid, {'data_consegna': self.consegna()})
        if not self.ok(c == 200 and d.get('order_id'), 'Preventivo accettato -> ordine', 'GRAVE',
                       'HTTP %s %s' % (c, str(d)[:300]), 'POST /api/preventivi/<id>/accetta', chiave=('prev_acc',)):
            return None, 'preventivo'
        oid = d['order_id']
        c2, d2 = self.com.post('/api/preventivi/%s/accetta' % pid, {})
        self.ok((c2 == 200 and d2.get('order_id') == oid) or c2 == 409,
                'Preventivo accettato due volte: stesso ordine, nessun doppione', 'GRAVE',
                'HTTP %s ordine %s (primo %s)' % (c2, d2.get('order_id') if isinstance(d2, dict) else d2, oid),
                'accettare due volte', chiave=('prev_doppio',))
        return oid, 'preventivo'

    # ------------------------------------------------------------- invarianti
    def stato(self, oid):
        c, d = self.uff.get('/api/orders/%s' % oid)
        o = d.get('order', d) if isinstance(d, dict) else {}
        return c, o

    def invarianti(self, oid, passo):
        c, o = self.stato(oid)
        self.ok(c == 200, 'L\'ordine si legge dopo "%s"' % passo, 'GRAVE', 'HTTP %s' % c,
                'GET /api/orders/<id> dopo %s' % passo, chiave=('inv_leggi', passo))
        if c != 200:
            return
        status = o.get('status')
        c2, l = self.las.get('/api/orders?aperti=1')
        aperti = {x.get('id'): x for x in (l.get('orders') or [])} if isinstance(l, dict) else {}
        if oid in aperti:
            self.ok(aperti[oid].get('status') == status,
                    'Stato dell\'ordine uguale fra elenco e dettaglio', 'GRAVE',
                    'dettaglio %s, elenco %s (dopo %s)' % (status, aperti[oid].get('status'), passo),
                    'confrontare GET /api/orders/<id> e /api/orders?aperti=1', chiave=('inv_stato',))
        c3, lz = self.las.get('/api/laser/ordini?ordine=%s' % oid)
        self.ok(c3 == 200, 'Stato laser leggibile', 'MEDIO', 'HTTP %s' % c3, chiave=('inv_laser',))

    def audit(self, oid, azioni):
        self.adm.eleva()
        c, d = self.adm.get('/api/admin/audit-log?limit=400')
        if c != 200:
            self.ok(False, 'Registro attivita\' leggibile', 'MEDIO', 'HTTP %s %s' % (c, str(d)[:200]),
                    chiave=('audit_leggi',))
            return
        testo = json.dumps(d, ensure_ascii=False)
        for a in azioni:
            presente = oid in testo and a in testo
            self.ok(presente, 'Nel registro attivita\' c\'e\' "%s" per l\'ordine' % a, 'MEDIO',
                    'ordine %s, azione %s non trovata negli ultimi 400' % (oid[:8], a),
                    'GET /api/admin/audit-log dopo il passo', chiave=('audit', a))

    # ------------------------------------------------------------------ laser
    def laser(self, oid):
        b = '/api/orders/%s' % oid
        c, d = self.las.post(b + '/smistamento', {'va_tagliato': True})
        self.ok(c == 200, 'Smistamento al laser', 'GRAVE', 'HTTP %s %s' % (c, str(d)[:200]),
                'POST smistamento {va_tagliato:true}', chiave=('smista',))
        c, an = self.las.get(b + '/laser?fresco=1', timeout=600)
        if not self.ok(c == 200 and isinstance(an, dict), 'Analisi laser dell\'ordine', 'GRAVE',
                       'HTTP %s %s' % (c, str(an)[:200]), 'GET /api/orders/<id>/laser?fresco=1',
                       chiave=('laser_an',)):
            return
        pezzi = an.get('pezzi') or []
        risposte = {}
        for p in pezzi:
            if not p.get('da_controllare'):
                continue
            aid = p.get('articolo_id')
            if not aid:
                continue
            scelta = self.rnd.choices(['giusto', 'scelgo', 'V', 'P', 'N'], [50, 15, 15, 10, 10])[0]
            base = b + '/pezzi/%s' % aid
            if scelta == 'giusto':
                c, d = self.las.post(base + '/giusto', {'serve_pulito': p.get('lantek') in ('nuovo', 'sconosciuto')})
                ok = c == 200 or (c == 409 and d.get('codice') in ('senza_contorno', 'contorno_non_preciso'))
            elif scelta == 'scelgo':
                c, g = self.las.get(base + '/cad/geometry-json')
                pt = _primo_punto(g) if c == 200 else None
                ok = True
                if pt:
                    c, cand = self.las.post(base + '/cad/pick-candidates', {'x': pt[0], 'y': pt[1]})
                    cc = (cand.get('candidates') or []) if isinstance(cand, dict) else []
                    if cc:
                        c, d = self.las.post(base + '/contorno', {'outer_xy': cc[0]['outer_xy'],
                                                                  'holes_xy': cc[0].get('holes_xy') or [],
                                                                  'pagina': 'laser'})
                        ok = c in (200, 201) or (c in (400, 409) and isinstance(d, dict) and d.get('error'))
                    else:
                        d = cand
                else:
                    d = g
            else:
                sc = {'V': 'sviluppo', 'N': 'non_laser', 'P': 'piu_pezzi'}[scelta]
                c, d = self.las.post(base + '/decisione', {'scelta': sc})
                ok = c == 200 or (c in (400, 409) and isinstance(d, dict) and d.get('error'))
            risposte[scelta] = risposte.get(scelta, 0) + 1
            self.ok(ok, 'Risposta "%s" nel controllo del laser' % scelta, 'MEDIO', 'HTTP %s %s' % (c, str(d)[:200]),
                    'controllo laser, pezzo %s, risposta %s' % (p.get('codice'), scelta), chiave=('ctrl', scelta, c))
        # spessori/materiali bloccati -> /dati
        c, an = self.las.get(b + '/laser?fresco=1', timeout=600)
        for bl in (an.get('bloccati') or []) if isinstance(an, dict) else []:
            aid = bl.get('articolo_id')
            prop = bl.get('proposta') or {}
            if bl.get('campo') == 'spessore':
                corpo = {'spessore_mm': prop.get('spessore_mm') or self.rnd.choice([2, 3, 4])}
            elif bl.get('campo') == 'materiale':
                corpo = {'materiale': prop.get('materiale') or 'FERRO'}
            else:
                continue
            c, d = self.las.post(b + '/pezzi/%s/dati' % aid, corpo)
            self.ok(c == 200, 'Dato bloccato (%s) inserito con /dati' % bl.get('campo'), 'MEDIO',
                    'HTTP %s %s' % (c, str(d)[:200]), 'POST /pezzi/<id>/dati %s' % corpo, chiave=('dati', c))
        # pezzi del foglio: lettura
        for p in pezzi[:2]:
            if p.get('articolo_id'):
                self.las.get(b + '/pezzi/%s/pezzi-foglio' % p['articolo_id'])
        # Manda a Lantek (stub) + secondo invio: niente doppioni
        c, an = self.las.get(b + '/laser?fresco=1', timeout=600)
        inv = (an.get('invio') or {}) if isinstance(an, dict) else {}
        codici, nuovi = inv.get('codici') or [], inv.get('nuovi') or []
        codici = [x if isinstance(x, str) else x.get('codice') for x in codici]
        nuovi = [x if isinstance(x, str) else x.get('codice') for x in nuovi]
        if codici or nuovi:
            c, d = self.las.post(b + '/lantek-invia', {'codici': codici, 'nuovi': nuovi}, timeout=600)
            self.ok(c == 200 or (c in (409, 502) and isinstance(d, dict) and d.get('codice')),
                    'Manda a Lantek (Lantek finto)', 'GRAVE', 'HTTP %s %s' % (c, str(d)[:300]),
                    'POST lantek-invia %d codici %d nuovi' % (len(codici), len(nuovi)), chiave=('lantek', c))
            if c == 200:
                c2, d2 = self.las.post(b + '/lantek-invia', {'codici': codici, 'nuovi': nuovi}, timeout=600)
                self.ok(c2 != 200 or not d2.get('mandati'),
                        'Secondo "Manda a Lantek" subito dopo: non deve rimandare gli stessi pezzi', 'GRAVE',
                        'secondo invio HTTP %s mandati %s' % (c2, d2.get('mandati') if isinstance(d2, dict) else d2),
                        'premere due volte Manda a Lantek', chiave=('lantek_doppio',))
                self.las.post(b + '/importato', {'annulla': False})
                self.audit(oid, ['LANTEK_INVIA'])
        else:
            self.reg.nota('ordine %s: niente da mandare a Lantek (codici/nuovi vuoti)' % oid[:8])
        # taglio fatto (e doppio clic)
        c, d = self.las.post(b + '/mark-laser-done', {})
        self.ok(c == 200, 'Taglio fatto', 'GRAVE', 'HTTP %s %s' % (c, str(d)[:200]), 'POST mark-laser-done',
                chiave=('taglio', c))
        c2, d2 = self.las.post(b + '/mark-laser-done', {})
        self.ok(c2 in (200, 400, 409), 'Taglio fatto premuto due volte', 'MEDIO', 'HTTP %s' % c2,
                chiave=('taglio2', c2))
        self.reg.nota('ordine %s: risposte controllo %s' % (oid[:8], risposte))

    # --------------------------------------------------------------- officina
    def officina(self, oid):
        c, d = self.rep.get('/api/officina/tablet')
        self.ok(c == 200, 'Tablet officina: elenco ordini', 'GRAVE', 'HTTP %s' % c, chiave=('tablet',))
        corpo = {'tipo': 'disegno', 'nota': 'prova notturna'}
        c, d = self.rep.post('/api/orders/%s/segnalazioni' % oid, corpo)
        self.ok(c in (200, 201), 'Segnalazione dal tablet', 'MEDIO', 'HTTP %s %s' % (c, str(d)[:200]),
                chiave=('segn', c))
        c2, d2 = self.rep.post('/api/orders/%s/segnalazioni' % oid, corpo)
        self.ok(c2 == 200 and d2.get('gia_segnalato'), 'Segnalazione doppia riconosciuta', 'LIEVE',
                'HTTP %s %s' % (c2, str(d2)[:200]), chiave=('segn2',))

    # ---------------------------------------------------------------- chiusura
    def chiusura(self, oid):
        passi = [('close', '/api/orders/%s/close' % oid, {}),
                 ('ddt', '/api/ordini/%s/ddt' % oid, {'numero': self.numero('DDT')}),
                 ('consegna', '/api/ordini/%s/consegna' % oid, {'completa': True, 'note': 'prova'}),
                 ('chiudi', '/api/ordini/%s/chiudi' % oid, {'numero_fattura': self.numero('F'),
                                                           'data_fattura': date.today().isoformat()})]
        for nome, perc, corpo in passi:
            c, d = self.uff.post(perc, corpo)
            self.ok(c == 200, 'Chiusura: passo "%s"' % nome, 'MEDIO', 'HTTP %s %s' % (c, str(d)[:250]),
                    'POST %s' % perc, chiave=('chiusura', nome, c))
            self.invarianti(oid, nome)
        c, v = self.uff.get('/api/ordini/viste')
        testo = json.dumps(v, ensure_ascii=False) if c == 200 else ''
        c, o = self.stato(oid)
        self.reg.nota('ordine %s chiuso: status %s' % (oid[:8], o.get('status')))

    # ------------------------------------------------------------------ giro
    def un_ordine(self, i):
        tipo = ['pacchetto', 'pdf', 'pacchetto', 'preventivo', 'dwg'][i % 5]
        t0 = time.time()
        try:
            if tipo == 'pdf':
                oid, tipo = self.da_pdf()
            elif tipo == 'preventivo':
                oid, tipo = self.da_preventivo()
            else:
                oid, tipo = self.da_pacchetto(dwg=(tipo == 'dwg'))
        except Exception as e:
            self.reg.eccezione('b2 ingresso %s' % tipo, e)
            return
        if not oid:
            return
        self.creati.append(oid)
        self.ctx.ordini_creati = getattr(self.ctx, 'ordini_creati', []) + [oid]
        self.invarianti(oid, 'creazione')
        self.audit(oid, ['CREATE'])
        for nome, f in (('laser', self.laser), ('officina', self.officina), ('chiusura', self.chiusura)):
            try:
                f(oid)
                self.invarianti(oid, nome)
            except Exception as e:
                self.reg.eccezione('b2 %s' % nome, e)
        self.reg.log('ordine %d (%s) %s completato in %.0f s' % (i + 1, tipo, oid[:8], time.time() - t0))


def _primo_punto(g):
    """Un punto qualsiasi del disegno (sta su un segmento)."""
    stack = [g]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            if len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]) and len(x) <= 3:
                return x[:2]
            stack.extend(reversed(x))
        elif isinstance(x, dict):
            for k in ('x1', 'x'):
                if k in x and isinstance(x[k], (int, float)):
                    return [x[k], x.get('y1', x.get('y', 0))]
            stack.extend(reversed(list(x.values())))
    return None


def verifica_lantek_stub(ctx):
    """I file che sarebbero andati a Lantek: formato giusto, niente doppioni."""
    reg = ctx.reg
    p = os.path.join(ctx.run_dir, 'lantek_stub', 'invii.jsonl')
    if not os.path.isfile(p):
        reg.nota('nessun invio a Lantek (stub) registrato')
        return
    visti, n = {}, 0
    with open(p, encoding='utf-8') as f:
        for riga in f:
            try:
                v = json.loads(riga)
            except Exception:
                continue
            n += 1
            if v.get('errori_formato'):
                reg.problema('GRAVE', 'File per Lantek fuori formato (%s)' % v.get('tipo'),
                             '; '.join(v['errori_formato'][:5]), passi='vedi %s' % (v.get('file') or v.get('cartella')),
                             prova=v, chiave=('lantek_formato', v.get('tipo'), v['errori_formato'][0][:40]))
            if v.get('tipo') == 'xml' and v.get('esito') == 'ok':
                for pr in v.get('prodotti') or []:
                    k = (pr.get('SaleOrder'), pr.get('Product'))
                    visti.setdefault(k, []).append(v.get('quando'))
    doppi = {k: q for k, q in visti.items() if len(q) > 1}
    reg.nota('invii a Lantek finto: %d registrati, %d coppie ordine/pezzo, %d mandate piu\' volte'
             % (n, len(visti), len(doppi)))
    for k, q in list(doppi.items())[:20]:
        reg.problema('GRAVE', 'Lo stesso pezzo dello stesso ordine e\' stato mandato a Lantek piu\' volte',
                     'ordine %s pezzo %s: %d volte (%s)' % (k[0], k[1], len(q), ', '.join(q[:4])),
                     passi='vedi lantek_stub/invii.jsonl', chiave=('lantek_doppio_stub', k))


def file_orfani(ctx):
    """Righe di order_files il cui file non c'e' (nella copia)."""
    c = sqlite3.connect('file:%s?mode=ro' % ctx.db.replace('\\', '/'), uri=True)
    righe = c.execute('select o.id, f.filename, f.filepath from order_files f left join orders o '
                      'on o.id = f.order_id').fetchall()
    c.close()
    nostri = set(getattr(ctx, 'ordini_creati', []))
    tutti_mancanti = [(oid, fn) for oid, fn, fp in righe if fp and not os.path.exists(fp)]
    mancano = [x for x in tutti_mancanti if x[0] in nostri]
    if len(tutti_mancanti) > len(mancano):
        ctx.reg.nota('%d file mancanti in ordini preesistenti (copia degli uploads non aggiornata?)'
                     % (len(tutti_mancanti) - len(mancano)))
    senza_ordine = [fn for oid, fn, fp in righe if oid is None]
    ctx.reg.controllo(not mancano, 'File registrati negli ordini ma mancanti sul disco', 'MEDIO',
                      '%d mancanti, es. %s' % (len(mancano), mancano[:5]), chiave=('orfani_mancanti',))
    ctx.reg.controllo(not senza_ordine, 'File registrati senza ordine (orfani)', 'LIEVE',
                      '%d, es. %s' % (len(senza_ordine), senza_ordine[:5]), chiave=('orfani_senza',))


def esegui(ctx):
    f = Flussi(ctx)
    minimo = 30 if ctx.profilo == 'notte' else 3
    i = getattr(ctx, 'b2_contatore', 0)
    fatti = 0
    while datetime.now() < ctx.scadenza or (ctx.profilo == 'notte' and fatti < minimo
                                            and datetime.now() < ctx.fine - timedelta(hours=6)):
        f.un_ordine(i)
        i += 1
        fatti += 1
        if ctx.profilo == 'rapido' and fatti >= 3:
            break
    ctx.b2_contatore = i
    ctx.reg.nota('ordini creati in questo giro: %d (tentati %d)' % (len(f.creati), fatti))
    try:
        verifica_lantek_stub(ctx)
    except Exception as e:
        ctx.reg.eccezione('b2 verifica stub', e)
    try:
        file_orfani(ctx)
    except Exception as e:
        ctx.reg.eccezione('b2 orfani', e)
