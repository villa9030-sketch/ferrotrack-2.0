"""Quando l'import si dice SICURO di un disegno (pulizia 'auto', nessuna revisione).

Il detector da' una confidenza alla SCELTA del contorno; qui si raccolgono gli
indizi indipendenti che dicono se la lettura torna davvero (peso del
cartiglio, misure del cartiglio, quote, fonti dello spessore, geometria dentro
il pezzo che non e' ne' taglio ne' piega) e si decide se il disegno puo'
restare "sicuro" o va messo "da verificare".

Solo lettura: non cambia il contorno ne' le misure. Un disegno non sicuro
resta col suo contorno, ma la confidenza scende sotto la soglia del sicuro
(0,7) e la pulizia diventa 'auto_review'.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Confidenza data a un disegno che il controllo toglie dai "sicuri": sotto 0,7
# (soglia di 'auto'), sopra 0,5 (non chiede la scelta manuale del pezzo).
CONF_DA_VERIFICARE = 0.65


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def raccogli_indizi(dxf_path: str, geo: dict | None, cartiglio: dict | None, spessore: dict | None,
                    dim_info: dict | None, cleanup: dict | None, conf_prima_cartiglio: float | None) -> dict:
    """Indizi (numeri e flag) per decidi_sicuro. Non solleva eccezioni."""
    geo = geo or {}
    ind: dict = {}
    try:
        ind['diag'] = dict(geo.get('diagnostica') or {})
        ind['conf'] = _f(geo.get('confidence'), 0.0)
        ind['conf_detector'] = _f(conf_prima_cartiglio, ind['conf'])
        ind['n_pezzi'] = geo.get('n_pezzi_rilevati')
        ind['avvisi'] = [str(w)[:90] for w in (geo.get('warnings') or [])]
        ind['area_dm2'] = _f(geo.get('area_dm2'), 0.0)
        ind['area_lorda_dm2'] = _f(geo.get('area_lorda_dm2'), 0.0)
        w, h = _f(geo.get('bbox_width_mm'), 0.0), _f(geo.get('bbox_height_mm'), 0.0)
        ind['bbox'] = [w, h]
        ind['scala'] = _f(geo.get('scala_unita_mm'), 1.0)
        # fori da trapano (sotto 2/3 dello spessore): tolti dal taglio, o dubbio
        # se lo spessore non e' sicuro (dxf_batch_worker.applica_fori_trapano)
        ind['n_fori_trapano'] = len(geo.get('fori_trapano') or [])
        ind['fori_trapano_dubbio'] = bool(geo.get('fori_trapano_dubbio'))
        ind['fori_stretti_non_tondi'] = int(geo.get('fori_stretti_non_tondi') or 0)
        # misure del cartiglio (descrizione "120x80 sp.3" o cartiglio tabellare)
        if dim_info and dim_info.get('dim_x_mm') and dim_info.get('dim_y_mm'):
            a = sorted((w, h))
            b = sorted((_f(dim_info.get('dim_x_mm'), 0.0), _f(dim_info.get('dim_y_mm'), 0.0)))
            ind['dim_cartiglio'] = b
            ind['dim_cartiglio_ok'] = bool(min(b) > 0 and all(
                abs(x - y) <= max(1.0, 0.01 * y) for x, y in zip(a, b)))
            ind['dim_cartiglio_area'] = _f(dim_info.get('area_dm2'))
        # spessore e sue fonti
        sp = spessore or {}
        ind['sp_mm'] = _f(sp.get('spessore_mm'))
        ind['sp_conf'] = _f(sp.get('confidence'), 0.0)
        ind['sp_fonte'] = sp.get('source')
        fonti = sp.get('fonti') or []
        ind['sp_fonti'] = [[str(x.get('source')), _f(x.get('spessore_mm'))] for x in fonti][:8]
        ind['sp_discorde'] = any('discordante' in str(x).lower() for x in (sp.get('warnings') or []))
        # spessore da una fonte che NON e' il peso (altrimenti il controllo peso e' circolare)
        sp_indip = [x for x in fonti if str(x.get('source') or '').split('+')[0] != 'peso_area'
                    and _f(x.get('spessore_mm')) and (_f(x.get('confidence'), 0) or 0) >= 0.5]
        ind['sp_indipendente'] = _f(sp_indip[0]['spessore_mm']) if sp_indip else None
        # materiale e peso
        ca = cartiglio or {}
        ind['mat'] = ca.get('materiale')
        ind['mat_conf'] = _f(ca.get('confidence'), 0.0)
        try:
            from .dxf_scanner import estrai_peso_da_cartiglio
            pc = estrai_peso_da_cartiglio(dxf_path) or {}
        except Exception:
            pc = {}
        ind['peso_cart'] = _f(pc.get('peso_kg'))
        ind['peso_cart_conf'] = _f(pc.get('confidence'), 0.0)
        rho = None
        if ind['mat'] and ind['mat_conf'] >= 0.5:
            try:
                from .verifica_ordine import densita_materiale
                rho = densita_materiale(ind['mat']) or None
            except Exception:
                rho = None
        ind['densita'] = rho
        t = ind['sp_indipendente'] or ind['sp_mm']
        ind['peso_calc'] = (ind['area_dm2'] * t / 100.0 * (rho or 7.85)) if t and ind['area_dm2'] else None
        if ind['peso_calc'] and ind['peso_cart']:
            ind['peso_rapporto'] = round(ind['peso_calc'] / ind['peso_cart'], 4)
        # pulizia: cosa c'e' dentro il pezzo oltre al taglio
        st = (cleanup or {}).get('cleanup_stats') or {}
        for k in ('n_taglio', 'n_piega', 'n_marcatura', 'n_simboli_tolti', 'copertura', 'n_lung_marcatura_mm'):
            if k in st:
                ind['pul_' + k] = st[k]
        ind['pul_avvisi'] = [str(w)[:90] for w in (st.get('warnings') or [])]
    except Exception as e:      # noqa: BLE001
        logger.warning('indizi sicurezza %s: %s', dxf_path, e)
        ind['errore'] = str(e)[:120]
    return ind


# Soglie (taratura sull'archivio Lantek, banco_motore/agente_G: meta' delle
# cartelle d'ordine per tarare, l'altra meta' per verificare)
PESO_TOLL_CONFERMA = 0.08       # peso calcolato entro l'8% del cartiglio = conferma
PESO_LEGGERO = 0.92             # sotto il 92% del peso del cartiglio = manca materiale
MARCATURA_MAX_MM = 20.0         # linee dentro il pezzo che non sono ne' taglio ne' piega
# (la vecchia regola "foro piu' piccolo di 0,8 x spessore" non c'e' piu': i fori
# sotto i 2/3 dello spessore vanno al trapano e si tolgono dal taglio da soli,
# dxf_batch_worker.applica_fori_trapano; resta il dubbio solo se lo spessore
# non e' sicuro)
PIEGA_DENTRO_MAX = 3            # entita' col colore/layer di piega dentro il pezzo
SIMBOLI_FORI_MAX = None         # fori con simbolo di filettatura/svasatura: NON e' un dubbio.
                                # Regola di Stefano (08/10/2026): i fori filettati si tagliano al
                                # laser e poi si filettano. Archivio: +398 sicuri, precisione 99,0%


def rapporto_peso(ind: dict) -> float | None:
    """Peso calcolato (area x spessore x densita') / peso del cartiglio, con
    uno spessore letto da una fonte che NON e' il peso stesso; None se non
    confrontabile."""
    pc, t, a = ind.get('peso_cart'), ind.get('sp_indipendente'), ind.get('area_dm2')
    if not pc or not t or not a or (ind.get('peso_cart_conf') or 0) < 0.5:
        return None
    return a * t / 100.0 * (ind.get('densita') or 7.85) / pc


def decidi_sicuro(ind: dict) -> tuple[bool, list[str]]:
    """(sicuro, motivi): motivi = perche' il disegno va verificato.

    Il contorno scelto dal detector resta "sicuro" solo se:
    - qualcosa di indipendente dalla scelta lo conferma (peso del cartiglio,
      misure del cartiglio, o almeno un lato dell'ingombro e' una quota);
    - nessun indizio dice che cosa si taglia e' dubbio (fori come lettere,
      linee dentro il pezzo che non sono ne' taglio ne' piega, fori piccoli
      con lo spessore incerto, geometria col colore di piega);
    - il peso del cartiglio non dice che manca materiale;
    - il disegno non e' stato riportato in scala."""
    if not ind or ind.get('errore'):
        return False, ['indizi non calcolati']
    d = ind.get('diag') or {}
    motivi = []
    r = rapporto_peso(ind)
    # 1. Fori dubbi: contorni a forma di lettera o in fila come una scritta alta
    #    (le misure del cartiglio confermano il contorno, non cosa c'e' dentro)
    if (d.get('n_segni') or 0) > 0 or (d.get('n_scritte_dubbie') or 0) > 0:
        motivi.append('fori a forma di lettera/scritta: tagliare o marcare?')
    # 2. Contorni attaccati senza prova che siano falde: la scelta e' un'ipotesi
    if (d.get('n_attaccati') or 0) > 0:
        motivi.append('contorni attaccati al pezzo senza linee di piega')
    # 3. Nessuna conferma indipendente
    confermato = ((r is not None and abs(r - 1) <= PESO_TOLL_CONFERMA)
                  or ind.get('dim_cartiglio_ok') is True
                  or (d.get('lati_quotati') or 0) >= 1)
    if not confermato:
        motivi.append("nessun riscontro: ne' peso ne' misure del cartiglio ne' quote dell'ingombro")
    # 4. Il pezzo pesa meno di quanto dice il cartiglio: manca materiale
    if r is not None and r < PESO_LEGGERO:
        motivi.append(f'peso calcolato {r * 100:.0f}% del peso del cartiglio')
    # 5. Linee dentro il pezzo che non sono ne' taglio ne' piega (tagli aperti, incisioni)
    if (ind.get('pul_n_lung_marcatura_mm') or 0) > MARCATURA_MAX_MM:
        motivi.append(f"{ind['pul_n_lung_marcatura_mm']:.0f} mm di linee dentro il pezzo non chiuse")
    # 6. Fori piccoli con lo spessore incerto: laser o trapano dipende dallo
    #    spessore (regola dei 2/3), quindi non si puo' decidere da soli
    if ind.get('fori_trapano_dubbio'):
        motivi.append('fori piccoli: spessore da confermare per decidere laser o trapano')
    #    Contorni NON tondi piu' stretti del minimo laser: il trapano non li fa
    #    e il laser non li taglia bene (tagliare, forare o marcare?)
    if ind.get('fori_stretti_non_tondi'):
        motivi.append(f"{ind['fori_stretti_non_tondi']} contorni non tondi sotto il minimo laser "
                      f"(2/3 dello spessore): tagliare, forare o marcare?")
    # 7. Geometria col colore/layer di piega dentro il pezzo: pieghe o contorni da tagliare?
    if (ind.get('pul_n_piega') or 0) > PIEGA_DENTRO_MAX:
        motivi.append(f"{ind['pul_n_piega']} linee di piega dentro il pezzo")
    # 8. Disegno riportato in scala (unita' o quote): fattore da confermare
    if abs((ind.get('scala') or 1.0) - 1.0) > 1e-6:
        motivi.append(f"disegno in scala (x{ind['scala']:g})")
    # 9. Molti fori con simbolo di filettatura/svasatura
    if SIMBOLI_FORI_MAX is not None and (ind.get('pul_n_simboli_tolti') or 0) > SIMBOLI_FORI_MAX:
        motivi.append(f"{ind['pul_n_simboli_tolti']} simboli di filettatura/svasatura sui fori")
    return (not motivi), motivi
