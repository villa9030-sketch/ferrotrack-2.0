"""Aggiunge a un ordine gia' caricato gli assiemi / i disegni che mancano.

    python tools\\aggiungi_disegni_ordine.py <id ordine> <cartella del pacchetto> [--applica]

Senza --applica (prova): legge il pacchetto con la stessa strada dell'import,
stampa il piano (assiemi, pezzi per assieme, materiale, spessore, pezzi gia'
nell'ordine, bloccati) e poi butta via il preventivo d'appoggio: l'ordine non
cambia. Con --applica: stesso piano, poi i pezzi entrano nell'ordine.

Usa il database dell'installazione (o FERROTRACK_DB / FERROTRACK_UPLOADS se
impostati). Fare prima il backup. Lantek non si lancia mai: i codici gia'
mandati restano mandati, "Manda a Lantek" proporra' solo i nuovi.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('order_id')
    ap.add_argument('cartella')
    ap.add_argument('--applica', action='store_true', help='aggiunge davvero i pezzi all\'ordine')
    ap.add_argument('--chi', default='strumento aggiungi_disegni_ordine')
    ap.add_argument('--json', action='store_true', help='stampa anche il piano in JSON')
    args = ap.parse_args(argv)
    if not os.path.isdir(args.cartella):
        print('Cartella non trovata:', args.cartella)
        return 2

    from backend import aggiungi_disegni as AD
    from backend.database import AuditManager

    zb = AD.zip_da_cartella(args.cartella)
    try:
        p = AD.analizza(args.order_id, zb, os.path.basename(args.cartella.rstrip('\\/')), None)
    except AD.Errore as e:
        print(f'ERRORE ({e.codice}): {e.messaggio}')
        return 1
    print(AD.descrivi(p))
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=1))
    if not args.applica:
        AD.scarta(args.order_id, p['tecnico_id'])
        print('\nPROVA: niente e\' cambiato. Per aggiungere davvero: rilancia con --applica')
        return 0
    try:
        r = AD.applica(args.order_id, p['tecnico_id'], args.chi)
    except AD.Errore as e:
        print(f'ERRORE ({e.codice}): {e.messaggio}')
        AD.scarta(args.order_id, p['tecnico_id'])
        return 1
    try:
        AuditManager.log(user_id=None, user_name=args.chi, action='ORDINE_AGGIUNTI_DISEGNI',
                         entity_type='order', entity_id=args.order_id,
                         detail=(f"Aggiunti {r['aggiunti']} pezzi, assiemi {', '.join(r['assiemi'])}; "
                                 f"bloccati {', '.join(r['bloccati']) or '-'}")[:2000])
    except Exception as e:  # noqa: BLE001
        print('audit non registrato:', e)
    print('\nAPPLICATO:', json.dumps({k: v for k, v in r.items() if k != 'export'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
