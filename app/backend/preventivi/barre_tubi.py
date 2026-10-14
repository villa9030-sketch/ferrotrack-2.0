"""Tubi del preventivo: pezzi totali e BARRE DA ORDINARE (14/10/2026).

Stefano, DECA 26T30SA0233-00 x 3: "sono 3 strutture da fare ma il materiale
da preparare mi dice x1 pz e in piu' non vedo dove mi dice che servono X barre
di tubo da ordinare oltre alla lavorazione".

La regola delle quantita' (UNA, la stessa del prezzo in calcolo.py e della
distinta dell'ordine in app._distinta_da_preventivo):

  - riga tubo DENTRO un assieme: `qty` = pezzi per 1 assieme; peso_kg,
    costo_materiale, costo_taglio_totale e costo_esterno sono il totale
    della riga per 1 assieme. Per tutte le strutture si moltiplica per la
    quantita' dell'assieme:  1 per struttura x 3 strutture = 3 pz.
    Vale per tutte le origini: STEP (qty = istanze del corpo), "Leggi
    distinta" (qty = riga x sotto-assiemi dentro UN assieme), "È un tubo" e
    tubolare a mano (qty = quantita' per assieme).
  - riga tubo SCIOLTA (senza assieme): la riga e' gia' il totale (come nel
    prezzo, che non la moltiplica per i lotti).

Le barre: per profilo + materiale, i pezzi (uno per pezzo fisico) vanno nel
piano di taglio di "Da procurare" (procura.piano_barre: first-fit
decreasing, con la lama, barra e lama dalle Impostazioni). Non si duplica.

Prezzo: il costo materiale del preventivo e' sui kg dei PEZZI (metri tagliati
x kg/m x €/kg). Qui si mostrano anche i kg e gli € delle barre comprate:
la differenza e' lo sfrido, oggi NON messo in prezzo (proposta, non regola).
"""
from __future__ import annotations


def _num(v, default=0.0) -> float:
    try:
        if v is None or v == '':
            return default
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _int_pos(v, default=1) -> int:
    try:
        n = int(round(float(v)))
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def pezzi_riga(t: dict) -> int:
    """Pezzi della riga: per 1 assieme se la riga e' in un assieme, altrimenti
    il totale della riga."""
    return _int_pos((t or {}).get('qty'))


def qta_assiemi(assiemi) -> dict:
    return {a.get('codice_assieme'): _int_pos(a.get('qty'))
            for a in (assiemi or []) if a.get('codice_assieme')}


def moltiplicatore(t: dict, qta_ass: dict) -> int:
    """Quante strutture: la quantita' dell'assieme della riga, 1 se sciolta."""
    cod = (t or {}).get('codice_assieme')
    return qta_ass.get(cod, 1) if cod else 1


def lunghezza_mm(t: dict) -> float:
    """Lunghezza di UN pezzo (lunghezza_m e' sempre quella del singolo)."""
    lp = _num(t.get('lunghezza_pezzo_mm'))
    if lp > 0:
        return lp
    return _num(t.get('lunghezza_m')) * 1000.0


def costo_riga(t: dict) -> float:
    """Costo della riga (per 1 assieme se in assieme): come calcolo.costo_tubolare."""
    return _num(t.get('costo_materiale')) + _num(t.get('costo_taglio_totale')) + _num(t.get('costo_esterno'))


def riga_totale(t: dict, qta_ass: dict) -> dict:
    """Una riga tubo con i numeri per 1 assieme e per tutte le strutture."""
    q = pezzi_riga(t)
    m = moltiplicatore(t, qta_ass)
    return {
        'id': t.get('id'), 'codice_assieme': t.get('codice_assieme') or None,
        'profilo': t.get('profilo') or '', 'materiale': t.get('materiale') or 'acciaio',
        'lunghezza_mm': round(lunghezza_mm(t), 1),
        'pz_per_assieme': q if t.get('codice_assieme') else None,
        'n_assiemi': m, 'pz_totali': q * m,
        'peso_kg_totale': round(_num(t.get('peso_kg')) * m, 3),
        'costo_totale': round(costo_riga(t) * m, 2),
    }


_NOMI_MAT = {'acciaio': 'ferro', 'inox': 'inox', 'alluminio': 'alluminio'}


def _kg_m(t: dict) -> float:
    k = _num(t.get('kg_m'))
    if k > 0:
        return k
    L, q = lunghezza_mm(t), pezzi_riga(t)
    p = _num(t.get('peso_kg'))
    return p / (L / 1000.0 * q) if L > 0 and p > 0 else 0.0


def _euro_kg(t: dict) -> float:
    e = _num(t.get('euro_kg'))
    if e > 0:
        return e
    p = _num(t.get('peso_kg'))
    return _num(t.get('costo_materiale')) / p if p > 0 else 0.0


