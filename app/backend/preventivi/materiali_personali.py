"""Materiali aggiunti nelle Impostazioni (es. C75 a 6,5 €/kg, "taglia come" S235).

Il lettore del cartiglio riconduce ogni acciaio al carbonio (C45, C75, C70...)
alla famiglia S235: per il prezzo e' sbagliato quando il materiale e' stato
aggiunto con un suo €/kg. Qui, DOPO l'analisi del DXF (che resta com'e' ed e'
in cache), il testo del materiale letto nel cartiglio si confronta coi nomi
dei materiali aggiunti: se corrisponde, il pezzo prende quel materiale.
"""
from __future__ import annotations

import re


def personali(materiali_cfg: dict | None) -> dict:
    """{nome: dati} dei soli materiali aggiunti (quelli con "taglia come")."""
    return {k: v for k, v in (materiali_cfg or {}).items()
            if isinstance(v, dict) and v.get('taglio_come')}


def _regex_nome(nome: str):
    """'C75' trova 'C75', 'C 75', 'c-75', 'C75S', 'Acciaio C75 bonificato',
    non 'C750' ne' 'XC75'."""
    pezzi = [p for p in re.split(r'[\s_\-./]+', nome.upper()) if p]
    corpo = r'[\s_\-./]?'.join(
        r'[\s_\-./]?'.join(re.escape(t) for t in re.findall(r'[A-Z]+|\d+', p)) for p in pezzi)
    return re.compile(r'(?<![A-Z0-9])' + corpo + r'(?!\d)', re.I)


def trova(testo: str | None, materiali_cfg: dict | None) -> str | None:
    """Nome del materiale aggiunto citato nel testo, o None. A parita' vince il
    nome piu' lungo (C75S prima di C75)."""
    if not testo:
        return None
    t = str(testo).upper()
    for nome in sorted(personali(materiali_cfg), key=len, reverse=True):
        if _regex_nome(nome).search(t):
            return nome
    return None


def applica_a_risultato(payload: dict, materiali_cfg: dict | None) -> dict:
    """Corregge il materiale del cartiglio nel risultato dell'import di un DXF."""
    try:
        cart = (payload or {}).get('cartiglio')
        if not isinstance(cart, dict):
            return payload
        nome = trova(cart.get('materiale_raw'), materiali_cfg)
        if nome and nome != cart.get('materiale'):
            payload['cartiglio'] = dict(cart, materiale=nome,
                                        materiale_famiglia=cart.get('materiale'),
                                        confidence=max(float(cart.get('confidence') or 0), 0.8))
    except Exception:
        pass
    return payload
