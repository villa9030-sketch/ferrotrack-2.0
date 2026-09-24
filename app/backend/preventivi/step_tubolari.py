"""STEP file tubular structure analysis.

Detects CHS (Circular Hollow Sections), RHS (Rectangular Hollow Sections),
and SHS (Square Hollow Sections) from STEP geometry.

Lettura entita', unita' di misura e geometria B-rep condivise in step_parser
(coordinate gia' in mm, anelli ordinati, aree/coperture esatte).
"""

import json
import logging
import math
import os

from .step_parser import (
    carica_entita, GeometriaStep, v_dot, v_norm, v_cross, v_sub, v_len,
    copertura_angolare, distanza_retta,
)

logger = logging.getLogger(__name__)

DENSITA_ACCIAIO = 7.85  # kg/dm3

# Tolleranza sullo spessore misurato per accettare un profilo a catalogo
TOLL_SPESSORE_MM = 0.35


def carica_profili_tubolari(base_dir: str) -> dict:
    """Carica database profili tubolari da profili_tubolari.json.

    Args:
        base_dir: Directory base dove cercare il file profili_tubolari.json.

    Returns:
        dict con chiavi 'CHS', 'SHS', 'RHS', ciascuna con lista di profili.
    """
    profili_path = os.path.join(base_dir, "profili_tubolari.json")
    try:
        with open(profili_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"CHS": [], "SHS": [], "RHS": []}


def peso_teorico_rhs(lato_a, lato_b, spessore, densita=DENSITA_ACCIAIO) -> float:
    """kg/m di un tubo rettangolare/quadro (EN 10219: ro=2t se t<=6, 2.5t oltre)."""
    t = float(spessore or 0)
    if t <= 0:
        return 0.0
    ro = 2.0 * t if t <= 6.0 else 2.5 * t
    ri = ro - t
    area = 2 * t * (lato_a + lato_b - 4 * ro) + math.pi * (ro * ro - ri * ri)
    if area <= 0:  # sezioni piccolissime: formula a spigolo vivo
        area = 2 * t * (lato_a + lato_b - 2 * t)
    return area * densita / 1000.0


def peso_teorico_chs(d_ext, spessore, densita=DENSITA_ACCIAIO) -> float:
    """kg/m di un tubo tondo; spessore None/0 = tondo pieno."""
    if not spessore or spessore <= 0 or spessore >= d_ext / 2.0:
        return math.pi * d_ext * d_ext / 4.0 * densita / 1000.0
    return math.pi * (d_ext - spessore) * spessore * densita / 1000.0


def match_profilo_chs(d_ext, spessore, profili_db) -> dict | None:
    """Trova il profilo CHS piu' vicino nel database.

    Tolleranza +/-1.0mm sul diametro; se lo spessore e' MISURATO deve
    coincidere entro TOLL_SPESSORE_MM (40x1.5 non diventa 40x2, +30% peso).

    Args:
        d_ext: Diametro esterno in mm.
        spessore: Spessore parete misurato in mm (None = sconosciuto).
        profili_db: Database profili (da carica_profili_tubolari).

    Returns:
        dict del profilo matchato o None.
    """
    best = None
    best_dist = 999
    for p in profili_db.get("CHS", []):
        dist_d = abs(p["d_ext"] - d_ext)
        dist_s = abs(p["spessore"] - spessore) if spessore else 0
        if dist_d >= 1.0:
            continue
        if spessore and dist_s > TOLL_SPESSORE_MM:
            continue
        dist = dist_d + dist_s * 2
        if dist < best_dist:
            best_dist = dist
            best = p
    return best


