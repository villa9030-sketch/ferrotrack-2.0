"""Le PAROLE degli stati di un ordine: le stesse in ogni pagina.

Prima lo stesso stato aveva nomi diversi: l'ufficio diceva "Da importare",
il laser "Pronto per Lantek", il capo officina "Da tagliare"; il calendario
dell'ufficio diceva "Finito" a un ordine con la lavorazione finita ma non
ancora consegnato, e il capo officina prometteva che "Lavoro finito" lo
mandava "Da fatturare" (nell'ufficio diventa "DDT da fare").

Questa e' l'unica tabella. Le pagine la leggono da /ft-stati.js (generato da
qui, vedi js()): window.FTStati.laser[fase], window.FTStati.passo[passo].

  laser (ordini_service.fase_laser):
    da_smistare   Da smistare          il laser non ha ancora deciso se passa da lui
    da_importare  Da mandare a Lantek  va tagliato, non ancora in Lantek
    in_lantek     In Lantek            mandato a Lantek, da tagliare
    tagliato      Tagliato             taglio fatto
    non_serve     Senza laser          non passa dal laser

  ordine, ciclo dell'ufficio (ordini_service.passo):
    in_lavorazione     In lavorazione
    attesa_ddt         DDT da fare          lavorazione finita
    ddt_registrato     Da consegnare        DDT registrato
    consegna_parziale  Consegna parziale
    da_fatturare       Da fatturare         consegnato
    chiuso             Fatturato e chiuso

"Finito" da solo non si usa: lavorazione finita e' "DDT da fare", la merce
consegnata e' "Da fatturare", la pratica chiusa e' "Fatturato e chiuso".
"""
import json

LASER = {
    'da_smistare': 'Da smistare',
    'da_importare': 'Da mandare a Lantek',
    'in_lantek': 'In Lantek',
    'tagliato': 'Tagliato',
    'non_serve': 'Senza laser',
}
LASER_SPIEGA = {
    'da_smistare': 'Il laser non ha ancora deciso se l’ordine passa da lui',
    'da_importare': 'Va tagliato: il laser deve ancora mandarlo a Lantek',
    'in_lantek': 'Mandato a Lantek: da tagliare',
    'tagliato': 'Taglio laser fatto',
    'non_serve': 'Non passa dal laser',
}

PASSO = {
    'in_lavorazione': 'In lavorazione',
    'attesa_ddt': 'DDT da fare',
    'ddt_registrato': 'Da consegnare',
    'consegna_parziale': 'Consegna parziale',
    'da_fatturare': 'Da fatturare',
    'chiuso': 'Fatturato e chiuso',
}
PASSO_SPIEGA = {
    'in_lavorazione': 'In produzione',
    'attesa_ddt': 'Lavorazione finita: da preparare il DDT',
    'ddt_registrato': 'DDT registrato: da consegnare',
    'consegna_parziale': 'Consegnato in parte: resta un residuo',
    'da_fatturare': 'Consegnato: da fatturare',
    'chiuso': 'Fatturato e chiuso',
}

# Stato storico (colonna orders.status) -> passo, per le pagine che hanno solo
# lo stato (calendario). DA_FATTURARE e' "lavorazione finita" (vedi
# ordini_service.fase), non "consegnato".
DA_STATUS = {
    'RICEVUTO': 'in_lavorazione',
    'DA_FATTURARE': 'attesa_ddt',
    'SPEDITO': 'chiuso',
    'CHIUSO': 'chiuso',
    'ARCHIVIATO': 'chiuso',
}


def js() -> str:
    """Il file /ft-stati.js: la stessa tabella per il browser."""
    dati = {'laser': LASER, 'laserSpiega': LASER_SPIEGA, 'passo': PASSO, 'passoSpiega': PASSO_SPIEGA,
            'daStatus': DA_STATUS}
    return ('/* Generato da backend/stati.py: le parole degli stati, uguali in ogni pagina. */\n'
            'window.FTStati = ' + json.dumps(dati, ensure_ascii=False) + ';\n'
            # fase laser di un ordine qualunque (stessa regola di ordini_service.fase_laser)
            'window.FTStati.faseLaser = function (o) {\n'
            "  if (!o) return null;\n"
            "  if (o.taglio_completato || o.taglio_fatto) return 'tagliato';\n"
            "  if (o.taglio_richiesto == null) return 'da_smistare';\n"
            "  if (o.taglio_richiesto === false) return 'non_serve';\n"
            "  return o.importato_lantek_il ? 'in_lantek' : 'da_importare';\n"
            '};\n')
