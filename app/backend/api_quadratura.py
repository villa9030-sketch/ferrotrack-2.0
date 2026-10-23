"""La quadratura degli ordini nelle pagine (backend/quadratura.py, 23/10/2026).

  GET  /api/orders/<id>/quadratura            la quadratura dell'ordine
         ?attendi=1  se non e' pronta la calcola adesso (qualche secondo la
                     prima volta); senza, parte in sottofondo e si torna
                     quella di prima (pronto=false): la pagina richiede dopo
  GET  /api/quadratura                        il riassunto di tutti gli ordini
                                              aperti, da quello che c'e' gia'
                                              (quelli da rifare partono in sottofondo)
  POST /api/orders/<id>/quadratura/controllato  "Ho controllato" prima di
         mandare a Lantek o segnare il taglio con un ordine che non quadra:
         {"azione": "lantek" | "taglio" | "pacco_lantek" | "pacco_taglio",
          "codici": [...]}  -> nello storico (audit), con chi e quando

Niente qui blocca da solo: la pagina laser chiede la spunta, chi lavora decide.
"""
from __future__ import annotations

import importlib
import logging

from flask import Blueprint, jsonify, request

from .accesso import richiede

logger = logging.getLogger(__name__)

bp_quadratura = Blueprint('quadratura', __name__)

AZIONI = {'lantek': 'mandato a Lantek', 'taglio': 'segnato taglio fatto',
          'pacco_lantek': 'mandato a Lantek col pacco', 'pacco_taglio': 'segnato tagliato col pacco'}


def _app():
    return importlib.import_module(__package__ + '.app')


@bp_quadratura.route('/api/orders/<order_id>/quadratura', methods=['GET'])
@richiede('laser', 'ufficio')
def api_quadratura_ordine(order_id):
    from . import quadratura as q
    try:
        attendi = request.args.get('attendi') in ('1', 'true', 'si')
        r = q.risultato(order_id, attendi=attendi)
        if attendi and r['quadratura'] is None:
            return jsonify({'success': False, 'error': 'Ordine non trovato'}), 404
        return jsonify({'success': True, 'pronto': r['pronto'], 'in_corso': q.in_corso(order_id),
                        'vecchio': r.get('vecchio', False), 'quadratura': r['quadratura']}), 200
    except Exception as e:
        logger.exception('quadratura di %s non riuscita', order_id)
        return jsonify({'success': False, 'error': str(e)[:300]}), 500


@bp_quadratura.route('/api/quadratura', methods=['GET'])
@richiede('laser', 'ufficio')
def api_quadratura_tutti():
    from . import quadratura as q
    try:
        out, da_fare = {}, []
        for o in q.ordini_aperti():
            r, valido = q.in_cache(o['id'])
            if not valido:
                q.in_sottofondo(o['id'])
                da_fare.append(o['id'])
            out[o['id']] = {'numero': o['numero'], 'cliente': o['cliente'], 'pronto': valido,
                            'quadratura': q.riassunto(r)}
        return jsonify({'success': True, 'ordini': out, 'in_corso': da_fare}), 200
    except Exception as e:
        logger.exception('quadratura degli ordini non riuscita')
        return jsonify({'success': False, 'error': str(e)[:300]}), 500


@bp_quadratura.route('/api/orders/<order_id>/quadratura/controllato', methods=['POST'])
@richiede('laser', 'ufficio')
def api_quadratura_controllato(order_id):
    from . import quadratura as q
    corpo = request.get_json(silent=True) or {}
    azione = str(corpo.get('azione') or '')
    if azione not in AZIONI:
        return jsonify({'success': False, 'error': 'azione non valida'}), 400
    codici = [str(c)[:60] for c in (corpo.get('codici') or [])][:200]
    r, _valido = q.in_cache(order_id)
    esito = (r or {}).get('testo') or 'quadratura non calcolata'
    _app()._audit('QUADRATURA_CONTROLLATA', 'orders', order_id,
                  f"{AZIONI[azione]} con l'ordine che non quadra ({esito}). "
                  f"Ha spuntato «Ho controllato» per: {', '.join(codici) or '-'}")
    return jsonify({'success': True}), 200
