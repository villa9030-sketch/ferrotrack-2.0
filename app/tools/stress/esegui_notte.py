# -*- coding: utf-8 -*-
"""Prova notturna di stress e affidabilita' di FerroTrack, su una COPIA completa.

    python esegui_notte.py --profilo notte --durata-ore 13
    python esegui_notte.py --profilo rapido --durata-ore 0.25

Tutto avviene in ferrotrack-stress/ e nella cartella della prova
banco_motore/stress/<data_ora>/ (REPORT.md, risultati.json, log, prove).
La produzione (ferrotrack-2.0) si legge soltanto. Lantek: solo stub.
Arresto forzato alle 07:30 comunque vada.
"""
import argparse
import importlib
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

QUI = os.path.dirname(os.path.abspath(__file__))
if QUI not in sys.path:
    sys.path.insert(0, QUI)

from comune import CARTELLA_RUNS, Registro, priorita_bassa  # noqa: E402
import preparazione  # noqa: E402
from gestione_server import Server, porta_libera  # noqa: E402

# (modulo, nome, minuti notte, minuti rapido) in ordine di priorita'
BLOCCHI = [
    ('b1_inventario', '1. Inventario e pagine', 45, 4),
    ('b2_flussi', '2. Ordini dall\'inizio alla fine', 180, 4),
    ('b3_concorrenza', '3. Piu\' postazioni insieme', 60, 2),
    ('b4_guasti', '4. Guasti simulati', 90, 3),
    ('b5_durata', '5. Carico prolungato', 180, 2),
]


class Contesto:
    pass


