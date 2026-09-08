"""Generatore PDF cartellino A6 per ordine officina.

Layout: una pagina A6 con barcode Code128 grande, numero ordine in chiaro,
cliente, data consegna. Stampato dall'impiegata e attaccato sul faldone
fisico dell'ordine — l'operaio lo scansiona con la pistola WiFi.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

import barcode
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import A6
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

logger = logging.getLogger(__name__)


def _render_barcode_png(value: str) -> bytes:
    """Genera un PNG del barcode Code128 in memoria."""
    code128 = barcode.get_barcode_class('code128')
    options = {
        'module_width': 0.35,
        'module_height': 18.0,
        'quiet_zone': 4.0,
        'font_size': 0,        # numero gestito da noi sotto, evita doppio
        'text_distance': 1.0,
        'write_text': False,
        'background': 'white',
        'foreground': 'black',
    }
    buf = io.BytesIO()
    code128(value, writer=ImageWriter()).write(buf, options=options)
    return buf.getvalue()


def _barcode_da_stampare() -> bool:
    """Il barcode ha senso solo se qualcuno puo' leggerlo.

    Le pistole sono dismesse: stampare un codice che nessuno scansiona occupa
    meta' etichetta e fa credere a chi la vede che ci sia un passaggio da fare.
    Se un domani le pistole tornano, torna anche il barcode.
    """
    try:
        from .database import BarcodeManager
        return BarcodeManager.pistole_attive()
    except Exception:
        logger.warning('stato pistole non leggibile: cartellino senza barcode')
        return False


def genera_cartellino_pdf(
    codice: str,
    cliente: str = '',
    data_consegna: datetime | None = None,
    note: str = '',
) -> bytes:
    """Ritorna i byte di un PDF A6 col cartellino dell'ordine.

    Args:
        codice: numero ordine (es. "ORD-204-2026") — usato sia come testo
            che come contenuto del barcode.
        cliente: nome cliente.
        data_consegna: data consegna (datetime o None).
        note: testo libero opzionale (es. "URGENTE").

    Returns:
        bytes del PDF pronto da servire come application/pdf.
    """
    from reportlab.lib.utils import ImageReader

    codice = (codice or '').strip() or 'NO-CODE'

    # A6: 105mm x 148mm (verticale)
    w, h = A6  # in punti (1 pt = 1/72 inch)

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A6)

    # Header: cliente in alto + data consegna
    c.setFont('Helvetica-Bold', 11)
    c.drawString(6 * mm, h - 8 * mm, (cliente or '—')[:28])
    if data_consegna:
        try:
            data_txt = data_consegna.strftime('%d/%m/%Y')
        except Exception:
            data_txt = str(data_consegna)[:10]
        c.setFont('Helvetica', 9)
        c.drawRightString(w - 6 * mm, h - 8 * mm, f'Consegna: {data_txt}')

    # Linea sotto header
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.4)
    c.line(6 * mm, h - 10 * mm, w - 6 * mm, h - 10 * mm)

    col_barcode = _barcode_da_stampare()

    # Barcode al centro, solo se c'e' qualcosa che lo legge
    if col_barcode:
        try:
            png_bytes = _render_barcode_png(codice)
            img = ImageReader(io.BytesIO(png_bytes))
            # Posizionamento barcode: centrato orizzontalmente, ~metà altezza
            bw_mm = 88  # larghezza barcode in mm (max ~93 per A6)
            bh_mm = 30  # altezza barcode in mm
            bw = bw_mm * mm
            bh = bh_mm * mm
            x = (w - bw) / 2
            y = h - 60 * mm  # un po' sotto l'header
            c.drawImage(img, x, y, width=bw, height=bh, preserveAspectRatio=True, anchor='c')
        except Exception as e:
            logger.error('Errore rendering barcode per %s: %s', codice, e)
            c.setFont('Helvetica-Bold', 10)
            c.drawCentredString(w / 2, h - 40 * mm, f'[ERRORE BARCODE: {e}]')

    # Il numero dell'ordine: e' quello che si legge sul faldone, da lontano.
    # Senza barcode c'e' lo spazio per scriverlo grande davvero.
    if col_barcode:
        c.setFont('Helvetica-Bold', 18)
        c.drawCentredString(w / 2, h - 70 * mm, codice)
    else:
        c.setFont('Helvetica-Bold', 30 if len(codice) <= 16 else 22)
        c.drawCentredString(w / 2, h - 45 * mm, codice)

    # Note opzionali in basso
    if note:
        c.setFont('Helvetica-Oblique', 9)
        # Wrap manuale semplice
        max_chars = 38
        lines = []
        s = note.strip()
        while s:
            lines.append(s[:max_chars])
            s = s[max_chars:]
        y_n = 25 * mm
        for line in lines[:3]:
            c.drawString(6 * mm, y_n, line)
            y_n -= 4 * mm

    # Footer
    c.setFont('Helvetica', 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawCentredString(
        w / 2, 6 * mm,
        'FerroTrack — scansiona col lettore barcode' if col_barcode
        else 'FerroTrack — incolla sul faldone dell\'ordine')

    c.showPage()
    c.save()
    return buf.getvalue()
