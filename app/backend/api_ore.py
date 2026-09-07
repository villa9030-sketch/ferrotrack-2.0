"""API delle DICHIARAZIONI ORE (blueprint isolato dal monolite `app.py`).

Tutti gli endpoint sono protetti da `require_scope`: i permessi derivano dal
TOKEN DI DISPOSITIVO verificato dal server, mai da un ruolo o user_id inviato
dal browser.

  scope 'ore'     -> tablet officina: legge/scrive SOLO dichiarazioni, solo oggi
  scope 'ufficio' -> impiegata: puo' correggere anche i giorni precedenti

Il nome operaio selezionato sul tablet e' una DICHIARAZIONE del soggetto, non
un'autenticazione: viene registrato separatamente dal dispositivo che ha
effettuato l'operazione (tracciabilita' richiesta).
"""
import logging

from flask import Blueprint, jsonify, request

from . import ore_service as svc
from .auth_device import device_corrente, require_scope

logger = logging.getLogger(__name__)

bp_ore = Blueprint('ore', __name__, url_prefix='/api/ore')


def _audit(azione, operatore_id, dettaglio):
    """Traccia la modifica distinguendo operaio dichiarato e dispositivo."""
    try:
        from .database import AuditManager
        dev = device_corrente()
        AuditManager.log(
            user_id=operatore_id,
            action=azione,
            entity_type='ore',
            entity_id=operatore_id or '',
            detail=f"[device={dev.get('label', '?')} scope={dev.get('scope', '?')}] {dettaglio}",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Contesto del dispositivo (il tablet sa cosa puo' fare)
# ---------------------------------------------------------------------------
@bp_ore.route('/contesto', methods=['GET'])
@require_scope('ore', 'ufficio')
def api_contesto():
    dev = device_corrente()
    return jsonify({
        'success': True,
        'dispositivo': dev.get('label'),
        'scope': dev.get('scope'),
        'oggi': svc.oggi_locale().isoformat(),
        'passo_minuti': svc.PASSO_MINUTI,
        'etichetta_interna': svc.ETICHETTA_INTERNA,
    }), 200


# ---------------------------------------------------------------------------
# Anagrafiche
# ---------------------------------------------------------------------------
@bp_ore.route('/operai', methods=['GET'])
@require_scope('ore', 'ufficio')
def api_operai():
    return jsonify({'success': True, 'operai': svc.elenco_operai()}), 200


@bp_ore.route('/clienti', methods=['GET'])
@require_scope('ore', 'ufficio')
def api_clienti():
    return jsonify({'success': True, 'clienti': svc.elenco_clienti()}), 200


# ---------------------------------------------------------------------------
# Giornata
# ---------------------------------------------------------------------------
@bp_ore.route('/giornata', methods=['GET'])
@require_scope('ore', 'ufficio')
def api_leggi_giornata():
    operatore_id = (request.args.get('operatore_id') or '').strip()
    data = (request.args.get('data') or '').strip() or svc.oggi_locale().isoformat()
    if not operatore_id:
        return jsonify({'success': False, 'error': 'operatore_id obbligatorio'}), 400

    dev = device_corrente()
    # Il tablet ore consulta solo la giornata corrente: nient'altro gli serve.
    if dev.get('scope') == 'ore' and data != svc.oggi_locale().isoformat():
        return jsonify({'success': False,
                        'error': 'Questo dispositivo puo\' consultare solo la giornata di oggi.',
                        'codice': 'giorno_non_consentito'}), 403

    g = svc.leggi_giornata(operatore_id, data)
    if g.get('error'):
        return jsonify({'success': False, **g}), 400
    return jsonify({'success': True, 'giornata': g}), 200


@bp_ore.route('/giornata', methods=['POST'])
@require_scope('ore', 'ufficio')
def api_salva_giornata():
    """Salva l'intera giornata (sostituzione atomica).

    Body: {operatore_id, data, righe[], revisione_attesa, richiesta_id, note?}
    L'origine NON viene dal client: e' derivata dallo scope del dispositivo.
    """
    dev = device_corrente()
    data_in = request.get_json(silent=True) or {}

    operatore_id = (data_in.get('operatore_id') or '').strip()
    if not operatore_id:
        return jsonify({'success': False, 'error': 'operatore_id obbligatorio'}), 400

    origine = 'tablet' if dev.get('scope') == 'ore' else 'ufficio'
    giorno = (data_in.get('data') or '').strip() or svc.oggi_locale().isoformat()

    res = svc.salva_giornata(
        operatore_id,
        giorno,
        data_in.get('righe'),
        origine=origine,
        device_label=dev.get('label'),
        modificata_da=dev.get('label'),
        revisione_attesa=data_in.get('revisione_attesa'),
        richiesta_id=(data_in.get('richiesta_id') or '').strip() or None,
        note=data_in.get('note'),
    )

    if res.get('success'):
        g = res['giornata']
        if not g.get('idempotente'):
            _audit('DICHIARAZIONE_ORE', operatore_id,
                   f"{giorno}: {g.get('totale_minuti', 0)} min, rev {g.get('revisione')}")
        # Un salvataggio andato a buon fine puo' risolvere un'anomalia aperta.
        try:
            from .anomalie_service import rivaluta_giornata
            rivaluta_giornata(operatore_id, giorno)
        except Exception:
            pass
        return jsonify(res), 200

    codice = res.get('codice')
    if codice == 'conflitto':
        return jsonify({'success': False, **res}), 409
    if codice in ('operatore_non_valido', 'giorno_non_corrente'):
        return jsonify({'success': False, **res}), 403
    if codice == 'errore_server':
        return jsonify({'success': False, **res}), 500
    return jsonify({'success': False, **res}), 400


# ===========================================================================
#  CONTROLLO MANCANZE — riservato all'UFFICIO (scope 'ufficio')
#  Un tablet di officina non puo' accedere a nulla di tutto questo.
# ===========================================================================
@bp_ore.route('/anomalie', methods=['GET'])
@require_scope('ufficio')
def api_anomalie():
    from . import anomalie_service as an
    stato = (request.args.get('stato') or 'aperta').strip()
    if stato not in ('aperta', 'risolta', 'tutte'):
        stato = 'aperta'
    return jsonify({
        'success': True,
        'anomalie': an.elenco_anomalie(
            stato=None if stato == 'tutte' else stato,
            dal=request.args.get('dal'), al=request.args.get('al')),
    }), 200


@bp_ore.route('/controlla', methods=['POST'])
@require_scope('ufficio')
def api_controlla():
    """Esecuzione manuale del controllo (oltre a quella periodica automatica)."""
    from . import anomalie_service as an
    data = request.get_json(silent=True) or {}
    return jsonify({'success': True,
                    'esito': an.controlla_periodo(data.get('dal'), data.get('al'))}), 200


@bp_ore.route('/configurazione', methods=['GET'])
@require_scope('ufficio')
def api_configurazione():
    from . import anomalie_service as an
    return jsonify({'success': True, 'operai': an.elenco_configurazione()}), 200


@bp_ore.route('/configurazione', methods=['POST'])
@require_scope('ufficio')
def api_salva_configurazione():
    from . import anomalie_service as an
    d = request.get_json(silent=True) or {}
    dev = device_corrente()
    res = an.salva_configurazione(
        (d.get('operatore_id') or '').strip(),
        bool(d.get('tenuto')),
        d.get('minuti_attesi'),
        d.get('giorni_settimana'),
        da=dev.get('label'),
    )
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('CONFIG_ORE_ATTESE', d.get('operatore_id'),
           f"tenuto={d.get('tenuto')} minuti={d.get('minuti_attesi')}")
    return jsonify(res), 200


@bp_ore.route('/eccezione', methods=['POST'])
@require_scope('ufficio')
def api_salva_eccezione():
    """Assenza / giornata ridotta / festivo."""
    from . import anomalie_service as an
    d = request.get_json(silent=True) or {}
    dev = device_corrente()
    res = an.salva_eccezione(
        (d.get('operatore_id') or '').strip(), d.get('data'), d.get('tipo'),
        minuti_attesi=d.get('minuti_attesi'), nota=d.get('nota'),
        da=dev.get('label'),
    )
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('ECCEZIONE_GIORNO', d.get('operatore_id'),
           f"{d.get('data')}: {d.get('tipo')}")
    return jsonify(res), 200


@bp_ore.route('/eccezione', methods=['DELETE'])
@require_scope('ufficio')
def api_elimina_eccezione():
    from . import anomalie_service as an
    d = request.get_json(silent=True) or {}
    res = an.elimina_eccezione((d.get('operatore_id') or '').strip(), d.get('data'))
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('ECCEZIONE_GIORNO_RIMOSSA', d.get('operatore_id'), str(d.get('data')))
    return jsonify(res), 200


# ===========================================================================
#  RIEPILOGO ECONOMICO — riservato all'UFFICIO
#  Nessun dato economico e' esposto ai dispositivi di officina.
# ===========================================================================
@bp_ore.route('/riepilogo', methods=['GET'])
@require_scope('ufficio')
def api_riepilogo():
    from . import riepilogo_service as ri
    return jsonify({'success': True, 'riepilogo': ri.riepilogo(
        anno=request.args.get('anno'), mese=request.args.get('mese'),
        dal=request.args.get('dal'), al=request.args.get('al'),
        cliente=(request.args.get('cliente') or '').strip() or None,
    )}), 200


@bp_ore.route('/riepilogo/dettaglio', methods=['GET'])
@require_scope('ufficio')
def api_riepilogo_dettaglio():
    """Risalita ai dati che compongono i totali di un cliente."""
    from . import riepilogo_service as ri
    cliente = (request.args.get('cliente') or '').strip()
    if not cliente:
        return jsonify({'success': False, 'error': 'cliente obbligatorio'}), 400
    return jsonify({'success': True, 'dettaglio': ri.dettaglio_cliente(
        cliente, anno=request.args.get('anno'), mese=request.args.get('mese'),
        dal=request.args.get('dal'), al=request.args.get('al'),
    )}), 200


@bp_ore.route('/tariffa', methods=['GET'])
@require_scope('ufficio')
def api_tariffe():
    from . import riepilogo_service as ri
    return jsonify({'success': True, 'tariffe': ri.elenco_tariffe()}), 200


@bp_ore.route('/tariffa', methods=['POST'])
@require_scope('ufficio')
def api_imposta_tariffa():
    """Nuova tariffa valida da una data: non altera lo storico precedente."""
    from . import riepilogo_service as ri
    d = request.get_json(silent=True) or {}
    dev = device_corrente()
    res = ri.imposta_tariffa(d.get('valido_dal'), d.get('euro_ora'),
                             da=dev.get('label'), nota=d.get('nota'))
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('COSTO_ORARIO', '', f"{d.get('euro_ora')} EUR/h dal {d.get('valido_dal')}")
    return jsonify(res), 200


@bp_ore.route('/fatturato', methods=['POST'])
@require_scope('ufficio')
def api_salva_fatturato():
    from . import riepilogo_service as ri
    d = request.get_json(silent=True) or {}
    dev = device_corrente()
    res = ri.salva_fatturato(d.get('cliente'), d.get('anno'), d.get('mese'),
                             d.get('importo'), riferimento=d.get('riferimento'),
                             nota=d.get('nota'), da=dev.get('label'))
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('FATTURATO_MANUALE', '',
           f"{d.get('cliente')} {d.get('anno')}-{d.get('mese')}: {d.get('importo')}")
    return jsonify(res), 200


@bp_ore.route('/materiale', methods=['POST'])
@require_scope('ufficio')
def api_salva_materiale():
    """Costo materiali. Cliente vuoto = NON attribuito (non viene ripartito)."""
    from . import riepilogo_service as ri
    d = request.get_json(silent=True) or {}
    dev = device_corrente()
    res = ri.salva_materiale(d.get('cliente'), d.get('anno'), d.get('mese'),
                             d.get('importo'), descrizione=d.get('descrizione'),
                             riferimento=d.get('riferimento'), da=dev.get('label'))
    if res.get('error'):
        return jsonify({'success': False, **res}), 400
    _audit('MATERIALE_MANUALE', '',
           f"{d.get('cliente') or 'non attribuito'} {d.get('anno')}-{d.get('mese')}: {d.get('importo')}")
    return jsonify(res), 200
