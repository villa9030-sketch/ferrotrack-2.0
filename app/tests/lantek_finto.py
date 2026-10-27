"""Lantek finto per i test di "Manda a Lantek": gli ordini di produzione che
Lantek "ha creato" sono quelli degli XML preparati da FerroTrack
(lantek.xml_ordini_produzione), riletti per riferimento come fa
lantek.ordini_per_riferimento dopo l'invio.

    from tests.lantek_finto import usa
    usa(L)                      # dopo aver finto importa_xml & co.
    usa(L, cambia={'A1-00': 5}) # Lantek che ha creato A1-00 con 5 pezzi
"""
import re

XML = []
CAMBIA = {}


def _ordini(riferimenti):
    out = {}
    for t in XML:
        for cmd in re.findall(r'<COMMAND.*?</COMMAND>', t, re.S):
            f = dict(re.findall(r'FldRef="(\w+)" FldValue="([^"]*)"', cmd))
            q = float(CAMBIA.get(f['Product'], f['Quantity']))
            if q >= 0:
                out[f['Reference'].upper()] = {'codice': f['Product'], 'quantita': q}
    chiesti = {str(r).upper() for r in riferimenti or []}
    return {k: v for k, v in out.items() if k in chiesti}


def usa(L, cambia=None):
    """Da qui in poi Lantek "crea" quello che c'e' negli XML. cambia: {codice:
    quantita'} per far creare a Lantek una quantita' diversa (-1 = non lo crea)."""
    CAMBIA.clear()
    CAMBIA.update(cambia or {})
    vero = getattr(L, '_xml_vero', None) or L.xml_ordini_produzione
    L._xml_vero = vero

    def xml(*a, **k):
        b = vero(*a, **k)
        XML.append(b.decode('utf-8'))
        return b
    L.xml_ordini_produzione = xml
    L.ordini_per_riferimento = _ordini