def match_profilo_rhs(lato_a, lato_b, spessore, profili_db) -> dict | None:
    """Trova il profilo RHS/SHS piu' vicino. Tolleranza +/-2mm sui lati;
    se lo spessore e' misurato deve coincidere entro TOLL_SPESSORE_MM.

    Args:
        lato_a: Dimensione lato A in mm.
        lato_b: Dimensione lato B in mm.
        spessore: Spessore parete in mm (None = sconosciuto, solo sezione).
        profili_db: Database profili (da carica_profili_tubolari).

    Returns:
        dict del profilo matchato o None.
    """
    a, b = max(lato_a, lato_b), min(lato_a, lato_b)
    best = None
    best_dist = 999
    search_types = ["SHS", "RHS"] if abs(a - b) < 2.0 else ["RHS", "SHS"]
    for tipo in search_types:
        for p in profili_db.get(tipo, []):
            pa, pb = max(p["lato_a"], p["lato_b"]), min(p["lato_a"], p["lato_b"])
            dist_a = abs(pa - a)
            dist_b = abs(pb - b)
            dist_s = abs(p["spessore"] - spessore) if spessore else 0
            if dist_a >= 2.0 or dist_b >= 2.0:
                continue
            if spessore and dist_s > TOLL_SPESSORE_MM:
                continue
            dist = dist_a + dist_b + dist_s * 2
            if dist < best_dist:
                best_dist = dist
                best = p
    return best


def _fmt(x):
    """Numero compatto per i nomi profilo (40.0 -> '40', 1.5 -> '1.5')."""
    x = round(float(x), 1)
    return str(int(x)) if abs(x - int(x)) < 1e-9 else str(x)


def _classifica_testate(fs, esclusi, axis, p_min, p_max, faccia_parallela):
    """Classifica le due testate del tubo: dritto / obliquo / sagomato.

    - sagomato: la testata ha facce NON piane (bocca di lupo, cilindro/bspline
      di taglio) o piani paralleli all'asse (tacche/intagli);
    - obliquo: testata piana inclinata > 2 gradi rispetto al perpendicolare;
    - dritto: testata piana perpendicolare all'asse.

    Una faccia appartiene a una testata se TOCCA l'estremo (entro 1 mm): i fori
    in mezzo al tubo o vicino alla testa non contano.
    Ritorna [(taglio, angolo), (taglio, angolo)] per testata 1 (p_min) e 2 (p_max).
    """
    TOL = 1.0
    testate = [[], []]
    for f in fs:
        if f['id'] in esclusi or not f['punti']:
            continue
        if faccia_parallela(f):
            continue
        pr = [v_dot(p, axis) for p in f['punti']]
        if min(pr) <= p_min + TOL:
            testate[0].append(f)
        if max(pr) >= p_max - TOL:
            testate[1].append(f)
    out = []
    for facce in testate:
        if not facce:
            out.append(("dritto", 0.0))
            continue
        sagomato = False
        ang_max = 0.0
        for f in facce:
            if f['tipo'] != 'PLANE':
                sagomato = True
                continue
            d = abs(v_dot(v_norm(f['normal']), axis))
            ang = math.degrees(math.acos(max(-1.0, min(1.0, d))))
            if ang > 80.0:
                sagomato = True  # piano parallelo all'asse che tocca la testa = intaglio
                continue
            ang_max = max(ang_max, ang)
        ang_max = round(ang_max, 1)
        if sagomato:
            out.append(("sagomato", ang_max))
        elif ang_max > 2.0:
            out.append(("obliquo", ang_max))
        else:
            out.append(("dritto", ang_max))
    return out


def _centroide(pts):
    if not pts:
        return None
    return [(max(p[k] for p in pts) + min(p[k] for p in pts)) / 2 for k in range(3)]


