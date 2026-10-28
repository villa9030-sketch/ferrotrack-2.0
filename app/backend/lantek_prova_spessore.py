"""PROVA della correzione dello spessore in Lantek (28/10/2026, punto 5 della
proposta del 27/10; la "proposta 2" del 12/10, lantek_correzioni.py).

La domanda: se FerroTrack reimporta con Procesos (come i pezzi nuovi di
"Manda a Lantek") un pezzo che in Lantek c'e' gia', con un altro spessore,
Lantek lo AGGIORNA, ne fa un DOPPIONE o lo RIFIUTA? Finche' non si sa, la
correzione automatica non si attiva.

Si prova su un pezzo di prova, mai su un pezzo vero, in due clic di Stefano:
  1. "crea":       FT-PROVA-30, FERRO 2 mm, un rettangolo 200 x 100;
  2. "reimporta":  lo stesso pezzo, FERRO 3 mm.
Prima e dopo ogni passo si fotografa l'archivio pezzi di Lantek (SOLA
LETTURA: tutti i pezzi il cui codice comincia per FT-PROVA-30) e si dice cosa
e' successo. A scrivere in Lantek e' solo Procesos.exe di Lantek, lanciato
dall'app dopo il clic. L'esito resta in uploads/lantek_import/prova_spessore.json.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

CODICE = 'FT-PROVA-30'
MATERIALE = 'FERRO'
SPESSORE = {'crea': 2.0, 'reimporta': 3.0}


def _file_esito(cartella: str) -> str:
    return os.path.join(cartella, 'prova_spessore.json')


def storico(cartella: str) -> list:
    try:
        with open(_file_esito(cartella), encoding='utf-8') as f:
            return json.load(f) or []
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning('esito della prova spessore non letto', exc_info=True)
        return []


def _scrivi(cartella: str, voce: dict) -> None:
    v = storico(cartella) + [voce]
    os.makedirs(cartella, exist_ok=True)
    with open(_file_esito(cartella), 'w', encoding='utf-8') as f:
        json.dump(v, f, ensure_ascii=False, indent=1, default=str)


def fotografia() -> dict:
    """{'ok', 'errore', 'pezzi': [{codice, tipo, materiale, spessore, creato}]}
    dall'archivio pezzi di Lantek. SOLA LETTURA."""
    from . import lantek as lt
    cfg = lt._config()
    if not cfg.get('abilitato'):
        return {'ok': False, 'errore': 'collegamento a Lantek disattivato', 'pezzi': []}
    try:
        con, cur = lt._connessione(cfg)
        try:
            cur.execute("SELECT LTRIM(RTRIM(PrdRef)), PType, LTRIM(RTRIM(DIS_MatRef)), DIS_Thickness, CDate "
                        "FROM PPRR_PPRR_00000100 WHERE PrdRef LIKE ? ORDER BY CDate, PrdRef", [CODICE + '%'])
            pezzi = [{'codice': r[0], 'tipo': r[1], 'materiale': r[2],
                      'spessore': float(r[3]) if r[3] is not None else None,
                      'creato': r[4].isoformat(sep=' ', timespec='seconds') if hasattr(r[4], 'isoformat') else r[4]}
                     for r in cur.fetchall()]
        finally:
            con.close()
        return {'ok': True, 'errore': None, 'pezzi': pezzi}
    except Exception as e:
        return {'ok': False, 'errore': 'Lantek non risponde: ' + str(e)[:160], 'pezzi': []}


def prossimo_passo(foto: dict) -> str | None:
    """'crea' se il pezzo di prova non c'e'; 'reimporta' se c'e' uno solo a
    2 mm; None se la prova e' gia' fatta (o Lantek non si legge)."""
    if not foto.get('ok'):
        return None
    p = foto['pezzi']
    if not p:
        return 'crea'
    if len(p) == 1 and p[0]['codice'].upper() == CODICE and p[0]['spessore'] is not None \
            and abs(p[0]['spessore'] - SPESSORE['crea']) < 0.01:
        return 'reimporta'
    return None


