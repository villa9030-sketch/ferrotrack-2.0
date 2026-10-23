"""La quadratura di tutti gli ordini aperti, ogni mattina (attivita' pianificata
"FerroTrack quadratura", 06:45, senza finestre: la lancia quadratura_giornaliera.vbs).

Per ogni ordine aperto (non chiuso, che passa dal laser) confronta cosa ha
ordinato il cliente, cosa c'e' in FerroTrack e cosa c'e' in Lantek
(backend/quadratura.py: Lantek SOLO LETTURA). Poi:
  - scrive in logs/quadratura.log una riga per ordine e il totale;
  - se qualcosa non quadra, UNA notifica alla stazione laser e
    all'amministrazione: "Quadratura: 2 ordini non quadrano — A 001221 BIS (9), ...".
    Quelle dei giorni prima non ancora lette si tolgono (vale l'ultima).
Non cambia niente negli ordini, in Lantek o nei file.

Uso (dalla cartella app):
    .venv\\Scripts\\python.exe tools\\quadratura_giornaliera.py            calcola e avvisa
    .venv\\Scripts\\python.exe tools\\quadratura_giornaliera.py --prova    calcola e stampa, niente notifica
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIPO = 'quadratura'


def _log() -> logging.Logger:
    os.makedirs(os.path.join(APP, 'logs'), exist_ok=True)
    lg = logging.getLogger('quadratura_giornaliera')
    lg.setLevel(logging.INFO)
    h = RotatingFileHandler(os.path.join(APP, 'logs', 'quadratura.log'), maxBytes=2 * 1024 * 1024,
                            backupCount=3, encoding='utf-8')
    h.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    lg.addHandler(h)
    if sys.stderr is not None:
        lg.addHandler(logging.StreamHandler())
    return lg


def destinatari() -> list:
    """La stazione laser e l'amministrazione (utenze di postazione attive)."""
    from backend.models import get_session, User, RUOLI_LASER
    s = get_session()
    try:
        return [u.id for u in s.query(User).filter(User.is_active == True,       # noqa: E712
                                                   User.e_postazione == True).all()  # noqa: E712
                if u.role in RUOLI_LASER or u.role == 'Amministrazione']
    finally:
        s.close()


def togli_vecchie() -> int:
    """Le notifiche di quadratura dei giorni prima non ancora lette: vale l'ultima."""
    from backend.models import get_session, Notification
    s = get_session()
    try:
        n = 0
        for x in s.query(Notification).filter(Notification.notification_type == TIPO,
                                              Notification.is_deleted == False,      # noqa: E712
                                              Notification.is_read == False).all():  # noqa: E712
            x.is_deleted = True
            n += 1
        s.commit()
        return n
    finally:
        s.close()


def main(argv) -> int:
    prova = '--prova' in argv
    os.environ.setdefault('FERROTRACK_SKIP_DB_INIT', '1')      # niente migrazioni all'avvio: lo fa il server
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    if APP not in sys.path:
        sys.path.insert(0, APP)
    lg = _log()
    logging.getLogger('backend').setLevel(logging.WARNING)
    from backend import quadratura as q
    t0 = time.time()
    risultati = []
    try:
        ordini = q.ordini_aperti()
    except Exception:
        lg.exception('ordini non letti')
        return 2
    for o in ordini:
        try:
            r = q.calcola(o['id'])
        except Exception as e:
            lg.exception('%s: quadratura non riuscita', o['numero'])
            r = {'order_id': o['id'], 'numero': o['numero'], 'esito': q.NON_VERIFICABILE, 'n_problemi': 0,
                 'testo': f'Quadratura non riuscita: {str(e)[:120]}'}
        if r is None:
            continue
        risultati.append(r)
        cod = ', '.join(x['codice'] for x in (r.get('problemi') or [])[:6])
        lg.info('%s %s: %s%s', o['numero'], o['cliente'], r['testo'], f' ({cod})' if cod else '')
    testo = q.testo_avviso(risultati)
    lg.info('%d ordini controllati in %.0f s. %s', len(risultati), time.time() - t0, testo or 'Tutto torna.')
    if prova:
        return 0
    try:
        tolte = togli_vecchie()
        if tolte:
            lg.info('%d notifiche di quadratura dei giorni prima tolte', tolte)
        if testo:
            from backend.database import NotificationManager
            nq = [r for r in risultati if r.get('esito') == q.NON_QUADRA]
            titolo = (f"Quadratura: {len(nq)} {'ordine non quadra' if len(nq) == 1 else 'ordini non quadrano'}"
                      if nq else 'Quadratura da verificare a mano')
            for uid in destinatari():
                NotificationManager.create_notification(
                    user_id=uid, order_id=None, title=titolo,
                    message=testo.replace("Quadratura: ", "", 1) + f". Controllata il {datetime.now():%d/%m alle %H:%M}: apri l'ordine e guarda «Non quadra».",
                    notification_type=TIPO, notification_category='attiva')
    except Exception:
        lg.exception('notifica della quadratura non creata')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
