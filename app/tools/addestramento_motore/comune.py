"""Dati comuni per l'addestramento: ground truth, cartelle d'ordine, fold, clienti.

Percorsi: l'app e' quella di QUESTO worktree (app/), il banco e' la cartella
banco_motore accanto ai worktree (o FT_BANCO). Esempi confermati da Stefano
(raccogli_esempi.py + prepara_esempi.py): fonte 'es', GT in FT_GT_ESEMPI.
"""
import csv
import hashlib
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WT = APP
B = os.environ.get('FT_BANCO') or os.path.join(os.path.dirname(os.path.dirname(APP)), 'banco_motore')
sys.path.insert(0, os.path.join(WT, 'tools'))
import banco_lantek_report as rep  # noqa: E402

GT_DXF = os.path.join(B, 'lantek_gt_corretto.csv')
GT_DWG = os.path.join(B, 'lantek_gt_dwg.csv')
GT_ES = os.environ.get('FT_GT_ESEMPI')     # esempi_gt_completo.csv (prepara_esempi.py)


def _righe(p):
    return list(csv.DictReader(open(p, encoding='utf-8-sig'), delimiter=';'))


def cliente(path):
    parts = path.replace(chr(92), '/').split('/')
    up = [x.upper() for x in parts]
    return parts[up.index('TAGLIO') + 1].upper() if 'TAGLIO' in up else '?'


def fold_di(chiave):
    return int(hashlib.md5(chiave.encode('utf-8')).hexdigest(), 16) % 5


def info_codici():
    """codice -> {'cartella', 'cliente', 'fold', 'fonte'} per DXF e DWG (cartella = quella dell'ordine originale)."""
    g = _righe(GT_DXF)
    orig = {}
    for r in g:
        orig.setdefault(r['code'], r['dxf_match'])
    out = {}
    for fonte, righe in (('dxf', g), ('dwg', _righe(GT_DWG))):
        for r in righe:
            p = r['dxf_match'] if fonte == 'dxf' else orig.get(r['code'], r['dxf_match'])
            cart = os.path.dirname(p.replace(chr(92), '/')).lower()
            out[(fonte, r['code'])] = {'cartella': cart, 'cliente': cliente(p), 'fold': fold_di(cart), 'fonte': fonte}
    if GT_ES and os.path.isfile(GT_ES):
        for r in _righe(GT_ES):
            # cartella = preventivo/ordine in cui Stefano l'ha confermato
            cart = 'esempi/' + r['gruppo']
            out[('es', r['code'])] = {'cartella': cart, 'cliente': r.get('cliente') or 'DECA?',
                                      'fold': fold_di(cart), 'fonte': 'es',
                                      'tenuto_fuori': str(r.get('tenuto_fuori')) == '1'}
    return out


def carica_gt():
    out = {'dxf': rep.carica_gt(GT_DXF), 'dwg': rep.carica_gt(GT_DWG)}
    if GT_ES and os.path.isfile(GT_ES):
        out['es'] = rep.carica_gt(GT_ES)
    return out


def leggi_jsonl(p):
    out = {}
    for l in open(p, encoding='utf-8'):
        try:
            r = json.loads(l)
        except Exception:
            continue
        out[r['codice']] = r
    return out


def deca(cl):
    return 'DECA' in (cl or '')