def _analizza_rhs(geo, bid, fs, profili_db, debug):
    """Riconosce un tubo rettangolare/quadro chiuso. Ritorna dict tubo o None."""
    piani = [f for f in fs if f['tipo'] == 'PLANE' and f['punti']]
    if len(piani) < 6:
        return None
    # Soglia big_cyl 30mm: alcuni RHS hanno raccordi interni r 15-25mm.
    if any(f['tipo'] == 'CYL' and f['raggio'] > 30.0 for f in fs):
        return None

    # Step 1: gruppi di piani a normali parallele
    gruppi = []
    for f in piani:
        n = v_norm(f['normal'])
        for g in gruppi:
            if abs(v_dot(n, g[0])) > 0.95:
                g[1].append(f)
                break
        else:
            gruppi.append([n, [f]])
    if len(gruppi) < 3:
        return None

    body_verts = [geo.punto(v) for f in fs for v in f['vertici']]
    body_verts = list({p for p in body_verts if p})
    if not body_verts:
        body_verts = geo.punti_corpo(bid)
    if not body_verts:
        return None

    def _extent(d):
        pr = [v_dot(v, d) for v in body_verts]
        return max(pr) - min(pr)

    # Asse candidato: normali dei gruppi con >=2 piani + assi cardinali
    cand = []
    for n, g in gruppi:
        if len(g) >= 2:
            cand.append(n)
    for c in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
        if not any(abs(v_dot(c, d)) > 0.98 for d in cand):
            cand.append(c)
    cand.sort(key=_extent, reverse=True)
    axis = cand[0]
    lunghezza_mm = _extent(axis)

    # Fix v14b: l'asse vero e' perpendicolare ai 2 gruppi laterali chiusi
    # (>=4 piani: 2 esterni + 2 interni). Vale anche per spezzoni corti
    # (prima richiedeva > 50mm e scartava i distanziali).
    laterali4 = [n for n, g in gruppi if len(g) >= 4]
    if len(laterali4) >= 2:
        n1, n2 = laterali4[0], laterali4[1]
        if abs(v_dot(n1, n2)) < 0.2:
            cr = v_norm(v_cross(n1, n2))
            nl = _extent(cr)
            if nl > 5:
                axis = cr
                lunghezza_mm = nl

    # Step 2: piani di testa (dritti/obliqui) vs laterali
    axis_planes, side_planes, oblique_planes = [], [], []
    for f in piani:
        d = abs(v_dot(v_norm(f['normal']), axis))
        if d > 0.995:
            axis_planes.append(f)
        elif d < 0.5:
            side_planes.append(f)
        else:
            oblique_planes.append(f)

    side_groups = []
    for f in side_planes:
        n = v_norm(f['normal'])
        for g in side_groups:
            if abs(v_dot(n, g[0])) > 0.95:
                g[1].append(f)
                break
        else:
            side_groups.append([n, [f]])
    closed_side = [g for g in side_groups if len(g[1]) >= 4]

    # Topologia da tubo cavo CHIUSO: 2 lati chiusi (4 piani ciascuno) e un
    # foro passante (genere >= 1). Lamiere piegate a U/C hanno 1 solo lato chiuso.
    if len(closed_side) < 2:
        if debug:
            logger.info("[TUBE bid=%s] SKIP topologia: side_groups=%s", bid,
                        sorted((len(g[1]) for g in side_groups), reverse=True))
        return None
    if geo.genere_corpo(bid) < 1:
        if debug:
            logger.info("[TUBE bid=%s] SKIP genere 0 (non cavo)", bid)
        return None

    # Dimensioni sezione: ingombro dei vertici sulle normali laterali
    dims = sorted((_extent(g[0]) for g in closed_side[:2]), reverse=True)
    dim_a, dim_b = dims[0], dims[1]

    # Spessore parete: sulla normale di ogni lato chiuso, distanza tra la
    # proiezione piu' esterna e la successiva (parete esterna -> interna).
    # I punti di tangenza dei raccordi cadono piu' all'interno (ro >= t),
    # quindi non disturbano. Range accettato 1.0 - 12.5 mm.
    spessori = []
    for n, g in closed_side[:2]:
        vp = sorted({round(v_dot(v, n), 2) for v in body_verts})
        if len(vp) >= 4:
            for t in (vp[-1] - vp[-2], vp[1] - vp[0]):
                if 0.95 <= t <= 12.5:
                    spessori.append(t)
    spessore = None
    if spessori:
        spessori.sort()
        spessore = round(spessori[len(spessori) // 2], 1)
    if spessore is None or spessore > 0.3 * min(dim_a, dim_b):
        if debug:
            logger.info("[TUBE bid=%s] SKIP spessore parete non plausibile (%s)", bid, spessore)
        return None

    # Lunghezza dai soli piani/facce di testa (evita piastre fuse sul tubo):
    # estensione lungo l'asse dei PUNTI delle facce di testa.
    teste = axis_planes + oblique_planes
    if teste:
        pr = [v_dot(p, axis) for f in teste for p in f['punti']]
        pr += [v_dot(p, axis) for g in closed_side for f in g[1] for p in f['punti']]
        lunghezza_mm = max(pr) - min(pr)
    pr_all = [v_dot(p, axis) for g in closed_side for f in g[1] for p in f['punti']]
    p_min, p_max = min(pr_all), max(pr_all)

    ratio_sezione = dim_a / max(dim_b, 0.1)
    if dim_b < 8 or ratio_sezione > 8.0:
        return None

    profilo = match_profilo_rhs(dim_a, dim_b, spessore, profili_db)
    elongation = lunghezza_mm / max(dim_a, 1.0)
    min_elong = 0.5 if profilo else 1.0
    if lunghezza_mm < 5 or elongation < min_elong:
        if debug:
            logger.info("[TUBE bid=%s] SKIP elongation %.2f < %.2f", bid, elongation, min_elong)
        return None

    lato_a = round(dim_a, 1)
    lato_b = round(dim_b, 1)
    avvisi = []
    if profilo:
        nome_profilo = profilo["nome"]
        peso_kg_m = profilo["peso_kg_m"]
        tipo = "SHS" if "Quadro" in profilo["nome"] else "RHS"
    else:
        tipo = "SHS" if abs(lato_a - lato_b) < 2.0 else "RHS"
        tipo_label = "Quadro" if tipo == "SHS" else "Rett."
        nome_profilo = f"{tipo_label} {_fmt(lato_a)}x{_fmt(lato_b)} sp.{_fmt(spessore)}mm"
        peso_kg_m = round(peso_teorico_rhs(lato_a, lato_b, spessore), 3)
        avvisi.append("profilo_non_a_catalogo")

    # Tagli: facce laterali (e raccordi paralleli all'asse) escluse
    esclusi = {f['id'] for g in closed_side for f in g[1]}

    def _parallela(f):
        if f['tipo'] == 'CYL':
            return abs(v_dot(v_norm(f['axis']), axis)) > 0.995
        return False
    (taglio_1, ang_1), (taglio_2, ang_2) = _classifica_testate(
        fs, esclusi, axis, p_min, p_max, _parallela)

    lunghezza_m = round(lunghezza_mm / 1000.0, 3)
    if debug:
        logger.info("[TUBE bid=%s] MATCHED %s %sx%s sp=%s len=%.3fm %s/%s", bid,
                    nome_profilo, lato_a, lato_b, spessore, lunghezza_m, taglio_1, taglio_2)
    return {
        "tipo": tipo,
        "profilo": nome_profilo,
        "lato_a": lato_a,
        "lato_b": lato_b,
        "spessore": spessore,
        "lunghezza_m": lunghezza_m,
        "peso_kg": round(peso_kg_m * lunghezza_m, 2),
        "peso_kg_m": peso_kg_m,
        "taglio_1": taglio_1,
        "taglio_2": taglio_2,
        "angolo_taglio_1": ang_1,
        "angolo_taglio_2": ang_2,
        "centroide": _centroide(body_verts),
        "body_id": bid,
        "avvisi": avvisi,
    }


def _gruppi_coassiali(cilindri):
    """Raggruppa facce cilindriche con la stessa retta d'asse."""
    gruppi = []
    for f in cilindri:
        a = v_norm(f['axis'])
        for g in gruppi:
            if abs(v_dot(a, g['axis'])) > 0.9995 and \
                    distanza_retta(f['origin'], g['origin'], g['axis']) < 0.05 + 0.002 * f['raggio']:
                g['facce'].append(f)
                break
        else:
            gruppi.append({'axis': a, 'origin': f['origin'], 'facce': [f]})
    return gruppi


def _raggi_pieni(g):
    """{raggio: (copertura_rad, facce)} per un gruppo coassiale (raggi fusi a 0.01mm)."""
    per_r = {}
    for f in g['facce']:
        key = None
        for r in per_r:
            if abs(r - f['raggio']) < 0.01:
                key = r
                break
        if key is None:
            key = f['raggio']
        per_r.setdefault(key, []).append(f)
    out = {}
    for r, ff in per_r.items():
        pts = [p for f in ff for p in f['punti']]
        out[r] = (copertura_angolare(pts, g['origin'], g['axis']), ff)
    return out


PIENO = 2 * math.pi * 0.97


def _analizza_chs(geo, bid, fs, profili_db, debug):
    """Riconosce un tubo tondo (o un tondo pieno). Ritorna dict tubo o None.

    Requisiti (evita dischi forati, semigusci calandrati, fori di piastre):
    - cilindro esterno a 360 gradi (unione delle facce coassiali);
    - le facce del gruppo coassiale sono la maggior parte dell'area del corpo;
    - cavo: cilindro interno coassiale a 360 gradi per quasi tutta la lunghezza,
      lunghezza >= 0.5 D;  pieno: lunghezza >= 2 D (peso del tondo pieno).
    """
    cil = [f for f in fs if f['tipo'] == 'CYL' and f['raggio'] > 1.0 and f['punti']]
    if not cil:
        return None
    area_tot = sum(f['area'] for f in fs) or 1.0
    gruppi = _gruppi_coassiali(cil)
    g = max(gruppi, key=lambda x: sum(f['area'] for f in x['facce']))
    area_g = sum(f['area'] for f in g['facce'])
    if area_g < 0.5 * area_tot:
        if debug:
            logger.info("[CHS bid=%s] SKIP area cilindrica %.0f%% del corpo", bid, 100 * area_g / area_tot)
        return None
    axis = g['axis']
    raggi = _raggi_pieni(g)
    r_ext = max(raggi)
    cov_ext, facce_ext = raggi[r_ext]
    if cov_ext < PIENO:
        if debug:
            logger.info("[CHS bid=%s] SKIP cilindro esterno non a 360 (%.0f gradi)", bid,
                        math.degrees(cov_ext))
        return None
    pr = [v_dot(p, axis) for f in g['facce'] for p in f['punti']]
    p_min, p_max = min(pr), max(pr)
    lunghezza_mm = p_max - p_min
    pr_ext = [v_dot(p, axis) for f in facce_ext for p in f['punti']]
    len_ext = max(pr_ext) - min(pr_ext)

    r_int = None
    for r in sorted(raggi, reverse=True):
        if r >= r_ext - 0.3:
            continue
        cov, ff = raggi[r]
        pr_i = [v_dot(p, axis) for f in ff for p in f['punti']]
        if cov >= PIENO and (max(pr_i) - min(pr_i)) >= 0.8 * len_ext:
            r_int = r
            break

    d_ext = round(r_ext * 2, 1)
    avvisi = []
    if r_int is not None:
        spessore = round(r_ext - r_int, 1)
        if lunghezza_mm < 0.5 * d_ext:
            return None
        tipo = "CHS"
        profilo = match_profilo_chs(d_ext, spessore, profili_db)
        if profilo:
            nome_profilo = profilo["nome"]
            peso_kg_m = profilo["peso_kg_m"]
            # snap al nominale del profilo DB (nomenclatura commerciale)
            d_ext = profilo.get("d_ext", d_ext)
            spessore = profilo.get("spessore", spessore)
        else:
            nome_profilo = f"Tondo Ø{_fmt(d_ext)} sp.{_fmt(spessore)}mm"
            peso_kg_m = round(peso_teorico_chs(d_ext, spessore), 3)
            avvisi.append("profilo_non_a_catalogo")
    else:
        if lunghezza_mm < 2.0 * d_ext:
            return None
        tipo = "TONDO_PIENO"
        spessore = None
        nome_profilo = f"Tondo pieno Ø{_fmt(d_ext)}mm"
        peso_kg_m = round(peso_teorico_chs(d_ext, None), 3)

    esclusi = {f['id'] for f in g['facce']}
    (taglio_1, ang_1), (taglio_2, ang_2) = _classifica_testate(
        fs, esclusi, axis, p_min, p_max, lambda f: False)

    lunghezza_m = round(lunghezza_mm / 1000.0, 3)
    if debug:
        logger.info("[CHS bid=%s] MATCHED %s d=%s sp=%s len=%.3fm %s/%s", bid,
                    nome_profilo, d_ext, spessore, lunghezza_m, taglio_1, taglio_2)
    return {
        "tipo": tipo,
        "profilo": nome_profilo,
        "d_ext": d_ext,
        "spessore": spessore,
        "lunghezza_m": lunghezza_m,
        "peso_kg": round(peso_kg_m * lunghezza_m, 2),
        "peso_kg_m": peso_kg_m,
        "taglio_1": taglio_1,
        "taglio_2": taglio_2,
        "angolo_taglio_1": ang_1,
        "angolo_taglio_2": ang_2,
        "centroide": _centroide(geo.punti_corpo(bid)),
        "body_id": bid,
        "avvisi": avvisi,
    }


def _tubi_da_corpo_fuso(geo, bid, fs, profili_db):
    """Corpo unico fuso (saldato nel CAD): cerca tratti di tubo tondo VERI,
    cioe' coppie di cilindri coassiali esterno+interno a 360 gradi con
    lunghezza >= 3 D. Nessuna lunghezza di default: i fori di una piastra
    (anche spezzati in due semicilindri) non generano tubi fantasma."""
    out = []
    cil = [f for f in fs if f['tipo'] == 'CYL' and f['raggio'] > 5.0 and f['punti']]
    for g in _gruppi_coassiali(cil):
        raggi = _raggi_pieni(g)
        if len(raggi) < 2:
            continue
        r_ext = max(raggi)
        cov_e, ff_e = raggi[r_ext]
        if cov_e < PIENO:
            continue
        pr = [v_dot(p, g['axis']) for f in ff_e for p in f['punti']]
        lung = max(pr) - min(pr)
        r_int = None
        for r in sorted(raggi, reverse=True):
            if r < r_ext - 0.3 and raggi[r][0] >= PIENO:
                r_int = r
                break
        if r_int is None or lung < 3 * 2 * r_ext:
            continue
        d_ext = round(2 * r_ext, 1)
        sp = round(r_ext - r_int, 1)
        profilo = match_profilo_chs(d_ext, sp, profili_db)
        peso_kg_m = profilo["peso_kg_m"] if profilo else round(peso_teorico_chs(d_ext, sp), 3)
        lunghezza_m = round(lung / 1000.0, 3)
        out.append({
            "tipo": "CHS",
            "profilo": profilo["nome"] if profilo else f"Tondo Ø{_fmt(d_ext)} sp.{_fmt(sp)}mm",
            "d_ext": d_ext,
            "spessore": sp,
            "lunghezza_m": lunghezza_m,
            "peso_kg": round(peso_kg_m * lunghezza_m, 2),
            "peso_kg_m": peso_kg_m,
            "taglio_1": "dritto",
            "taglio_2": "dritto",
            "angolo_taglio_1": 0.0,
            "angolo_taglio_2": 0.0,
            "body_id": bid,
            "stimato": True,
            "avvisi": ["stimato_da_corpo_fuso"] + ([] if profilo else ["profilo_non_a_catalogo"]),
        })
    return out


def analizza_step_tubolari(step_path: str, profili_db: dict, debug: bool = False) -> dict:
    """Analizza file STEP per rilevare strutture tubolari (CHS, RHS, SHS).

    Per ogni corpo solido prova prima il riconoscimento RHS/SHS (piani), poi
    CHS/tondo pieno (cilindri coassiali). Se la sezione non e' a catalogo il
    tubo NON viene scartato: peso dalla geometria misurata + avviso
    'profilo_non_a_catalogo'.

    Args:
        step_path: Path al file STEP.
        profili_db: Database profili tubolari (da carica_profili_tubolari).
        debug: Se True, logga dettagli diagnostici su classificazione candidati.

    Returns:
        dict con 'tubi' (ognuno con 'body_id' e 'avvisi'), 'peso_totale_kg',
        'n_tagli_dritti/obliqui/sagomati', 'body_ids_tubi', 'avvisi', 'errore'.
        I conteggi sono per UNA istanza di ciascun corpo (le quantita'
        d'assieme si applicano a valle, vedi step_assieme.conta_istanze_nauo).
    """
    vuoto = {'tubi': [], 'peso_totale_kg': 0, 'n_tagli_dritti': 0,
             'n_tagli_obliqui': 0, 'n_tagli_sagomati': 0, 'body_ids_tubi': [],
             'avvisi': []}
    try:
        entities, info = carica_entita(step_path)
    except (IOError, OSError) as e:
        return {**vuoto, 'errore': str(e)}

    geo = GeometriaStep(entities)
    body_ids = geo.corpi()
    if not body_ids:
        return {**vuoto, 'avvisi': list(info['avvisi']), 'errore': 'Nessun corpo solido trovato'}

    tubi_rilevati = []
    for bid in body_ids:
        try:
            fs = geo.facce(bid)
            if not fs:
                continue
            tubo = _analizza_rhs(geo, bid, fs, profili_db, debug)
            if tubo is None:
                tubo = _analizza_chs(geo, bid, fs, profili_db, debug)
            if tubo is not None:
                tubi_rilevati.append(tubo)
        except Exception as e:  # un corpo anomalo non deve bloccare l'import
            logger.warning("analisi tubolare corpo #%s fallita: %s", bid, e)

    # --- Caso speciale: corpo unico fuso con tratti di tubo ---
    if not tubi_rilevati:
        for bid in body_ids:
            try:
                tubi_rilevati.extend(_tubi_da_corpo_fuso(geo, bid, geo.facce(bid), profili_db))
            except Exception as e:
                logger.warning("analisi corpo fuso #%s fallita: %s", bid, e)

    # --- Riepilogo ---
    peso_totale = round(sum(t["peso_kg"] for t in tubi_rilevati), 2)
    tagli = [tag for t in tubi_rilevati for tag in (t["taglio_1"], t["taglio_2"])]
    avvisi = list(info['avvisi'])
    for t in tubi_rilevati:
        if "profilo_non_a_catalogo" in t.get("avvisi", []):
            avvisi.append(f"profilo_non_a_catalogo: {t['profilo']} (peso calcolato dalla geometria)")

    return {
        'tubi': tubi_rilevati,
        'peso_totale_kg': peso_totale,
        'n_tagli_dritti': tagli.count("dritto"),
        'n_tagli_obliqui': tagli.count("obliquo"),
        'n_tagli_sagomati': tagli.count("sagomato"),
        'body_ids_tubi': [t["body_id"] for t in tubi_rilevati if not t.get("stimato")],
        'avvisi': avvisi,
        'unita': info['unita'],
        'errore': None if tubi_rilevati else 'Nessun tubo rilevato nel file STEP'
    }


def calcola_costo_tubolare(analisi: dict, config: dict, materiale: str = "acciaio") -> dict:
    """Calcola il costo di una struttura tubolare.

    Args:
        analisi: dict da analizza_step_tubolari().
        config: dict di configurazione applicazione.
        materiale: 'acciaio', 'inox', o 'alluminio'.

    Returns:
        dict con breakdown costi.
    """
    costi_mat = {
        "acciaio": float(config.get("costo_materiale_acciaio_kg", 1.20)),
        "inox": float(config.get("costo_materiale_inox_kg", 4.50)),
        "alluminio": float(config.get("costo_materiale_alluminio_kg", 3.50)),
    }
    costo_mat_kg = costi_mat.get(materiale, costi_mat["acciaio"])

    costo_ora_taglio = float(config.get("costo_orario_taglio_tubo", 40.0))
    tempo_dritto = float(config.get("tempo_taglio_dritto_min", 1.0))
    tempo_obliquo = float(config.get("tempo_taglio_obliquo_min", 2.5))
    tempo_sagomato = float(config.get("tempo_taglio_sagomato_min", 5.0))

    peso_kg = analisi.get('peso_totale_kg', 0)
    n_dritti = analisi.get('n_tagli_dritti', 0)
    n_obliqui = analisi.get('n_tagli_obliqui', 0)
    n_sagomati = analisi.get('n_tagli_sagomati', 0)

    costo_materiale = round(peso_kg * costo_mat_kg, 2)

    costo_taglio_dritto = round(n_dritti * tempo_dritto * costo_ora_taglio / 60, 2)
    costo_taglio_obliquo = round(n_obliqui * tempo_obliquo * costo_ora_taglio / 60, 2)
    costo_taglio_sagomato = round(n_sagomati * tempo_sagomato * costo_ora_taglio / 60, 2)
    costo_taglio_totale = costo_taglio_dritto + costo_taglio_obliquo + costo_taglio_sagomato

    totale = costo_materiale + costo_taglio_totale

    return {
        'costo_materiale': costo_materiale,
        'costo_taglio_dritto': costo_taglio_dritto,
        'costo_taglio_obliquo': costo_taglio_obliquo,
        'costo_taglio_sagomato': costo_taglio_sagomato,
        'costo_taglio_totale': costo_taglio_totale,
        'totale': round(totale, 2),
        'peso_kg': peso_kg,
        'materiale': materiale,
        'n_tagli_dritti': n_dritti,
        'n_tagli_obliqui': n_obliqui,
        'n_tagli_sagomati': n_sagomati,
    }
