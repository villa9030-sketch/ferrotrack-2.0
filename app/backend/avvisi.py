"""Notifiche: solo quelle su cui c'e' ancora qualcosa da fare, raggruppate.

Sulla copia del 13/10/2026 il laser aveva 18 notifiche non lette e l'ufficio
7: "Nuovo ordine" di ordini gia' smistati o eliminati, "Consegna in
scadenza" di ordini gia' tagliati o cancellati, piu' avvisi uguali per lo
stesso ordine. Nessuno le leggeva piu'.

Regole (le applica GET /api/notifications, per la stazione che chiede):
  - un avviso su un ordine eliminato o con la pratica chiusa e' superato;
  - "Nuovo ordine" (tipo order) e' superato quando il laser ha smistato
    l'ordine (o lo ha tagliato); all'ufficio quando l'ordine e' uscito dalla
    lavorazione (lavorazione finita, DDT, consegna);
  - "Consegna in scadenza" (consegna_rischio) e' superato quando l'ordine e'
    tagliato o non e' piu' in lavorazione;
  - "Ordine forse finito" (sospetto_finito) quando la lavorazione e' segnata
    finita;
  - "Da procurare" (procura) quando per quell'ordine non resta niente da
    ordinare o da far arrivare.
Gli avvisi superati si tolgono (come "Elimina", con lo storico in audit).
Quelli rimasti dello stesso tipo sullo stesso ordine diventano UNO, il piu'
recente, con quanti erano. Gli "attivi" (da fare) restano distinti dagli
informativi: `importante`.
"""
from __future__ import annotations


def superata(tipo: str, ordine: dict | None, n_procura_aperti: int = 0, stazione: str = '') -> str | None:
    """Perche' una notifica non serve piu' (None = serve ancora).

    ordine: {'eliminato', 'fase', 'taglio_richiesto', 'tagliato', 'completato'}
    (None = la notifica non riguarda un ordine, o l'ordine non c'e' piu')."""
    if ordine is None:
        return None
    if ordine.get('eliminato'):
        return 'ordine eliminato'
    fase = ordine.get('fase')
    if fase == 'archivio':
        return 'pratica chiusa'
    if tipo == 'order':
        if stazione == 'laser' and (ordine.get('taglio_richiesto') is not None or ordine.get('tagliato')):
            return 'gia\' smistato dal laser'
        if stazione == 'ufficio' and fase != 'aperto':
            return 'ordine gia\' avanti nel ciclo'
        return None
    if tipo == 'consegna_rischio':
        if ordine.get('tagliato'):
            return 'gia\' tagliato'
        if fase != 'aperto':
            return 'lavorazione finita'
        return None
    if tipo == 'sospetto_finito':
        return 'lavorazione segnata finita' if (ordine.get('completato') or fase != 'aperto') else None
    if tipo == 'procura':
        return 'niente piu\' da procurare' if not n_procura_aperti else None
    return None


def raggruppa(notifiche: list) -> list:
    """Una sola voce per ordine e tipo (la piu' recente), con 'ripetute' =
    quante erano; le altre restano nel database. Ordine: dalla piu' recente."""
    visti, out = {}, []
    for n in sorted(notifiche, key=lambda x: x.get('timestamp') or '', reverse=True):
        k = (n.get('order_id'), n.get('notification_type')) if n.get('order_id') else ('id', n.get('id'))
        if k in visti:
            g = visti[k]
            g['ripetute'] += 1
            g['ids_simili'].append(n['id'])
            if not n.get('is_read'):
                g['is_read'] = False
            continue
        g = dict(n, ripetute=1, ids_simili=[],
                 importante=(n.get('notification_category') == 'attiva'))
        visti[k] = g
        out.append(g)
    for g in out:
        if g['ripetute'] > 1:
            prec = g['ripetute'] - 1
            g['message'] = f"{g.get('message') or ''} (+{prec} {'avviso precedente' if prec == 1 else 'avvisi precedenti'})".strip()
    return out
