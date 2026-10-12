# -*- coding: utf-8 -*-
"""Blocco 5: carico prolungato e realistico.

Pagine aperte che interrogano il server come fanno davvero (laser ogni 5 s,
ufficio ogni 10/30 s, tablet ogni 60 s, dashboard ogni 10 s), e ogni tanto
un ordine nuovo che fa tutto il giro. Ogni minuto: memoria del server (RSS),
CPU, dimensione del database, errori. Alla fine: perdite di memoria (memoria
che cresce sempre) e chiamate lente (p95 oltre 1 s).
"""
import os
import random
import threading
import time
from datetime import datetime, timedelta

from comune import Client, percentile
from b3_concorrenza import POLL

STAZIONI = ['laser', 'laser', 'ufficio', 'ufficio', 'reparto', 'reparto', 'reparto', 'ore', 'pubblico']


def _pendenza(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def esegui(ctx):
    reg = ctx.reg
    base = ctx.server.base
    fine = ctx.scadenza
    stop = threading.Event()
    inizio_misure = len(reg.serie)
    tempi_prima = {k: len(v) for k, v in reg.tempi.items()}

    def stazione(tipo, n):
        try:
            cl = Client(base, reg, 'b5-%s%d' % (tipo, n), None if tipo == 'pubblico' else tipo,
                        persona_n=40 + n)
        except Exception as e:
            reg.eccezione('b5 postazione %s' % tipo, e)
            return
        rnd = random.Random(n)
        prossimo = {p: time.time() + rnd.uniform(0, s) for p, s in POLL.get(tipo, [])}
        while not stop.is_set() and datetime.now() < fine:
            ora = time.time()
            for p, s in POLL.get(tipo, []):
                if ora >= prossimo[p]:
                    cl.get(p)
                    prossimo[p] = ora + s
            if tipo == 'laser' and rnd.random() < 0.02:
                c, d = cl.get('/api/laser/ordini')
                ordini = list(((d or {}).get('ordini') or {}).keys()) if isinstance(d, dict) else []
                if ordini:
                    cl.get('/api/orders/%s/laser' % rnd.choice(ordini))
            stop.wait(1.0)

    th = [threading.Thread(target=stazione, args=(t, i), daemon=True) for i, t in enumerate(STAZIONI)]
    for t in th:
        t.start()

    # ordini nuovi ogni tanto (giro completo)
    def ordini_periodici():
        try:
            import b2_flussi
            f = b2_flussi.Flussi(ctx)
        except Exception as e:
            reg.eccezione('b5 flussi', e)
            return
        i = 0
        intervallo = 300 if ctx.profilo == 'notte' else 45
        while not stop.wait(intervallo) and datetime.now() < fine - timedelta(minutes=3 if ctx.profilo == 'notte' else 0.5):
            try:
                f.un_ordine(1000 + i)
            except Exception as e:
                reg.eccezione('b5 ordine periodico', e)
            i += 1
        reg.nota('carico: %d ordini completi durante il carico' % i)
    to = threading.Thread(target=ordini_periodici, daemon=True)
    to.start()

    passo = 60 if ctx.profilo == 'notte' else 15
    t0 = time.time()
    cpu0 = ctx.server.cpu_s()
    while datetime.now() < fine:
        if not ctx.server.vivo():
            reg.problema('BLOCCANTE', 'Il server si e\' fermato da solo durante il carico prolungato',
                         'processo terminato', 'vedi server_*.log', chiave=('b5_morto',))
            try:
                ctx.server.uccidi()
                ctx.server.avvia()
            except Exception as e:
                reg.eccezione('b5 riavvio', e)
                break
        db = ctx.db
        dbmb = sum(os.path.getsize(db + c) for c in ('', '-wal') if os.path.exists(db + c)) / 1e6
        cpu = ctx.server.cpu_s()
        reg.campione(rss_mb=ctx.server.rss_mb(), db_mb=round(dbmb, 2), cpu_s=cpu,
                     minuti=round((time.time() - t0) / 60, 1), fase='carico')
        reg.salva()
        stop.wait(passo)
    stop.set()
    for t in th:
        t.join(30)
    to.join(120)

    # analisi
    serie = [s for s in reg.serie[inizio_misure:] if s.get('rss_mb') and s.get('fase') == 'carico']
    if len(serie) >= 5:
        xs = [s['minuti'] / 60 for s in serie]
        ys = [s['rss_mb'] for s in serie]
        pend = _pendenza(xs, ys)
        crescita = ys[-1] - ys[0]
        # "sempre in crescita": la seconda meta' sta sopra la prima
        meta = len(ys) // 2
        sempre = min(ys[meta:]) > max(ys[:max(1, meta // 2)])
        reg.nota('memoria server: %.0f -> %.0f MB, pendenza %.1f MB/ora su %d campioni'
                 % (ys[0], ys[-1], pend, len(ys)))
        reg.controllo(not (pend > 30 and crescita > 80 and sempre),
                      'Memoria del server in crescita continua (possibile perdita)', 'MEDIO',
                      '%.0f -> %.0f MB, %.1f MB/ora' % (ys[0], ys[-1], pend), 'carico prolungato, vedi risultati.json serie',
                      chiave=('leak',))
        dbs = [s['db_mb'] for s in serie if s.get('db_mb')]
        if dbs:
            reg.nota('database: %.1f -> %.1f MB durante il carico' % (dbs[0], dbs[-1]))
        if cpu0 is not None and serie[-1].get('cpu_s') is not None:
            ore = max(1e-6, (time.time() - t0))
            reg.nota('CPU media del server durante il carico: %.0f%% di un core'
                     % (100 * (serie[-1]['cpu_s'] - cpu0) / ore))
    # chiamate lente durante il carico
    lente = []
    for k, v in reg.tempi.items():
        nuovi = v[tempi_prima.get(k, 0):]
        if len(nuovi) >= 10:
            p95 = percentile(nuovi, 95)
            if p95 > 1000:
                lente.append((k, p95, len(nuovi)))
    for k, p95, n in sorted(lente, key=lambda x: -x[1])[:15]:
        reg.problema('MEDIO' if p95 > 3000 else 'LIEVE', 'Chiamata lenta durante il carico: %s' % k,
                     'p95 %.0f ms su %d chiamate' % (p95, n), 'carico prolungato con %d postazioni' % len(STAZIONI),
                     chiave=('lento', k))
    tot = sum(sum(c.values()) for c in reg.codici.values())
    err = sum(n for c in reg.codici.values() for cod, n in c.items() if cod == 0 or cod >= 500)
    reg.nota('chiamate totali finora %d, errori 5xx/rete %d (%.2f%%)' % (tot, err, 100.0 * err / max(1, tot)))
