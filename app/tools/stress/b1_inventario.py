# -*- coding: utf-8 -*-
"""Blocco 1: inventario e fumo.

- tutte le pagine di frontend/*.html e tutte le rotte Flask (da rotte.json,
  scritto dal server all'avvio);
- ogni pagina si apre senza errori JavaScript da ogni tipo di postazione
  (ufficio, laser - che fa anche da capo officina -, tablet reparto,
  timbratrice, commerciale, amministratore). Playwright SOLO headless;
- ogni GET risponde senza 500 con ID validi presi dalla copia del database,
  e con ID inesistenti (deve dire 404, non 500).
"""
import json
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime

from comune import APP, Client, contesto_browser

STAZIONI = ['admin', 'ufficio', 'laser', 'reparto', 'ore', 'commerciale']
PROPRIA = {
    'laser.html': 'laser', 'laser-vecchio.html': 'laser', 'capo-officina.html': 'laser',
    'impiegata.html': 'ufficio', 'archivio.html': 'ufficio', 'ufficio-ore.html': 'ufficio',
    'admin.html': 'admin', 'preventivi.html': 'commerciale', 'dxf-editor.html': 'commerciale',
    'operaio-info.html': 'reparto', 'ore.html': 'ore',
}
NON_AUTONOME = {'preview-dxf.html', 'preview-step.html', 'fold3d.html'}
# GET che non sono letture innocue o non hanno senso da soli
SALTA = {'/<path:filename>', '/'}
FINTO = '00000000-0000-4000-8000-000000000000'


def pagine():
    d = os.path.join(APP, 'frontend')
    return sorted(f for f in os.listdir(d) if f.endswith('.html') and f not in NON_AUTONOME)


def campioni(db, n_ordini):
    """ID validi dalla copia del database."""
    c = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
    out = {'ordini': [], 'preventivi': []}
    for (oid,) in c.execute('select id from orders where coalesce(is_deleted,0)=0 order by data_ricezione desc '
                            'limit ?', (n_ordini,)):
        files = [r[0] for r in c.execute("select filename from order_files where order_id=? and upper(file_type) "
                                          "in ('DXF','DWG') limit 2", (oid,))]
        out['ordini'].append({'order_id': oid, 'dxf': files})
    for (pid,) in c.execute("select id from preventivi where coalesce(is_deleted,0)=0 order by data_creazione desc "
                            "limit ?", (n_ordini,)):
        dxf = [r[0] for r in c.execute('select dxf_filename from preventivo_articoli where preventivo_id=? '
                                        'and dxf_filename is not null limit 2', (pid,))]
        out['preventivi'].append({'preventivo_id': pid, 'dxf': dxf})
    out['user_id'] = [r[0] for r in c.execute('select id from users limit 2')]
    out['notification_id'] = [r[0] for r in c.execute('select id from notifications limit 2')]
    out['token_id'] = [r[0] for r in c.execute('select id from device_tokens limit 1')]
    c.close()
    return out


def clienti(ctx):
    if getattr(ctx, 'clienti_b1', None):
        return ctx.clienti_b1
    cl = {}
    for st in STAZIONI:
        try:
            cl[st] = Client(ctx.server.base, ctx.reg, 'b1-' + st, stazione=st)
        except Exception as e:
            ctx.reg.problema('BLOCCANTE' if st in ('ufficio', 'laser') else 'GRAVE',
                             'Non si riesce a entrare dalla postazione %s' % st, str(e)[:400],
                             passi='POST /api/accesso/registra + /api/accesso/entra')
    ctx.clienti_b1 = cl
    return cl


def chiama_con_stazione(ctx, cl, percorso, preferita=None):
    """Prova le postazioni finche' una e' ammessa. Torna (stazione, codice, dati)."""
    ordine = ([preferita] if preferita else []) + [s for s in STAZIONI if s != preferita]
    ultimo = None
    for st in ordine:
        c = cl.get(st)
        if not c:
            continue
        cod, d = c.get(percorso)
        ultimo = (st, cod, d)
        if cod == 403 and st == 'admin':
            c.eleva()
            cod, d = c.get(percorso)
            ultimo = (st, cod, d)
        if cod not in (401, 403):
            return ultimo
    return ultimo


