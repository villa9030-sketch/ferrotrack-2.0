"""STEP file assembly analysis.

Analyzes STEP assemblies for weld estimation and instance counting.

Albero di prodotto (PRODUCT_DEFINITION / NEXT_ASSEMBLY_USAGE_OCCURRENCE /
CONTEXT_DEPENDENT_SHAPE_REPRESENTATION) in step_parser.GeometriaStep.albero():
le trasformazioni sono COMPOSTE lungo i sotto-assiemi e le quantita' sono
moltiplicate lungo l'albero (sotto-assieme x2 con parte x3 = 6).

Saldatura: uno spigolo del corpo A e' "di contatto" se TUTTI i suoi punti
stanno su una faccia piana del corpo B *dentro il suo contorno* (punto in
poligono nel piano della faccia, fori esclusi) - non basta il piano infinito.
Per ogni faccia di contatto si tiene solo l'anello di contatto ESTERNO
(tubo su piastra: perimetro esterno, non esterno+interno).
"""

import logging
import math

from .step_parser import (
    carica_entita, GeometriaStep, MAT_ID, mat_punto, mat_dir,
    v_dot, v_sub, v_len, punto_in_poligono_2d,
)

logger = logging.getLogger(__name__)

TOL_PIANO = 0.5      # mm distanza punto-piano
TOL_BORDO = 0.5      # mm tolleranza sul contorno della faccia
MIN_WELD = 5.0       # mm: spigoli piu' corti (smussi, raccordi) ignorati
BB_TOL = 2.0         # mm: corpi con bbox identico = stesso pezzo (split SolidWorks)


def _geometria_mondo(geo, bid, T):
    """Facce piane e spigoli di un corpo trasformati in coordinate assieme."""
    ident = T is MAT_ID
    tp = (lambda p: p) if ident else (lambda p: mat_punto(T, p))
    td = (lambda d: d) if ident else (lambda d: mat_dir(T, d))
    facce = []
    spigoli = {}
    pts_all = []
    for f in geo.facce(bid):
        for lp in f['loops']:
            for ec, ep in lp['edges']:
                if ec not in spigoli:
                    spigoli[ec] = [tp(p) for p in ep]
        if f['tipo'] != 'PLANE' or not f['loops']:
            continue
        o, u, v = f['origin'], f['u'], f['v']
        loops2d = []
        for lp in f['loops']:
            if len(lp['punti']) >= 3:
                loops2d.append([(v_dot(v_sub(p, o), u), v_dot(v_sub(p, o), v)) for p in lp['punti']])
        if not loops2d:
            continue
        wpts = [tp(p) for p in f['punti']]
        facce.append({
            'id': f['id'], 'o': tp(o), 'n': td(f['normal']), 'u': td(u), 'v': td(v),
            'loops2d': loops2d,
            'bmin': tuple(min(p[k] for p in wpts) for k in range(3)),
            'bmax': tuple(max(p[k] for p in wpts) for k in range(3)),
        })
    for pts in spigoli.values():
        pts_all.extend(pts)
    if pts_all:
        bmin = tuple(min(p[k] for p in pts_all) for k in range(3))
        bmax = tuple(max(p[k] for p in pts_all) for k in range(3))
    else:
        bmin = bmax = (0.0, 0.0, 0.0)
    return {'facce': facce, 'spigoli': spigoli, 'bmin': bmin, 'bmax': bmax}


def _lunghezza(pts):
    return sum(v_len(v_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1))


def _campioni(pts, n=8):
    if len(pts) <= n:
        return pts
    passo = (len(pts) - 1) / float(n - 1)
    return [pts[int(round(i * passo))] for i in range(n)]


def _su_faccia(pts, fc):
    for p in pts:
        d = v_sub(p, fc['o'])
        if abs(v_dot(d, fc['n'])) > TOL_PIANO:
            return False
    for p in _campioni(pts):
        d = v_sub(p, fc['o'])
        if not punto_in_poligono_2d((v_dot(d, fc['u']), v_dot(d, fc['v'])), fc['loops2d'], TOL_BORDO):
            return False
    return True