def esito(passo: str, prima: dict, dopo: dict) -> dict:
    """Cosa e' successo in Lantek: {'parola', 'testo'}."""
    pp, pd = prima.get('pezzi') or [], dopo.get('pezzi') or []
    voluto = SPESSORE[passo]
    esatti = [p for p in pd if p['codice'].upper() == CODICE]
    if not dopo.get('ok'):
        return {'parola': 'non_letto', 'testo': 'Lantek non si legge adesso: guarda nel MES cosa è successo.'}
    if passo == 'crea':
        if len(esatti) == 1 and esatti[0]['spessore'] is not None and abs(esatti[0]['spessore'] - voluto) < 0.01:
            return {'parola': 'creato', 'testo': f'{CODICE} creato in Lantek, {MATERIALE} {voluto:g} mm. Ora si può fare il passo 2.'}
        return {'parola': 'non_creato', 'testo': f'{CODICE} non risulta creato come previsto: guarda nel MES.'}
    if len(pd) > len(pp):
        nuovi = [p['codice'] for p in pd if p['codice'] not in {x['codice'] for x in pp}]
        return {'parola': 'doppione',
                'testo': 'Lantek ha fatto un DOPPIONE (' + ', '.join(nuovi or [p['codice'] for p in pd]) + '): '
                         'la correzione automatica NON va attivata così.'}
    if len(esatti) == 1 and esatti[0]['spessore'] is not None and abs(esatti[0]['spessore'] - voluto) < 0.01:
        return {'parola': 'aggiornato',
                'testo': f'Lantek ha AGGIORNATO {CODICE}: ora è {voluto:g} mm, senza doppioni. '
                         'Controlla nel MES che il disegno e il resto siano a posto.'}
    if esatti and esatti[0]['spessore'] is not None and abs(esatti[0]['spessore'] - SPESSORE['crea']) < 0.01:
        return {'parola': 'rifiutato', 'testo': f'Lantek NON ha cambiato {CODICE}: è ancora {SPESSORE["crea"]:g} mm.'}
    return {'parola': 'altro', 'testo': 'Risultato non chiaro: guarda nel MES.'}


def dxf_prova(cartella: str) -> str:
    """Un rettangolo 200 x 100 mm (come i DXF puliti di FerroTrack)."""
    import ezdxf
    os.makedirs(cartella, exist_ok=True)
    p = os.path.join(cartella, CODICE + '.dxf')
    doc = ezdxf.new(setup=False)
    doc.header['$INSUNITS'] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    doc.saveas(p)
    return p


def esegui(passo: str, cartella: str, chi: str = '') -> dict:
    """Un passo della prova. {'ok', 'errore', 'prima', 'dopo', 'esito'}."""
    from . import lantek as lt
    if passo not in SPESSORE:
        return {'ok': False, 'errore': 'passo sconosciuto'}
    prima = fotografia()
    if not prima['ok']:
        return {'ok': False, 'errore': prima['errore']}
    if prossimo_passo(prima) != passo:
        return {'ok': False, 'errore': 'Questo passo non si può fare adesso: ricarica la finestra.', 'prima': prima}
    if not lt.procesos_disponibile():
        return {'ok': False, 'errore': 'Procesos.exe di Lantek non trovato su questo PC'}
    dxf = dxf_prova(os.path.join(cartella, 'prova_spessore'))
    r = lt.importa_pezzi_dxf([{'codice': CODICE, 'dxf': dxf, 'materiale': MATERIALE, 'spessore': SPESSORE[passo]}],
                             cartella, ['FT-PROVA', ''])
    if not r.get('eseguito'):
        return {'ok': False, 'errore': r.get('errore') or 'Procesos non ha finito', 'prima': prima}
    dopo = fotografia()
    e = esito(passo, prima, dopo)
    _scrivi(cartella, {'passo': passo, 'quando': datetime.now().isoformat(timespec='seconds'), 'chi': chi,
                       'prima': prima.get('pezzi'), 'dopo': dopo.get('pezzi'), 'esito': e})
    return {'ok': True, 'errore': None, 'prima': prima, 'dopo': dopo, 'esito': e}