def prossime_0730(da):
    t = da.replace(hour=7, minute=30, second=0, microsecond=0)
    if t <= da:
        t += timedelta(days=1)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--durata-ore', type=float, default=13)
    ap.add_argument('--profilo', choices=('notte', 'rapido'), default='notte')
    ap.add_argument('--porta', type=int, default=5060)
    ap.add_argument('--senza-uploads', action='store_true', help='non ricopiare uploads (solo per prove del banco)')
    ap.add_argument('--blocchi', default='', help='es. 1,2 (default: tutti)')
    a = ap.parse_args()
    priorita_bassa()
    os.environ['PYTHONIOENCODING'] = 'utf-8'

    inizio = datetime.now()
    run_dir = os.path.join(CARTELLA_RUNS, inizio.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(run_dir, exist_ok=True)
    tmp = os.path.join(run_dir, 'tmp')
    os.makedirs(tmp, exist_ok=True)
    for k in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[k] = tmp
    import tempfile
    tempfile.tempdir = tmp

    reg = Registro(run_dir)
    fine = inizio + timedelta(hours=a.durata_ore)
    stop_duro = min(prossime_0730(inizio), fine + timedelta(minutes=45))
    reg.log('Prova %s, profilo %s, durata %.2f h, fine prevista %s, arresto forzato %s'
            % (run_dir, a.profilo, a.durata_ore, fine.strftime('%H:%M'), stop_duro.strftime('%d/%m %H:%M')))

    ctx = Contesto()
    ctx.reg, ctx.run_dir, ctx.profilo, ctx.fine = reg, run_dir, a.profilo, fine
    ctx.server = None
    ctx.porte_extra = list(range(a.porta + 1, 5070))
    meta = {'inizio': inizio.isoformat(timespec='seconds'), 'profilo': a.profilo, 'durata_ore': a.durata_ore,
            'run_dir': run_dir}

    finito = threading.Event()
    report_lock = threading.Lock()

    def chiudi(motivo):
        with report_lock:
            meta['fine'] = datetime.now().isoformat(timespec='seconds')
            meta['motivo_fine'] = motivo
            try:
                if ctx.server:
                    reg.campione(rss_mb=ctx.server.rss_mb(), evento='fine')
            except Exception:
                pass
            for s in list(getattr(ctx, 'tutti_server', [])):
                try:
                    s.uccidi()
                except Exception:
                    pass
            try:
                verifica_produzione(ctx, impronta_prima)
            except Exception as e:
                reg.eccezione('verifica produzione', e)
            reg.salva()
            try:
                import report
                report.scrivi(ctx, meta)
            except Exception:
                with open(os.path.join(run_dir, 'REPORT.md'), 'w', encoding='utf-8') as f:
                    f.write('# Prova notturna FerroTrack\n\nIl report completo non e\' stato generato:\n\n```\n'
                            + traceback.format_exc() + '\n```\nVedi risultati.json.\n')

    def guardiano():
        if not finito.wait(max(1, (stop_duro - datetime.now()).total_seconds())):
            reg.log('ARRESTO FORZATO (orario limite)')
            for b in reg.blocchi.values():
                if b.get('stato') == 'in corso':
                    b['stato'] = 'interrotto (orario limite)'
            chiudi('arresto forzato alle %s' % stop_duro.strftime('%H:%M'))
            os._exit(3)
    threading.Thread(target=guardiano, daemon=True).start()

    impronta_prima = {}
    try:
        impronta_prima = preparazione.impronta_produzione()
        with open(os.path.join(run_dir, 'impronta_produzione_prima.json'), 'w', encoding='utf-8') as f:
            json.dump(impronta_prima, f, indent=1)
        reg.inizio_blocco('0. Preparazione copia')
        for p in range(a.porta, 5070):
            if not porta_libera(p):
                raise SystemExit('porta %d occupata: un\'altra prova e\' in corso?' % p)
        ctx.db, ctx.cfg = preparazione.prepara(run_dir, reg, copia_upl=not a.senza_uploads)
        ctx.server = Server(a.porta, run_dir, reg)
        ctx.tutti_server = [ctx.server]
        ctx.server.avvia()
        reg.fine_blocco('0. Preparazione copia', 'completato')

        scelti = {int(x) for x in a.blocchi.split(',') if x.strip()} if a.blocchi else None
        n_blocchi = len(BLOCCHI)
        for i, (mod, nome, min_notte, min_rapido) in enumerate(BLOCCHI, 1):
            if scelti and i not in scelti:
                continue
            ora_ = datetime.now()
            if ora_ >= fine:
                reg.inizio_blocco(nome)
                reg.fine_blocco(nome, 'saltato (tempo finito)')
                continue
            minuti = min_notte if a.profilo == 'notte' else min_rapido
            # il tempo rimasto si divide fra i blocchi che mancano, senza superare il loro budget
            rimasti = (fine - ora_).total_seconds() / 60
            minuti = min(minuti, max(1, rimasti - sum(
                (b[2] if a.profilo == 'notte' else b[3]) * 0.3 for b in BLOCCHI[i:])))
            ctx.scadenza = ora_ + timedelta(minutes=minuti)
            reg.inizio_blocco(nome)
            try:
                m = importlib.import_module(mod)
            except ImportError as e:
                reg.fine_blocco(nome, 'NON PRESENTE (da fare)', str(e))
                continue
            try:
                if not ctx.server.vivo():
                    ctx.server.uccidi()
                    ctx.server.avvia()
                m.esegui(ctx)
                reg.fine_blocco(nome)
            except Exception as e:
                reg.problema('LIEVE', 'Il blocco "%s" si e\' interrotto per un errore del banco' % nome,
                             '%s: %s' % (type(e).__name__, e), prova={'traceback': traceback.format_exc()})
                reg.fine_blocco(nome, 'interrotto da errore', str(e)[:200])
            reg.salva()

        # tempo avanzato: altri giri di ordini e di carico
        giro = 0
        while a.profilo == 'notte' and datetime.now() + timedelta(minutes=20) < fine:
            giro += 1
            for mod, nome in (('b2_flussi', '2. Ordini (giro extra %d)' % giro),
                              ('b5_durata', '5. Carico (giro extra %d)' % giro)):
                if datetime.now() + timedelta(minutes=10) >= fine:
                    break
                ctx.scadenza = min(fine, datetime.now() + timedelta(minutes=60))
                reg.inizio_blocco(nome)
                try:
                    if not ctx.server.vivo():
                        ctx.server.uccidi()
                        ctx.server.avvia()
                    importlib.import_module(mod).esegui(ctx)
                    reg.fine_blocco(nome)
                except Exception as e:
                    reg.fine_blocco(nome, 'interrotto da errore', str(e)[:200])
    except SystemExit as e:
        reg.problema('BLOCCANTE', 'Prova interrotta prima di partire', str(e))
    except Exception as e:
        reg.problema('BLOCCANTE', 'Prova interrotta da un errore del banco', '%s: %s' % (type(e).__name__, e),
                     prova={'traceback': traceback.format_exc()})
    finally:
        finito.set()
        chiudi('fine normale')
        reg.log('REPORT:', os.path.join(run_dir, 'REPORT.md'))


def verifica_produzione(ctx, prima):
    """La produzione non deve essere stata toccata dalla prova."""
    reg = ctx.reg
    dopo = preparazione.impronta_produzione()
    with open(os.path.join(ctx.run_dir, 'impronta_produzione_dopo.json'), 'w', encoding='utf-8') as f:
        json.dump(dopo, f, indent=1)
    viol = os.path.join(ctx.run_dir, 'guardia_violazioni.log')
    if os.path.isfile(viol) and os.path.getsize(viol) > 0:
        with open(viol, encoding='utf-8') as f:
            righe = f.read().splitlines()
        reg.problema('GRAVE', 'L\'app ha provato a scrivere FUORI dalla copia (bloccato dalla guardia)',
                     '%d tentativi, es.: %s' % (len(righe), righe[:3]),
                     passi='vedi guardia_violazioni.log', blocco='Sicurezza', chiave=('guardia',))
    diversi = [k for k in prima if prima.get(k) != dopo.get(k)]
    meta = {'cambiati_durante_la_prova': diversi}
    # Il server vero gira: il suo DB cambia da solo. Si segnala solo, senza colpe.
    with open(os.path.join(ctx.run_dir, 'verifica_produzione.json'), 'w', encoding='utf-8') as f:
        json.dump({'prima': prima, 'dopo': dopo, **meta}, f, indent=1)
    ctx.produzione_cambiata = diversi


if __name__ == '__main__':
    main()
