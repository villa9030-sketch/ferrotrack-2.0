# -*- coding: utf-8 -*-
"""Rimette il prezzo concordato sugli ordini nati prima della correzione.

Il riepilogo economico confronta quanto e' stato pagato (`prezzo_quotato`
sull'ordine) con quanto e' costato in ore. Gli ordini creati prima che
l'accettazione riportasse il totale del preventivo hanno quel campo vuoto: per
loro il riepilogo non ha il lato ricavi, e il margine risulta sempre zero.

Questo strumento ripesca il totale dal preventivo di origine e lo riscrive
sull'ordine. Tocca SOLO gli ordini che hanno il campo vuoto: non sovrascrive
mai un prezzo gia' inserito, che potrebbe essere stato corretto a mano.

    python app/tools/recupera_prezzi_ordini.py            # mostra e basta
    python app/tools/recupera_prezzi_ordini.py --scrivi   # applica

Con FERROTRACK_DB si punta a una copia, per provare senza rischi.
"""
import argparse
import os
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ.setdefault('FERROTRACK_SKIP_DB_INIT', '1')

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scrivi', action='store_true',
                   help='applica davvero (senza, mostra soltanto)')
    args = p.parse_args()

    from backend.models import DATABASE_PATH, get_session, Order, Preventivo
    from backend.database import _totale_concordato

    print('database: %s' % os.path.abspath(DATABASE_PATH))
    print('modo    : %s\n' % ('SCRITTURA' if args.scrivi else 'solo lettura'))

    s = get_session()
    try:
        senza = (s.query(Order)
                 .filter(Order.preventivo_id_origine.isnot(None))
                 .filter((Order.prezzo_quotato.is_(None))
                         | (Order.prezzo_quotato == 0))
                 .all())
        if not senza:
            print('Nessun ordine da recuperare: hanno tutti il prezzo.')
            return 0

        print('%-18s %-24s %12s' % ('ORDINE', 'CLIENTE', 'PREZZO'))
        print('-' * 58)
        fatti = 0
        saltati = []
        for o in senza:
            pr = (s.query(Preventivo)
                  .filter(Preventivo.id == o.preventivo_id_origine).first())
            if not pr:
                saltati.append((o.numero_ordine, 'preventivo di origine sparito'))
                continue
            # Stessa regola dell'accettazione: una sola definizione di prezzo.
            valore = _totale_concordato({
                'totale_lotto': pr.totale_lotto,
                'totale_pezzo_con_margine': pr.totale_pezzo_con_margine,
                'totale_pezzo': pr.totale_pezzo,
                'quantita': pr.quantita,
            })
            if valore is None:
                saltati.append((o.numero_ordine, 'il preventivo non ha un totale'))
                continue
            print('%-18s %-24s %12.2f' % (o.numero_ordine or o.id[:8],
                                          (o.cliente or '')[:24], valore))
            if args.scrivi:
                o.prezzo_quotato = valore
            fatti += 1

        if saltati:
            print('\nSaltati (vanno messi a mano):')
            for n, perche in saltati:
                print('  %-18s %s' % (n, perche))

        if args.scrivi:
            s.commit()
            print('\n%d ordini aggiornati.' % fatti)
        else:
            print('\n%d ordini da aggiornare. Rilancia con --scrivi per applicare.'
                  % fatti)
        return 0
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


if __name__ == '__main__':
    sys.exit(main())
