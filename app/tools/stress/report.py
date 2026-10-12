# -*- coding: utf-8 -*-
"""REPORT.md della prova notturna: in italiano semplice per Stefano, dettagli sotto."""
import json
import os

from comune import GRAVITA, prestazioni

SPIEGA = {
    'BLOCCANTE': 'ferma il lavoro: va sistemato prima di tutto',
    'GRAVE': 'puo\' far perdere dati o lavoro, o mostrare errori al banco',
    'MEDIO': 'fastidio o rischio limitato',
    'LIEVE': 'dettaglio, o problema del banco di prova stesso',
}


def _md(s):
    return str(s if s is not None else '').replace('|', '/').replace('\n', ' ')


def scrivi(ctx, meta):
    reg = ctx.reg
    probs = sorted(reg.problemi, key=lambda p: (GRAVITA.index(p['gravita']) if p['gravita'] in GRAVITA else 9,
                                                -p['volte']))
    perf = prestazioni(reg.tempi, reg.codici)
    L = []
    L.append('# Prova notturna di FerroTrack')
    L.append('')
    L.append('Copia completa (ferrotrack-stress), Lantek finto: nessun programma di Lantek lanciato, '
             'nessun dato vero toccato.')
    L.append('')
    L.append('- Inizio: %s  -  fine: %s (%s)' % (meta.get('inizio'), meta.get('fine'), meta.get('motivo_fine')))
    L.append('- Profilo: %s, durata prevista %.2f ore' % (meta.get('profilo'), meta.get('durata_ore') or 0))
    conta = {g: sum(1 for p in probs if p['gravita'] == g) for g in GRAVITA}
    L.append('- Problemi trovati: ' + ', '.join('%s %d' % (g, conta[g]) for g in GRAVITA))
    L.append('')
    L.append('## In breve, fase per fase')
    L.append('')
    L.append('| Fase | Esito | Controlli OK | Problemi | Note |')
    L.append('|---|---|---|---|---|')
    for nome, b in reg.blocchi.items():
        n_prob = sum(1 for p in probs if p['blocco'] == nome)
        esito = b.get('stato')
        if esito in ('completato',):
            esito = 'OK' if not n_prob else 'problemi'
        L.append('| %s | %s | %s | %s | %s |' % (_md(nome), _md(esito), b.get('ok', 0), n_prob,
                                                 _md('; '.join(b.get('note', [])[:3]))[:300]))
    L.append('')
    cambiati = getattr(ctx, 'produzione_cambiata', None)
    L.append('## La produzione e\' stata toccata?')
    L.append('')
    viol = os.path.join(ctx.run_dir, 'guardia_violazioni.log')
    if os.path.isfile(viol) and os.path.getsize(viol):
        L.append('- **ATTENZIONE**: l\'app di prova ha tentato scritture fuori dalla copia, BLOCCATE dalla guardia '
                 '(vedi guardia_violazioni.log).')
    else:
        L.append('- Nessun tentativo di scrittura fuori dalla copia.')
    if cambiati:
        L.append('- Cambiati durante la prova (il server vero lavora per conto suo, e\' normale per il suo '
                 'database): ' + ', '.join(os.path.basename(c) or c for c in cambiati))
    else:
        L.append('- Database, uploads e DISEGNI LANTEK di produzione: invariati.')
    L.append('')
    L.append('## Problemi, dal piu\' grave')
    L.append('')
    if not probs:
        L.append('Nessun problema trovato.')
    for i, p in enumerate(probs, 1):
        L.append('### %d. [%s] %s' % (i, p['gravita'], p['descrizione']))
        L.append('')
        L.append('- Fase: %s  -  quante volte: %d  -  prima volta: %s' % (p['blocco'], p['volte'], p['quando']))
        L.append('- Gravita\': %s' % SPIEGA.get(p['gravita'], ''))
        if p.get('dettagli'):
            L.append('- Cosa e\' successo: %s' % _md(p['dettagli'])[:700])
        if p.get('passi'):
            L.append('- Come rifarlo: %s' % _md(p['passi'])[:500])
        if p.get('prova'):
            L.append('- Prova: `%s`' % p['prova'])
        for e in p.get('esempi', [])[:3]:
            L.append('  - altro caso: %s' % _md(e)[:250])
        L.append('')
    L.append('## Prestazioni')
    L.append('')
    lenti = sorted(((k, v) for k, v in perf.items() if (v['p95_ms'] or 0) > 1000), key=lambda kv: -kv[1]['p95_ms'])
    L.append('Chiamate lente (p95 oltre 1 secondo): %d' % len(lenti))
    L.append('')
    L.append('| Chiamata | N | p50 ms | p95 ms | max ms | errori |')
    L.append('|---|---|---|---|---|---|')
    for k, v in sorted(perf.items(), key=lambda kv: -(kv[1]['p95_ms'] or 0))[:60]:
        L.append('| %s | %d | %.0f | %.0f | %.0f | %d |' % (_md(k), v['n'], v['p50_ms'] or 0, v['p95_ms'] or 0,
                                                         v['max_ms'] or 0, v['errori_5xx_o_rete']))
    L.append('')
    serie = [s for s in reg.serie if s.get('rss_mb')]
    if serie:
        L.append('Memoria del server (RSS): inizio %.0f MB, fine %.0f MB, massimo %.0f MB su %d campioni.'
                 % (serie[0]['rss_mb'], serie[-1]['rss_mb'], max(s['rss_mb'] for s in serie), len(serie)))
        db = [s for s in reg.serie if s.get('db_mb')]
        if db:
            L.append('Database: da %.1f MB a %.1f MB.' % (db[0]['db_mb'], db[-1]['db_mb']))
        L.append('')
    L.append('## Dettagli tecnici')
    L.append('')
    L.append('- Cartella della prova: `%s`' % ctx.run_dir)
    L.append('- File: risultati.json (tutto), harness.log, server_*.log, lantek_stub/invii.jsonl (file che '
             'sarebbero andati a Lantek), prove/ (una per problema), guardia_violazioni.log.')
    for nome, b in reg.blocchi.items():
        L.append('- %s: %s -> %s, controlli ok %s / falliti %s' % (nome, b.get('inizio'), b.get('fine'),
                                                                 b.get('ok'), b.get('ko')))
        for n in b.get('note', [])[3:20]:
            L.append('  - %s' % _md(n)[:300])
    with open(os.path.join(ctx.run_dir, 'REPORT.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