def _solo_anello_esterno(contatti, fc):
    """Tra gli spigoli di contatto su una faccia, tiene le catene esterne:
    una catena il cui ingombro 2D e' strettamente dentro un'altra (es. il
    perimetro interno di un tubo appoggiato) viene scartata."""
    def _k(p):
        return (round(p[0] / 0.05), round(p[1] / 0.05), round(p[2] / 0.05))
    n = len(contatti)
    padre = list(range(n))

    def _find(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i
    per_punto = {}
    for i, (_, pts, _) in enumerate(contatti):
        for p in (pts[0], pts[-1]):
            per_punto.setdefault(_k(p), []).append(i)
    for ids in per_punto.values():
        for j in ids[1:]:
            padre[_find(j)] = _find(ids[0])
    comp = {}
    for i in range(n):
        comp.setdefault(_find(i), []).append(i)
    box = {}
    for r, ids in comp.items():
        uu, vv = [], []
        for i in ids:
            for p in contatti[i][1]:
                d = v_sub(p, fc['o'])
                uu.append(v_dot(d, fc['u']))
                vv.append(v_dot(d, fc['v']))
        box[r] = (min(uu), max(uu), min(vv), max(vv))
    tenuti = []
    for r, ids in comp.items():
        b = box[r]
        interno = any(
            r2 != r and b2[0] < b[0] - 0.1 and b2[1] > b[1] + 0.1 and b2[2] < b[2] - 0.1 and b2[3] > b[3] + 0.1
            for r2, b2 in box.items())
        if not interno:
            tenuti.extend(contatti[i] for i in ids)
    return tenuti


def analizza_step_assieme(step_path: str) -> dict:
    """Analizza file STEP 3D di un assieme per stimare i ml di saldatura.

    Tutte le istanze dell'albero di prodotto vengono posizionate con la
    trasformazione composta; i file multi-body senza struttura d'assieme
    usano i corpi cosi' come sono.

    Args:
        step_path: Path al file STEP.

    Returns:
        dict: {"saldatura_mm": float, "n_corpi": int (istanze),
               "avvisi": [...], "unita": str, "errore": str|None}
    """
    try:
        entities, info = carica_entita(step_path)
    except (IOError, OSError) as e:
        return {"saldatura_mm": 0.0, "n_corpi": 0, "avvisi": [], "errore": str(e)}

    geo = GeometriaStep(entities)
    avvisi = list(info['avvisi'])
    try:
        occorrenze = geo.albero()['occorrenze']
    except Exception as e:
        logger.warning("albero prodotto STEP non leggibile: %s", e)
        occorrenze = [(b, MAT_ID) for b in geo.corpi(includi_superfici=True)]
        avvisi.append("albero_prodotto_non_leggibile: posizioni d'assieme ignorate")

    parti = []
    for bid, T in occorrenze:
        try:
            parti.append(_geometria_mondo(geo, bid, T))
        except Exception as e:
            logger.warning("geometria corpo #%s non leggibile: %s", bid, e)

    # corpi con bbox identico = stesso pezzo fisico (SolidWorks splitta i corpi)
    gruppi = []
    for p in parti:
        for g in gruppi:
            q = g[0]
            if all(abs(a - b) < BB_TOL for a, b in zip(p['bmin'], q['bmin'])) and \
                    all(abs(a - b) < BB_TOL for a, b in zip(p['bmax'], q['bmax'])):
                g.append(p)
                break
        else:
            gruppi.append([p])
    n_corpi = len(gruppi)
    if n_corpi < 2:
        return {"saldatura_mm": 0.0, "n_corpi": n_corpi, "avvisi": avvisi,
                "unita": info['unita'],
                "errore": "Meno di 2 corpi" if n_corpi < 2 else None}

    def _unisci(g):
        facce, spigoli = [], []
        for p in g:
            facce.extend(p['facce'])
            spigoli.extend(p['spigoli'].values())
        bmin = tuple(min(p['bmin'][k] for p in g) for k in range(3))
        bmax = tuple(max(p['bmax'][k] for p in g) for k in range(3))
        return {'facce': facce, 'spigoli': spigoli, 'bmin': bmin, 'bmax': bmax}
    pezzi = [_unisci(g) for g in gruppi]

    total_weld = 0.0
    visti = set()

    def _chiave(pts, ln):
        # posizione indipendente dal verso di percorrenza: estremi ordinati + lunghezza
        k = sorted([tuple(round(c / 0.5) for c in pts[0]), tuple(round(c / 0.5) for c in pts[-1])])
        return (k[0], k[1], round(ln / 0.5))

    for i in range(len(pezzi)):
        A = pezzi[i]
        for j in range(i + 1, len(pezzi)):
            B = pezzi[j]
            zmin = [max(A['bmin'][k], B['bmin'][k]) - 1.0 for k in range(3)]
            zmax = [min(A['bmax'][k], B['bmax'][k]) + 1.0 for k in range(3)]
            if any(zmin[k] > zmax[k] for k in range(3)):
                continue
            for src, dst in ((A, B), (B, A)):
                per_faccia = {}
                for pts in src['spigoli']:
                    if len(pts) < 2:
                        continue
                    if not all(zmin[k] <= p[k] <= zmax[k] for p in (pts[0], pts[-1]) for k in range(3)):
                        continue
                    ln = _lunghezza(pts)
                    if ln <= MIN_WELD:
                        continue
                    emin = [min(p[k] for p in pts) for k in range(3)]
                    emax = [max(p[k] for p in pts) for k in range(3)]
                    for fi, fc in enumerate(dst['facce']):
                        if any(emin[k] < fc['bmin'][k] - 1.0 or emax[k] > fc['bmax'][k] + 1.0
                               for k in range(3)):
                            continue
                        if _su_faccia(pts, fc):
                            per_faccia.setdefault(fi, []).append((None, pts, ln))
                            break
                for fi, contatti in per_faccia.items():
                    for _, pts, ln in _solo_anello_esterno(contatti, dst['facce'][fi]):
                        key = _chiave(pts, ln)
                        if key in visti:
                            continue
                        visti.add(key)
                        total_weld += ln

    return {
        "saldatura_mm": round(total_weld, 1),
        "n_corpi": n_corpi,
        "avvisi": avvisi,
        "unita": info['unita'],
        "errore": None
    }


def conta_istanze_nauo(step_path: str) -> dict:
    """Conta quante volte ogni body STEP e' istanziato nell'assieme.

    Costruisce l'albero di prodotto (PRODUCT_DEFINITION, NAUO, PDS, SDR,
    SHAPE_REPRESENTATION_RELATIONSHIP, ADVANCED_BREP_SHAPE_REPRESENTATION ->
    MANIFOLD_SOLID_BREP) e MOLTIPLICA le quantita' lungo l'albero: un
    sotto-assieme x2 che contiene una parte x3 da' 6. Il collegamento PD ->
    corpo segue le rappresentazioni, non il contesto geometrico (molti
    exporter condividono un solo contesto per tutte le parti).

    Args:
        step_path: Path al file STEP.

    Returns:
        dict: body_id -> numero di istanze (>= 1 per ogni corpo; file a parte
        singola -> tutti 1). {} se il file non e' leggibile.
    """
    try:
        entities, _ = carica_entita(step_path)
    except (IOError, OSError):
        return {}
    try:
        return GeometriaStep(entities).albero()['qty']
    except Exception as e:
        logger.warning("conteggio istanze STEP fallito: %s", e)
        return {}