def riempi(regola, valori):
    """Sostituisce <arg> / <path:arg> con i valori; None se manca un valore."""
    import re
    def sost(m):
        nome = m.group(2)
        v = valori.get(nome)
        if v is None:
            raise KeyError(nome)
        return urllib.parse.quote(str(v), safe='')
    try:
        return re.sub(r'<(?:(\w+):)?(\w+)>', sost, regola)
    except KeyError:
        return None


def prova_get(ctx, cl):
    reg = ctx.reg
    with open(os.path.join(ctx.run_dir, 'rotte.json'), encoding='utf-8') as f:
        rotte = json.load(f)
    get = [r for r in rotte if 'GET' in r['metodi'] and r['regola'] not in SALTA
           and not r['regola'].startswith('/static')]
    reg.nota('%d rotte Flask, %d GET da provare' % (len(rotte), len(get)))
    n_ord = 3 if ctx.profilo == 'rapido' else 12
    cam = campioni(ctx.db, n_ord)

    # articoli degli ordini (dal laser)
    for o in cam['ordini']:
        _, cod, d = chiama_con_stazione(ctx, cl, '/api/orders/%s/laser' % o['order_id'], 'laser')
        art = [p.get('articolo_id') for p in (d.get('pezzi') or [])] if isinstance(d, dict) else []
        o['articoli'] = [a for a in art if a][:2]

    combinazioni = []
    for r in get:
        args = set(r['argomenti'])
        if not args:
            combinazioni.append((r, {}))
            continue
        if args <= {'order_id', 'articolo_id', 'filename'}:
            for o in cam['ordini']:
                v = {'order_id': o['order_id']}
                if 'filename' in args:
                    if not o['dxf']:
                        continue
                    v['filename'] = o['dxf'][0]
                if 'articolo_id' in args:
                    if not o.get('articoli'):
                        continue
                    for a in o['articoli']:
                        combinazioni.append((r, dict(v, articolo_id=a)))
                    continue
                combinazioni.append((r, v))
        elif args <= {'preventivo_id', 'filename'}:
            for p in cam['preventivi']:
                v = {'preventivo_id': p['preventivo_id']}
                if 'filename' in args:
                    if not p['dxf']:
                        continue
                    v['filename'] = p['dxf'][0]
                combinazioni.append((r, v))
        else:
            for nome in args:
                for x in cam.get(nome, [])[:1]:
                    combinazioni.append((r, {nome: x}))
        # sempre: ID inesistenti -> 404/400, mai 500
        combinazioni.append((r, {a: FINTO for a in args} | {'_finto': True}))

    reg.nota('%d chiamate GET preparate' % len(combinazioni))
    stazione_buona = {}
    for r, v in combinazioni:
        if datetime.now() > ctx.scadenza:
            reg.nota('GET: tempo del blocco finito, provate solo in parte')
            break
        finto = v.pop('_finto', False)
        perc = riempi(r['regola'], v)
        if perc is None:
            continue
        pref = stazione_buona.get(r['regola'])
        st, cod, d = chiama_con_stazione(ctx, cl, perc, pref)
        if cod not in (401, 403):
            stazione_buona[r['regola']] = st
        # il 500 lo annota gia' il client (con la prova): qui solo il conteggio
        with reg.lock:
            reg.blocchi[reg.blocco_attuale]['ok' if (cod < 500 or cod in (502, 503)) else 'ko'] += 1
        if finto and cod == 200 and isinstance(d, dict) and d.get('success') is not False and r['argomenti']:
            reg.controllo(False, 'GET %s con un ID che non esiste risponde 200 invece di 404' % r['regola'],
                          'LIEVE', str(d)[:200], passi='GET %s' % perc, chiave=('get404', r['regola']))
        if cod in (401, 403) and not finto:
            reg.controllo(False, 'GET %s rifiutato da tutte le postazioni' % r['regola'], 'LIEVE',
                          'HTTP %s %s' % (cod, str(d)[:200]), passi='GET %s' % perc, chiave=('get403', r['regola']))