def barre(tubolari, assiemi=None, cfg: dict | None = None, solo_assieme=None) -> dict:
    """Barre da ordinare per i tubi del preventivo (o di un solo assieme).

    Ritorna {gruppi: [...], totali: {...}, barra_mm, lama_mm}. Ogni gruppo
    (profilo + materiale): n_pezzi, metri usati, n_barre, sfrido_pct, piano
    (per barra i tagli), kg e € usati (quelli del prezzo) e delle barre.
    """
    from .. import procura as _pc
    cfg = cfg or _pc.config()
    qa = qta_assiemi(assiemi)
    per = {}
    for t in tubolari or []:
        if not isinstance(t, dict):
            continue
        if solo_assieme is not None and (t.get('codice_assieme') or None) != solo_assieme:
            continue
        mat = str(t.get('materiale') or 'acciaio').lower()
        k = (_pc.chiave_profilo(t.get('profilo')), mat)
        g = per.setdefault(k, {
            'chiave': '|'.join(k), 'profilo': _pc.nome_profilo(t.get('profilo')),
            'materiale': mat, 'materiale_nome': _NOMI_MAT.get(mat, mat), 'testi': [],
            'pezzi': [], 'righe': [], 'kg_usati': 0.0, 'eur_usati': 0.0,
            'kg_m': 0.0, 'euro_kg': 0.0, 'pz_esterni': 0,
        })
        rt = riga_totale(t, qa)
        g['righe'].append(rt)
        testo = str(t.get('materiale_testo') or '').strip()
        if testo and testo not in g['testi']:
            g['testi'].append(testo)
        g['kg_usati'] += rt['peso_kg_totale']
        g['eur_usati'] += _num(t.get('costo_materiale')) * rt['n_assiemi']
        g['kg_m'] = g['kg_m'] or _kg_m(t)
        g['euro_kg'] = g['euro_kg'] or _euro_kg(t)
        if t.get('nota_esterno') or _num(t.get('costo_esterno')) > 0:
            g['pz_esterni'] += rt['pz_totali']
        rif = t.get('codice_pezzo') or t.get('posizione') or None
        for _ in range(rt['pz_totali']):
            g['pezzi'].append({'lunghezza_mm': rt['lunghezza_mm'], 'codice': rif,
                               'assieme': rt['codice_assieme'], 'id': t.get('id')})
    gruppi = []
    for g in per.values():
        pb = _pc.piano_barre(g['pezzi'], cfg['barra_mm'], cfg['lama_mm'])
        barra_m = pb['barra_mm'] / 1000.0
        metri_barre = pb['n_barre'] * barra_m
        # pezzi piu' lunghi di una barra: comprati a parte, uno per pezzo
        metri_lunghi = sum(_num(p.get('lunghezza_mm')) for p in pb['troppo_lunghi']) / 1000.0
        comprati_m = metri_barre + metri_lunghi
        kg_barre = comprati_m * g['kg_m']
        piano = [{'tagli': [{'lunghezza_mm': x['lunghezza_mm'], 'codice': x['codice'], 'assieme': x['assieme']}
                            for x in b['tagli']],
                  'usato_mm': b['usato_mm'], 'sfrido_mm': b['sfrido_mm']} for b in pb['barre']]
        gruppi.append({
            'chiave': g['chiave'], 'profilo': g['profilo'], 'materiale': g['materiale'],
            'materiale_nome': g['materiale_nome'] + (' ' + ', '.join(g['testi']) if g['testi'] else ''),
            'n_pezzi': len(g['pezzi']), 'metri': pb['metri'], 'n_barre': pb['n_barre'],
            'barra_mm': pb['barra_mm'], 'lama_mm': pb['lama_mm'],
            'metri_barre': round(comprati_m, 3),
            'sfrido_pct': round((1 - pb['metri'] / comprati_m) * 100, 1) if comprati_m > 0 else 0.0,
            'kg_m': round(g['kg_m'], 3), 'euro_kg': round(g['euro_kg'], 3),
            'kg_usati': round(g['kg_usati'], 2), 'eur_usati': round(g['eur_usati'], 2),
            'kg_barre': round(kg_barre, 2), 'eur_barre': round(kg_barre * g['euro_kg'], 2),
            'pz_esterni': g['pz_esterni'],
            'piano': piano,
            'troppo_lunghi': [{'lunghezza_mm': p['lunghezza_mm'], 'codice': p['codice']} for p in pb['troppo_lunghi']],
            'senza_lunghezza': len(pb['senza_lunghezza']),
            'righe': g['righe'],
        })
    gruppi.sort(key=lambda x: (x['profilo'].lower(), x['materiale']))
    tot = {k: round(sum(x[k] for x in gruppi), 2)
           for k in ('n_pezzi', 'n_barre', 'metri', 'metri_barre', 'kg_usati', 'kg_barre', 'eur_usati', 'eur_barre')}
    tot['n_pezzi'] = int(tot['n_pezzi'])
    tot['n_barre'] = int(tot['n_barre'])
    return {'gruppi': gruppi, 'totali': tot, 'barra_mm': cfg['barra_mm'], 'lama_mm': cfg['lama_mm'],
            'prezzo_su': 'kg_usati'}
