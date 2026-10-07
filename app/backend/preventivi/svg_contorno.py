"""Disegno del DXF con sopra il contorno preso da FerroTrack (per la verifica
al laser, Stefano 07/10/2026: "come verifico il contorno?").

Tutto nelle coordinate del DXF (le stesse del detector e di proponi_contorno),
cosi' il contorno cade esattamente sulle linee del disegno. Solo lettura: il
riconoscimento non si tocca, qui si disegna e basta.
"""
from __future__ import annotations

import logging
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

# entita' disegnate (le scritte e le quote si saltano: confondono e basta)
_TIPI = ('LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE')


def _tratti_disegno(dxf_path: str, max_tratti: int = 6000) -> list:
    """Le linee del disegno come liste di punti [(x, y), ...] (archi e curve
    spezzati in segmenti), blocchi compresi."""
    import ezdxf
    from ezdxf import path as ezpath
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    out = []

    def aggiungi(e):
        if len(out) >= max_tratti:
            return
        try:
            p = ezpath.make_path(e)
            pts = [(v.x, v.y) for v in p.flattening(distance=0.2, segments=16)]
            if len(pts) >= 2:
                out.append(pts)
        except Exception:
            pass

    for e in msp:
        t = e.dxftype()
        if t in _TIPI:
            aggiungi(e)
        elif t == 'INSERT':
            try:
                for v in e.virtual_entities():
                    if v.dxftype() in _TIPI:
                        aggiungi(v)
            except Exception:
                pass
    return out


def svg_con_contorni(dxf_path: str, contorni: list, *, larghezza: int = 640) -> str:
    """SVG del disegno con i contorni sopra.

    contorni: [{'punti': [[x, y], ...], 'fori': [[[x, y], ...], ...],
                'colore': '#16a34a', 'tratteggio': False, 'titolo': '...'}]"""
    tratti = _tratti_disegno(dxf_path)
    xs = [x for t in tratti for x, _ in t] + [p[0] for c in contorni for p in (c.get('punti') or [])]
    ys = [y for t in tratti for _, y in t] + [p[1] for c in contorni for p in (c.get('punti') or [])]
    if not xs:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>'
    # inquadra il pezzo (i contorni) con un po' d'aria, non tutto il foglio:
    # sul foglio intero col cartiglio il pezzo diventa un francobollo
    cx = [p[0] for c in contorni for p in (c.get('punti') or [])]
    cy = [p[1] for c in contorni for p in (c.get('punti') or [])]
    if cx:
        minx, maxx, miny, maxy = min(cx), max(cx), min(cy), max(cy)
        w, h = maxx - minx, maxy - miny
        m = max(w, h) * 0.25 + 1
        minx, maxx, miny, maxy = minx - m, maxx + m, miny - m, maxy + m
    else:
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    sw = max(w, h) / 500.0           # spessore delle linee in unita' del disegno

    def pts(lista):
        # y verso l'alto nel DXF, verso il basso nell'SVG
        return ' '.join(f'{x - minx:.3f},{maxy - y:.3f}' for x, y in lista)

    parti = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.3f} {h:.3f}" '
             f'width="{larghezza}" height="{int(larghezza * h / w)}" style="background:#fff">']
    parti.append(f'<g fill="none" stroke="#9aa3ad" stroke-width="{sw:.4f}">')
    for t in tratti:
        parti.append(f'<polyline points="{pts(t)}"/>')
    parti.append('</g>')
    for c in contorni:
        if not c.get('punti'):
            continue
        tr = f' stroke-dasharray="{sw * 6:.3f},{sw * 4:.3f}"' if c.get('tratteggio') else ''
        col = escape(c.get('colore') or '#16a34a')
        fill = 'none' if c.get('tratteggio') else col
        tit = f'<title>{escape(c.get("titolo") or "")}</title>' if c.get('titolo') else ''
        d = 'M ' + ' L '.join(f'{x - minx:.3f},{maxy - y:.3f}' for x, y in c['punti']) + ' Z'
        for f in c.get('fori') or []:
            if len(f) >= 3:
                d += ' M ' + ' L '.join(f'{x - minx:.3f},{maxy - y:.3f}' for x, y in f) + ' Z'
        parti.append(f'<path d="{d}" fill="{fill}" fill-opacity="0.12" fill-rule="evenodd" stroke="{col}" '
                     f'stroke-width="{sw * 3:.4f}"{tr}>{tit}</path>')
    parti.append('</svg>')
    return ''.join(parti)
