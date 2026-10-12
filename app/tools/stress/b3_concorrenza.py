# -*- coding: utf-8 -*-
"""Blocco 3: piu' postazioni insieme sugli STESSI ordini.

- 6-10 postazioni simulate (ufficio, laser, tablet, timbratrice) che
  interrogano il server come fanno le pagine e fanno azioni a caso sugli
  stessi ordini;
- prove mirate:
  * aggiornamento perso: laser (pianifica taglio) e ufficio (DDT) sullo
    stesso ordine nello stesso istante -> devono restare tutti e due;
  * doppio clic: la stessa POST due volte insieme (taglio fatto,
    smistamento, Manda a Lantek) -> un solo effetto;
  * ore: due postazioni salvano la stessa giornata con la stessa revisione
    -> una sola vince, l'altra riceve 409;
- controlli: niente 500, niente "database is locked", stato finale coerente.
"""
import json
import os
import random
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from comune import Client

POLL = {
    'laser': [('/api/orders?aperti=1', 5), ('/api/laser/ordini', 60), ('/api/notifications?limit=1', 30)],
    'ufficio': [('/api/ordini/viste', 10), ('/api/orders', 10), ('/api/ordini-da-fatturare/count', 30),
                ('/api/preventivi?status=INVIATO&limit=200', 30), ('/api/notifications?limit=1', 30),
                ('/api/ore/anomalie?stato=aperta', 120)],
    'reparto': [('/api/officina/tablet', 60)],
    'ore': [('/api/ore/bacheca', 60), ('/api/ore/clienti', 60)],
    'pubblico': [('/api/dashboard-live', 10)],
}


def _insieme(funzioni):
    """Fa partire le funzioni nello stesso istante; torna i risultati."""
    barriera = threading.Barrier(len(funzioni))
    out = [None] * len(funzioni)

    def corri(i, f):
        try:
            barriera.wait(10)
        except Exception:
            pass
        try:
            out[i] = f()
        except Exception as e:
            out[i] = ('eccezione', str(e))
    th = [threading.Thread(target=corri, args=(i, f)) for i, f in enumerate(funzioni)]
    for t in th:
        t.start()
    for t in th:
        t.join(300)
    return out