def prova_pagine(ctx):
    reg = ctx.reg
    from playwright.sync_api import sync_playwright
    base = ctx.server.base
    elenco = pagine()
    reg.nota('%d pagine: %s' % (len(elenco), ', '.join(elenco)))
    combin = []
    for pg in elenco:
        propria = PROPRIA.get(pg)
        if ctx.profilo == 'rapido':
            st = [propria or 'nessuna'] + (['reparto'] if propria != 'reparto' else ['ufficio'])
        else:
            st = [propria or 'nessuna'] + [s for s in STAZIONI if s != propria]
        combin += [(pg, s) for s in st]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=['--disable-gpu', '--no-first-run',
                                                         '--explicitly-allowed-ports=' + ','.join(str(x) for x in range(5060, 5070))])
        try:
            for pagina, st in combin:
                if datetime.now() > ctx.scadenza:
                    reg.nota('pagine: tempo del blocco finito, provate solo in parte')
                    break
                errori, console, risposte_500 = [], [], []
                ctx_b = b.new_context(viewport={'width': 1440, 'height': 900})
                try:
                    if st != 'nessuna':
                        contesto_browser(ctx_b, base, reg, st, 50)
                    page = ctx_b.new_page()
                    page.on('pageerror', lambda e: errori.append(str(e)[:300]))
                    page.on('console', lambda m: console.append(m.text[:200]) if m.type == 'error' else None)
                    page.on('response', lambda r: risposte_500.append('%s %s' % (r.status, r.url))
                            if r.status >= 500 else None)
                    t0 = time.perf_counter()
                    risp = page.goto(base + '/' + pagina, timeout=60000)
                    try:
                        page.wait_for_load_state('networkidle', timeout=25000)
                    except Exception:
                        pass
                    # tempo di caricamento misurato PRIMA dell'attesa fissa
                    # (quella serve solo a raccogliere gli errori JS tardivi)
                    ms = (time.perf_counter() - t0) * 1000
                    page.wait_for_timeout(1500)
                    reg.misura('PAGINA %s' % pagina, ms, risp.status if risp else 0)
                    propria = (st == (PROPRIA.get(pagina) or 'nessuna'))
                    stato = risp.status if risp else 0
                    reg.controllo(stato in (200, 302, 401, 403) and (stato == 200 or not propria),
                                  'La pagina %s si apre dalla sua postazione' % pagina,
                                  'BLOCCANTE' if propria else 'MEDIO', 'HTTP %s da %s' % (stato, st),
                                  passi='aprire /%s dalla postazione %s' % (pagina, st),
                                  chiave=('pagina_http', pagina, propria))
                    reg.controllo(not errori,
                                  'Errori JavaScript aprendo %s%s' % (pagina, '' if propria else
                                                                     ' da una postazione diversa'),
                                  'GRAVE' if propria else 'MEDIO', '%s: %s' % (st, errori[:2]),
                                  passi='aprire /%s dalla postazione %s (headless)' % (pagina, st),
                                  prova={'pagina': pagina, 'stazione': st, 'errori': errori, 'console': console},
                                  chiave=('pagina_js', pagina, propria))
                    reg.controllo(not risposte_500, 'La pagina %s riceve errori 500 dal server' % pagina, 'GRAVE',
                                  '%s: %s' % (st, risposte_500[:3]), passi='aprire /%s da %s' % (pagina, st),
                                  chiave=('pagina_500', pagina))
                except Exception as e:
                    reg.controllo(False, 'La pagina %s non si carica (timeout o errore)' % pagina, 'MEDIO',
                                  '%s: %s' % (st, str(e)[:300]), passi='aprire /%s da %s' % (pagina, st),
                                  chiave=('pagina_exc', pagina))
                finally:
                    try:
                        ctx_b.close()
                    except Exception:
                        pass
        finally:
            b.close()


def esegui(ctx):
    cl = clienti(ctx)
    if not cl.get('ufficio') or not cl.get('laser'):
        raise RuntimeError('postazioni ufficio/laser non disponibili')
    fine = ctx.scadenza
    ctx.scadenza = datetime.now() + (fine - datetime.now()) * 0.5
    try:
        prova_get(ctx, cl)
    except Exception as e:
        ctx.reg.eccezione('b1 GET', e)
    ctx.scadenza = fine
    try:
        prova_pagine(ctx)
    except Exception as e:
        ctx.reg.eccezione('b1 pagine', e)
