"""Dati comuni per l'addestramento (agente I): ground truth, cartelle d'ordine, fold, clienti."""
import csv
import hashlib
import json
import os
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WT = os.path.join(os.path.dirname(B), 'ferrotrack-motore-I', 'app')
sys.path.insert(0, os.path.join(WT, 'tools'))
import banco_lantek_report as rep  # noqa: E402

GT_DXF = os.path.join(B, 'lantek_gt_corretto.csv')
GT_DWG = os.path.join(B, 'lantek_gt_dwg.csv')


def _righe(p):
    return list(csv.DictReader(open(p, encoding='utf-8-sig'), delimiter=';'))


def cliente(path):
    parts = path.replace(chr(92), '/').split('/')
    up = [x.upper() for x in parts]
    return parts[up.index('TAGLIO') + 1].upper() if 'TAGLIO' in up else '?'


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
            fold = int(hashlib.md5(cart.encode('utf-8')).hexdigest(), 16) % 5
            out[(fonte, r['code'])] = {'cartella': cart, 'cliente': cliente(p), 'fold': fold, 'fonte': fonte}
    return out


def carica_gt():
    return {'dxf': rep.carica_gt(GT_DXF), 'dwg': rep.carica_gt(GT_DWG)}


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