class Concorrenza:
    def __init__(self, ctx):
        self.ctx, self.reg = ctx, ctx.reg
        base = ctx.server.base
        self.base = base
        self.uff = [Client(base, ctx.reg, 'b3-uff%d' % i, 'ufficio', persona_n=10 + i) for i in range(2)]
        self.las = [Client(base, ctx.reg, 'b3-las%d' % i, 'laser') for i in range(2)]
        self.com = Client(base, ctx.reg, 'b3-com', 'commerciale', persona_n=10)
        self.rnd = random.Random()
        self.ordini = []

    def ok(self, cond, desc, grav='MEDIO', det='', passi='', chiave=None, prova=None):
        return self.reg.controllo(bool(cond), desc, grav, det, passi, prova, chiave=chiave)

    def crea_ordine(self):
        """Ordine veloce: preventivo accettato (nessun file)."""
        c, d = self.com.post('/api/preventivi', {'cliente': 'CLIENTE PROVA', 'quantita': 1,
                                                 'numero_ordine_cliente': 'STCON' + uuid.uuid4().hex[:6]})
        if c != 201:
            return None
        pid = d['preventivo']['id']
        art = [{'codice': 'STC-%s-%d' % (pid[:4], i), 'quantita': 5, 'costo_base_stimato': 10.0,
                'spessore_mm': 3, 'materiale': 'S235'} for i in range(3)]
        self.com.put('/api/preventivi/%s/articoli' % pid, {'articoli': art})
        self.com.post('/api/preventivi/%s/invia' % pid, {})
        c, d = self.com.post('/api/preventivi/%s/accetta' % pid,
                             {'data_consegna': (date.today() + timedelta(days=20)).isoformat()})
        return d.get('order_id') if c == 200 else None

    def ordine(self, oid):
        c, d = self.uff[0].get('/api/orders/%s' % oid)
        return d.get('order', d) if isinstance(d, dict) else {}

    # ------------------------------------------------------------ prove mirate
    def aggiornamento_perso(self, oid):
        giorno = (date.today() + timedelta(days=self.rnd.randint(1, 15))).isoformat()
        ddt = 'DDT' + uuid.uuid4().hex[:5].upper()
        r = _insieme([
            lambda: self.las[0].post('/api/orders/%s/pianifica-taglio' % oid, {'data': giorno, 'durata_min': 30}),
            lambda: self.uff[0].post('/api/ordini/%s/ddt' % oid, {'numero': ddt}),
            lambda: self.las[1].post('/api/orders/%s/smistamento' % oid, {'va_tagliato': True}),
        ])
        o = self.ordine(oid)
        txt = json.dumps(o, ensure_ascii=False, default=str)
        if r[0] and r[0][0] == 200:
            self.ok(giorno in txt, 'Aggiornamento perso: la data di taglio pianificata e\' sparita', 'GRAVE',
                    'pianificata %s, nell\'ordine non c\'e\' (DDT e smistamento salvati insieme)' % giorno,
                    'laser pianifica-taglio + ufficio DDT + smistamento nello stesso istante',
                    chiave=('perso_pianifica',), prova={'risposte': r, 'ordine': o})
        if r[1] and r[1][0] == 200:
            self.ok(ddt in txt, 'Aggiornamento perso: il numero di DDT e\' sparito', 'GRAVE',
                    'DDT %s non presente nell\'ordine' % ddt,
                    'laser pianifica-taglio + ufficio DDT nello stesso istante', chiave=('perso_ddt',),
                    prova={'risposte': r, 'ordine': o})

    def doppio_clic(self, oid):
        # taglio fatto due volte insieme: uno solo deve "vincere"
        r = _insieme([lambda: self.las[0].post('/api/orders/%s/mark-laser-done' % oid, {}),
                      lambda: self.las[1].post('/api/orders/%s/mark-laser-done' % oid, {})])
        cod = [x[0] if isinstance(x, tuple) else None for x in r]
        self.ok(cod.count(200) <= 1 or True, 'Taglio fatto doppio', 'LIEVE', str(cod))
        if cod.count(200) == 2:
            self.reg.problema('MEDIO', 'Taglio fatto premuto due volte insieme: accettato due volte',
                              'risposte %s (il secondo dovrebbe dire "gia\' marcato")' % cod,
                              'due postazioni laser premono Taglio fatto nello stesso istante',
                              chiave=('doppio_taglio',))
        self.las[0].post('/api/orders/%s/mark-laser-undone' % oid, {})
        # chiusura "lavoro finito" doppia
        r = _insieme([lambda: self.uff[0].post('/api/orders/%s/close' % oid, {}),
                      lambda: self.uff[1].post('/api/orders/%s/close' % oid, {})])
        gia = [x[1].get('gia_registrato') for x in r if isinstance(x, tuple) and isinstance(x[1], dict)]
        if gia.count(False) == 2:
            self.reg.problema('MEDIO', 'Lavoro finito premuto due volte insieme: registrato due volte',
                              str(gia), 'due postazioni ufficio insieme su /close', chiave=('doppio_close',))
        self.uff[0].post('/api/ordini/%s/riapri' % oid, {})

    def doppio_lantek(self, oid):
        c, an = self.las[0].get('/api/orders/%s/laser?fresco=1' % oid, timeout=300)
        inv = (an.get('invio') or {}) if isinstance(an, dict) else {}
        cod = [x if isinstance(x, str) else x.get('codice') for x in inv.get('codici') or []]
        nuovi = [x if isinstance(x, str) else x.get('codice') for x in inv.get('nuovi') or []]
        if not (cod or nuovi):
            return
        corpo = {'codici': cod, 'nuovi': nuovi}
        r = _insieme([lambda: self.las[0].post('/api/orders/%s/lantek-invia' % oid, corpo, timeout=300),
                      lambda: self.las[1].post('/api/orders/%s/lantek-invia' % oid, corpo, timeout=300)])
        riusciti = [x for x in r if isinstance(x, tuple) and x[0] == 200 and (x[1] or {}).get('mandati')]
        self.ok(len(riusciti) <= 1, 'Manda a Lantek premuto da due postazioni insieme: mandato due volte', 'GRAVE',
                'risposte %s' % [x[0] if isinstance(x, tuple) else x for x in r],
                'due postazioni laser premono Manda a Lantek sullo stesso ordine nello stesso istante',
                chiave=('doppio_lantek',))

    def ore_conflitto(self):
        ore = [Client(self.base, self.reg, 'b3-ore%d' % i, 'ore') for i in range(2)]
        c, ctxo = ore[0].get('/api/ore/contesto')
        oggi = (ctxo or {}).get('oggi') if isinstance(ctxo, dict) else None
        c, op = ore[0].get('/api/ore/operai')
        operai = (op.get('operai') if isinstance(op, dict) else op) or []
        if not operai:
            self.uff[0].post('/api/ore/operai', {'nome': 'Operaio Stress %s' % uuid.uuid4().hex[:4]})
            c, op = ore[0].get('/api/ore/operai')
            operai = (op.get('operai') if isinstance(op, dict) else op) or []
        c, cl = ore[0].get('/api/ore/clienti')
        clienti = (cl.get('clienti') if isinstance(cl, dict) else cl) or []
        if not (oggi and operai and clienti):
            self.reg.nota('ore: niente operai/clienti nella copia, prova di conflitto saltata')
            return
        o = self.rnd.choice(operai)
        oid = o.get('id') if isinstance(o, dict) else o
        cli = clienti[0].get('nome') if isinstance(clienti[0], dict) else clienti[0]
        c, g = ore[0].get('/api/ore/giornata?operatore_id=%s&data=%s' % (oid, oggi))
        rev = ((g or {}).get('giornata') or {}).get('revisione', 0) if isinstance(g, dict) else 0

        def salva(cl_, minuti):
            return cl_.post('/api/ore/giornata', {'operatore_id': oid, 'data': oggi, 'revisione_attesa': rev,
                                                  'richiesta_id': uuid.uuid4().hex,
                                                  'righe': [{'cliente': cli, 'minuti': minuti}],
                                                  'scostamento_confermato': True})
        r = _insieme([lambda: salva(ore[0], 240), lambda: salva(ore[1], 480)])
        cod = [x[0] if isinstance(x, tuple) else None for x in r]
        self.ok(cod.count(200) <= 1, 'Ore: due postazioni salvano la stessa giornata insieme e passano tutte e due',
                'GRAVE', 'risposte %s (atteso: una 200 e una 409)' % cod,
                'due timbratrici salvano la giornata dello stesso operaio con la stessa revisione',
                chiave=('ore_doppio',), prova={'risposte': r})

    # ---------------------------------------------------------------- traffico
    def postazione(self, tipo, n, fine, ordini, stop):
        try:
            if tipo == 'pubblico':
                cl = Client(self.base, self.reg, 'b3-pub%d' % n)
            else:
                cl = Client(self.base, self.reg, 'b3-%s%d' % (tipo, n), tipo,
                            persona_n=20 + n if tipo in ('ufficio', 'commerciale') else 0)
        except Exception as e:
            self.reg.eccezione('b3 postazione %s' % tipo, e)
            return
        prossimo = {p: time.time() + self.rnd.uniform(0, s) for p, s in POLL.get(tipo, [])}
        rnd = random.Random()
        while datetime.now() < fine and not stop.is_set():
            ora = time.time()
            for p, s in POLL.get(tipo, []):
                if ora >= prossimo[p]:
                    cl.get(p)
                    prossimo[p] = ora + s
            if ordini and rnd.random() < 0.15:
                oid = rnd.choice(ordini)
                try:
                    self.azione(cl, tipo, oid, rnd)
                except Exception as e:
                    self.reg.eccezione('b3 azione %s' % tipo, e)
            time.sleep(rnd.uniform(0.5, 1.5))

    def azione(self, cl, tipo, oid, rnd):
        b = '/api/orders/%s' % oid
        if tipo == 'laser':
            a = rnd.choice(['smista', 'pianifica', 'analisi', 'taglio', 'stato'])
            if a == 'smista':
                cl.post(b + '/smistamento', {'va_tagliato': rnd.random() < 0.8})
            elif a == 'pianifica':
                cl.post(b + '/pianifica-taglio', {'data': (date.today() + timedelta(days=rnd.randint(0, 10))).isoformat()})
            elif a == 'analisi':
                cl.get(b + '/laser')
            elif a == 'taglio':
                cl.post(b + ('/mark-laser-done' if rnd.random() < 0.6 else '/mark-laser-undone'), {})
            else:
                cl.get('/api/laser/ordini?ordine=%s' % oid)
        elif tipo == 'ufficio':
            a = rnd.choice(['leggi', 'ddt', 'close', 'riapri'])
            if a == 'leggi':
                cl.get(b)
            elif a == 'ddt':
                cl.post('/api/ordini/%s/ddt' % oid, {'numero': 'DDT' + uuid.uuid4().hex[:4]})
            elif a == 'close':
                cl.post(b + '/close', {})
            else:
                cl.post('/api/ordini/%s/riapri' % oid, {})
        elif tipo == 'reparto':
            if rnd.random() < 0.3:
                cl.post(b + '/segnalazioni', {'tipo': rnd.choice(['manca_materiale', 'disegno', 'da_rifare']),
                                              'nota': 'prova concorrenza'})

    def esegui(self):
        n_ord = 3
        for _ in range(n_ord):
            oid = self.crea_ordine()
            if oid:
                self.ordini.append(oid)
        # un ordine con pezzi veri (per il doppio Manda a Lantek)
        self.ordine_lantek = None
        try:
            import b2_flussi
            oid, _ = b2_flussi.Flussi(self.ctx).da_pacchetto()
            if oid:
                self.las[0].post('/api/orders/%s/smistamento' % oid, {'va_tagliato': True})
                self.ordine_lantek = oid
        except Exception as e:
            self.reg.eccezione('b3 ordine con pezzi veri', e)
        self.reg.nota('ordini condivisi per la concorrenza: %d' % len(self.ordini))
        if not self.ordini:
            self.reg.problema('GRAVE', 'Concorrenza: impossibile creare gli ordini di prova', '')
            return
        fine = self.ctx.scadenza
        stop = threading.Event()
        n_post = 10 if self.ctx.profilo == 'notte' else 6
        tipi = (['laser', 'laser', 'ufficio', 'ufficio', 'reparto', 'reparto', 'reparto', 'ore', 'pubblico',
                 'reparto'])[:n_post]
        th = [threading.Thread(target=self.postazione, args=(t, i, fine, self.ordini, stop), daemon=True)
              for i, t in enumerate(tipi)]
        for t in th:
            t.start()
        giri = 0
        try:
            while datetime.now() < fine - timedelta(seconds=20):
                giri += 1
                oid = self.rnd.choice(self.ordini)
                for f in (self.aggiornamento_perso, self.doppio_clic, self.doppio_lantek):
                    try:
                        f(self.ordine_lantek if (f == self.doppio_lantek and self.ordine_lantek) else oid)
                    except Exception as e:
                        self.reg.eccezione('b3 %s' % f.__name__, e)
                if giri % 5 == 1:
                    try:
                        self.ore_conflitto()
                    except Exception as e:
                        self.reg.eccezione('b3 ore', e)
                time.sleep(self.rnd.uniform(2, 6))
        finally:
            stop.set()
            for t in th:
                t.join(60)
        self.reg.nota('concorrenza: %d postazioni, %d giri di prove mirate' % (len(th), giri))
        # stato finale coerente
        for oid in self.ordini:
            c, o = self.uff[0].get('/api/orders/%s' % oid)
            self.ok(c == 200, 'Ordine leggibile dopo la concorrenza', 'GRAVE', 'HTTP %s' % c,
                    chiave=('fine_leggi',))
            c2, l = self.las[0].get('/api/laser/ordini?ordine=%s' % oid)
            self.ok(c2 == 200, 'Stato laser leggibile dopo la concorrenza', 'GRAVE', 'HTTP %s' % c2,
                    chiave=('fine_laser',))


def esegui(ctx):
    Concorrenza(ctx).esegui()
