"""Professional PDF quote exporter using ReportLab.

Generates polished, client-ready PDF preventivo documents with:
- Company branding header with logo support
- Article detail tables with alternating row colours
- Cost breakdown with visual bar charts
- Assembly, tubular and plate sections
- Prominent totals box
- Footer with terms and page numbers
"""

import logging
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.piecharts import Pie

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_W, _H = A4  # 595.27, 841.89 points
_MARGIN = 25 * mm


def _eur(value: float) -> str:
    """Format a number as EUR currency string."""
    return f"EUR {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eur_plain(value: float) -> str:
    """Format number with 2 decimals, comma as decimal separator."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------------
# PDF Generator
# ---------------------------------------------------------------------------


class PDFPreventivo:
    """Generate professional PDF quotes for clients."""

    def __init__(self, config: dict):
        """Initialize with app config for pricing/cost parameters.

        Args:
            config: Application configuration dictionary.
        """
        self.config = config

        # Colours matching the app design system
        # Verde brand Carpenteria L.S. — più chiaro del logo così risalta
        self.COLOR_PRIMARY = colors.Color(38 / 255, 162 / 255, 105 / 255)  # #26A269 verde
        self.COLOR_PRIMARY_LIGHT = colors.Color(
            220 / 255, 243 / 255, 228 / 255
        )  # verde chiaro (#DCF3E4)
        self.COLOR_SUCCESS = colors.Color(5 / 255, 150 / 255, 105 / 255)  # #059669
        self.COLOR_DARK = colors.Color(15 / 255, 23 / 255, 42 / 255)  # #0F172A
        self.COLOR_MUTED = colors.Color(148 / 255, 163 / 255, 184 / 255)  # #94A3B8
        self.COLOR_LIGHT = colors.Color(250 / 255, 250 / 255, 250 / 255)  # #FAFAFA
        self.COLOR_BORDER = colors.Color(226 / 255, 232 / 255, 240 / 255)  # #E2E8F0
        self.COLOR_ROW_ALT = colors.Color(248 / 255, 250 / 255, 252 / 255)  # #F8FAFC
        self.COLOR_WHITE = colors.white
        self.COLOR_WARNING = colors.Color(217 / 255, 119 / 255, 6 / 255)  # #D97706

        self._init_styles()

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def _init_styles(self):
        """Create paragraph styles used across the PDF."""
        self.styles = getSampleStyleSheet()

        self.style_title = ParagraphStyle(
            "Title2",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=26,
            textColor=self.COLOR_DARK,
        )
        self.style_subtitle = ParagraphStyle(
            "Subtitle2",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=self.COLOR_MUTED,
        )
        self.style_heading = ParagraphStyle(
            "Heading2",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=16,
            textColor=self.COLOR_PRIMARY,
            spaceBefore=14,
            spaceAfter=6,
        )
        self.style_body = ParagraphStyle(
            "Body2",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=self.COLOR_DARK,
        )
        self.style_body_bold = ParagraphStyle(
            "BodyBold2",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=self.COLOR_DARK,
        )
        self.style_small = ParagraphStyle(
            "Small2",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=10,
            textColor=self.COLOR_MUTED,
        )
        self.style_note = ParagraphStyle(
            "Note2",
            parent=self.styles["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=11,
            textColor=colors.Color(71 / 255, 85 / 255, 105 / 255),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def genera_pdf(self, path: str, dati: dict, interno: bool = False) -> str:
        """Generate a complete PDF quote.

        Args:
            path: Output PDF file path.
            interno: Se True genera la distinta INTERNA completa (costi
                scomposti, margine, BOM, distinta taglio) per uso ufficio.
                Se False (default) genera il PDF CLIENTE: elenco pezzi con
                prezzo finale per riga e totale, senza rivelare costi/margine.
            dati: Dictionary with quote data. Keys:
                - cliente: str
                - numero_ordine: str
                - data: str (dd/mm/yyyy) or None for today
                - articoli: list of article dicts
                - quantita: int
                - margine: float (percentage)
                - costi_montaggio: dict {codice_assieme: cost_data}
                - tubolari_per_assieme: dict
                - piastre_per_assieme: dict
                - totale_pezzo: float
                - totale_lotto: float
                - costo_piegatura: float
                - costo_saldatura: float
                - costo_filettatura: float
                - costo_svasatura: float
                - costo_montaggio_totale: float
                - costo_tubolari_totale: float
                - costo_piastre_totale: float
                - note: str (optional)
                - logo_path: str (optional)
                - azienda: dict (optional) with nome, indirizzo, telefono,
                  email, piva, sito
                - condizioni: str (optional)

        Returns:
            Path to generated PDF.
        """
        try:
            self._dati = dati
            self._page_count = 0

            doc = BaseDocTemplate(
                path,
                pagesize=A4,
                leftMargin=_MARGIN,
                rightMargin=_MARGIN,
                topMargin=40 * mm,
                bottomMargin=22 * mm,
                title=f"Preventivo {dati.get('numero_ordine', '')}",
                author=dati.get("azienda", {}).get("nome", ""),
            )

            frame = Frame(
                doc.leftMargin,
                doc.bottomMargin,
                doc.width,
                doc.height,
                id="main",
            )

            def _on_page(canvas, doc_ref):
                self._page_count += 1
                self._build_header(canvas, doc_ref, dati)
                self._build_footer(canvas, doc_ref, dati)

            template = PageTemplate(
                id="main",
                frames=[frame],
                onPage=_on_page,
            )
            doc.addPageTemplates([template])

            # Build flowable content
            elements = []
            elements.append(Spacer(1, 6 * mm))

            # Cover page: client box + total (+ margine/KPI/donut solo interno)
            self._build_cover_section(elements, dati, interno=interno)

            if interno:
                # ---- DISTINTA INTERNA (ufficio): costi scomposti + margine ----
                elements.append(PageBreak())

                articoli = dati.get("articoli", [])
                if articoli:
                    self._build_article_table(elements, dati)

                # Cost breakdown (include riga margine highlight se applicato)
                self._build_cost_breakdown(elements, dati)

                # Assembly section (con preview 3D se disponibili)
                costi_montaggio = dati.get("costi_montaggio", {})
                if costi_montaggio:
                    self._build_assembly_section(elements, dati)

                # Tubular section
                tubolari = dati.get("tubolari_per_assieme", {})
                if tubolari:
                    self._build_tubular_section(elements, dati)

                # Plate section
                piastre = dati.get("piastre_per_assieme", {})
                if piastre:
                    self._build_plate_section(elements, dati)

                # Totals box
                self._build_totals_box(elements, dati)
            else:
                # ---- PDF CLIENTE: elenco pezzi + prezzo finale per riga ----
                # Nessun costo scomposto, nessun margine: solo prezzi finali.
                elements.append(Spacer(1, 4 * mm))
                self._build_customer_lines(elements, dati)

            # Notes
            if dati.get("note"):
                self._build_notes(elements, dati)

            # Conditions
            if dati.get("condizioni"):
                elements.append(Spacer(1, 4 * mm))
                elements.append(
                    Paragraph("Condizioni", self.style_heading)
                )
                elements.append(
                    Paragraph(
                        dati["condizioni"].replace("\n", "<br/>"),
                        self.style_body,
                    )
                )

            # Signature/acceptance box (blocco coeso condizioni + accettazione)
            self._build_signature_box(elements, dati, interno=interno)

            doc.build(elements)
            logger.info("PDF generato: %s", path)
            return path

        except Exception:
            logger.exception("Errore generazione PDF")
            raise

    # ------------------------------------------------------------------
    # Header & Footer (drawn on canvas, not flowables)
    # ------------------------------------------------------------------

    def _build_header(self, canvas, doc, dati):
        """Draw header with company info/logo and quote details."""
        canvas.saveState()

        # Header band
        canvas.setFillColor(self.COLOR_PRIMARY)
        canvas.rect(0, _H - 32 * mm, _W, 32 * mm, fill=True, stroke=False)

        # Logo (if provided)
        logo_path = dati.get("logo_path")
        x_text_start = _MARGIN
        if logo_path and Path(logo_path).is_file():
            try:
                logo = Image(logo_path)
                # Scale logo to fit header
                aspect = logo.imageWidth / logo.imageHeight
                logo_h = 18 * mm
                logo_w = logo_h * aspect
                if logo_w > 40 * mm:
                    logo_w = 40 * mm
                    logo_h = logo_w / aspect
                # Riquadro bianco dietro al logo: lo stacca dalla banda verde
                pad = 2.5 * mm
                canvas.setFillColor(colors.white)
                canvas.roundRect(
                    _MARGIN - pad,
                    _H - 27 * mm - pad,
                    logo_w + 2 * pad,
                    logo_h + 2 * pad,
                    2 * mm,
                    stroke=0,
                    fill=1,
                )
                canvas.drawImage(
                    logo_path,
                    _MARGIN,
                    _H - 27 * mm,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                x_text_start = _MARGIN + logo_w + 4 * mm
            except Exception:
                logger.warning("Impossibile caricare logo: %s", logo_path)

        # Company name
        azienda = dati.get("azienda", {})
        nome_azienda = azienda.get("nome", "")
        if nome_azienda:
            canvas.setFillColor(colors.white)
            canvas.setFont("Helvetica-Bold", 16)
            canvas.drawString(x_text_start, _H - 15 * mm, nome_azienda)

            # Company details line
            details_parts = []
            if azienda.get("indirizzo"):
                details_parts.append(azienda["indirizzo"])
            if azienda.get("telefono"):
                details_parts.append(f"Tel: {azienda['telefono']}")
            if azienda.get("email"):
                details_parts.append(azienda["email"])
            if details_parts:
                canvas.setFont("Helvetica", 7.5)
                canvas.setFillColor(colors.Color(1, 1, 1, 0.8))
                canvas.drawString(
                    x_text_start, _H - 20 * mm, "  |  ".join(details_parts)
                )

            if azienda.get("piva"):
                canvas.setFont("Helvetica", 7)
                canvas.drawString(
                    x_text_start, _H - 24 * mm, f"P.IVA: {azienda['piva']}"
                )
        else:
            canvas.setFillColor(colors.white)
            canvas.setFont("Helvetica-Bold", 18)
            canvas.drawString(_MARGIN, _H - 17 * mm, "PREVENTIVO")

        # Right side: quote number and date
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 11)
        numero = dati.get("numero_ordine", "")
        if numero:
            canvas.drawRightString(_W - _MARGIN, _H - 13 * mm, f"N. {numero}")

        data_str = dati.get("data") or datetime.now().strftime("%d/%m/%Y")
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(_W - _MARGIN, _H - 18 * mm, f"Data: {data_str}")

        canvas.restoreState()

    def _build_footer(self, canvas, doc, dati):
        """Draw footer with page numbers and branding."""
        canvas.saveState()

        # Thin line
        canvas.setStrokeColor(self.COLOR_BORDER)
        canvas.setLineWidth(0.5)
        y_footer = 14 * mm
        canvas.line(_MARGIN, y_footer, _W - _MARGIN, y_footer)

        # Page number
        canvas.setFillColor(self.COLOR_MUTED)
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(
            _W - _MARGIN,
            y_footer - 4 * mm,
            f"Pagina {doc.page}",
        )

        # Branding: nome azienda (professionale), fallback neutro
        azienda = dati.get("azienda", {}) or {}
        footer_left = azienda.get("nome") or "Preventivo"
        if azienda.get("piva"):
            footer_left += f" · P.IVA {azienda['piva']}"
        canvas.drawString(_MARGIN, y_footer - 4 * mm, footer_left)

        # Company website if available
        sito = dati.get("azienda", {}).get("sito", "")
        if sito:
            canvas.drawCentredString(_W / 2, y_footer - 4 * mm, sito)

        canvas.restoreState()

    # ------------------------------------------------------------------
    # Client info box
    # ------------------------------------------------------------------

    def _build_client_box(self, elements, dati):
        """Build the client information box under the header."""
        cliente = dati.get("cliente", "")
        numero = dati.get("numero_ordine", "")
        data_str = dati.get("data") or datetime.now().strftime("%d/%m/%Y")
        quantita = dati.get("quantita", 1)

        info_data = []
        if cliente:
            info_data.append(["Cliente:", cliente])
        if numero:
            info_data.append(["Rif. Ordine:", numero])
        info_data.append(["Data:", data_str])
        info_data.append(["Quantita:", f"{quantita} pz"])

        if not info_data:
            return

        t = Table(info_data, colWidths=[30 * mm, 120 * mm])
        t.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("TEXTCOLOR", (0, 0), (0, -1), self.COLOR_MUTED),
                    ("TEXTCOLOR", (1, 0), (1, -1), self.COLOR_DARK),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Article table
    # ------------------------------------------------------------------

    def _build_article_table(self, elements, dati):
        """Build the article detail table."""
        elements.append(Paragraph("Dettaglio Articoli", self.style_heading))

        articoli = dati.get("articoli", [])
        fattore = self._fattore_prezzo(dati)

        # Table header
        header = ["Codice", "Materiale", "Piegatura", "Saldatura", "Altro", "Totale"]
        table_data = [header]

        totale_generale = 0.0
        for art in articoli:
            codice = art.get("codice", "")
            costo_piega = art.get("costo_piega", 0) or 0
            costo_sald = art.get("costo_saldatura", 0) or 0
            costo_altro = (
                (art.get("costo_filettatura", 0) or 0)
                + (art.get("costo_svasatura", 0) or 0)
                + (art.get("costo_apporto", art.get("costo_mat_apporto", 0)) or 0)
                + (art.get("costo_pulizia", 0) or 0)
            )
            # `costo` e' il costo unitario COMPLETO (base + lavorazioni): la
            # colonna Materiale deve mostrare solo la base, altrimenti le
            # lavorazioni si contavano due volte.
            if "costo_base" in art:
                costo_mat = art.get("costo_base") or 0
            else:
                costo_mat = (art.get("costo", 0) or 0) - costo_piega - costo_sald - costo_altro
            costo_tot = (costo_mat + costo_piega + costo_sald + costo_altro) * fattore
            totale_generale += costo_tot

            table_data.append(
                [
                    codice,
                    _eur_plain(costo_mat),
                    _eur_plain(costo_piega),
                    _eur_plain(costo_sald),
                    _eur_plain(costo_altro),
                    _eur_plain(costo_tot),
                ]
            )

        # Total row
        table_data.append(
            ["TOTALE", "", "", "", "", _eur_plain(totale_generale)]
        )

        avail_width = _W - 2 * _MARGIN
        col_widths = [
            avail_width * 0.28,
            avail_width * 0.14,
            avail_width * 0.14,
            avail_width * 0.14,
            avail_width * 0.14,
            avail_width * 0.16,
        ]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)

        # Build style commands
        style_cmds = [
            # Header row
            ("BACKGROUND", (0, 0), (-1, 0), self.COLOR_PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("FONTNAME", (0, 1), (-1, -2), "Helvetica"),
            # Total row
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), self.COLOR_PRIMARY_LIGHT),
            ("LINEABOVE", (0, -1), (-1, -1), 1, self.COLOR_PRIMARY),
            # Alignment
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # Padding
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            # Grid
            ("LINEBELOW", (0, 0), (-1, 0), 1, self.COLOR_PRIMARY),
            ("LINEBELOW", (0, -1), (-1, -1), 1, self.COLOR_PRIMARY),
            ("LINEBEFORE", (0, 0), (0, -1), 0.5, self.COLOR_BORDER),
            ("LINEAFTER", (-1, 0), (-1, -1), 0.5, self.COLOR_BORDER),
        ]

        # Alternating row colours
        for i in range(1, len(table_data) - 1):
            if i % 2 == 0:
                style_cmds.append(
                    ("BACKGROUND", (0, i), (-1, i), self.COLOR_ROW_ALT)
                )

        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Cost breakdown with visual bars
    # ------------------------------------------------------------------

    def _build_cost_breakdown(self, elements, dati):
        """Build cost breakdown section with visual bar chart."""
        elements.append(
            Paragraph("Ripartizione Costi", self.style_heading)
        )

        cost_items = [
            ("Piegatura", dati.get("costo_piegatura", 0)),
            ("Saldatura", dati.get("costo_saldatura", 0)),
            ("Filettatura", dati.get("costo_filettatura", 0)),
            ("Svasatura", dati.get("costo_svasatura", 0)),
        ]

        # Add optional cost items
        if dati.get("costo_montaggio_totale", 0) > 0:
            cost_items.append(
                ("Montaggio assiemi", dati["costo_montaggio_totale"])
            )
        if dati.get("costo_tubolari_totale", 0) > 0:
            cost_items.append(("Tubolari", dati["costo_tubolari_totale"]))
        if dati.get("costo_piastre_totale", 0) > 0:
            cost_items.append(("Piastre", dati["costo_piastre_totale"]))

        # Filter out zero-cost items for the bars
        cost_items_nonzero = [(n, v) for n, v in cost_items if v > 0]
        if not cost_items_nonzero:
            elements.append(
                Paragraph(
                    "Nessun costo di lavorazione rilevato.", self.style_body
                )
            )
            elements.append(Spacer(1, 4 * mm))
            return

        max_val = max(v for _, v in cost_items_nonzero)

        # Build a table with: Name | Bar | Value
        avail = _W - 2 * _MARGIN
        bar_max_w = avail * 0.45  # max bar width in points

        table_data = []
        bar_colors = [
            self.COLOR_PRIMARY,
            self.COLOR_SUCCESS,
            self.COLOR_WARNING,
            colors.Color(99 / 255, 102 / 255, 241 / 255),  # indigo-400
            colors.Color(168 / 255, 85 / 255, 247 / 255),  # purple
            colors.Color(14 / 255, 165 / 255, 233 / 255),  # sky
            colors.Color(244 / 255, 63 / 255, 94 / 255),  # rose
        ]

        for idx, (name, value) in enumerate(cost_items_nonzero):
            bar_w = (value / max_val) * bar_max_w if max_val > 0 else 0
            bar_color = bar_colors[idx % len(bar_colors)]

            # Create a tiny table as the bar
            bar_cell_content = ""
            bar_table = Table(
                [[bar_cell_content]],
                colWidths=[max(bar_w, 2)],
                rowHeights=[12],
            )
            bar_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, 0), bar_color),
                        ("TOPPADDING", (0, 0), (0, 0), 0),
                        ("BOTTOMPADDING", (0, 0), (0, 0), 0),
                        ("LEFTPADDING", (0, 0), (0, 0), 0),
                        ("RIGHTPADDING", (0, 0), (0, 0), 0),
                    ]
                )
            )

            table_data.append([name, bar_table, _eur(value)])

        # Riga margine highlight (se applicato)
        margine = dati.get("margine", 0)
        margine_row_idx = None
        if margine > 0:
            base_total = sum(v for _, v in cost_items_nonzero)
            margine_val = base_total * (margine / 100)
            bar_w = min((margine_val / max_val) * bar_max_w, bar_max_w) if max_val > 0 else 0
            bar_table = Table(
                [[""]],
                colWidths=[max(bar_w, 2)],
                rowHeights=[12],
            )
            bar_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, 0), self.COLOR_WARNING),
                        ("TOPPADDING", (0, 0), (0, 0), 0),
                        ("BOTTOMPADDING", (0, 0), (0, 0), 0),
                        ("LEFTPADDING", (0, 0), (0, 0), 0),
                        ("RIGHTPADDING", (0, 0), (0, 0), 0),
                    ]
                )
            )
            table_data.append([
                f"Margine +{margine:.1f}%",
                bar_table,
                _eur(margine_val),
            ])
            margine_row_idx = len(table_data) - 1

        col_widths = [avail * 0.25, avail * 0.50, avail * 0.25]
        t = Table(table_data, colWidths=col_widths)
        style_cmds = [
            ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("TEXTCOLOR", (0, 0), (0, -1), self.COLOR_DARK),
            ("TEXTCOLOR", (2, 0), (2, -1), self.COLOR_DARK),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if margine_row_idx is not None:
            # Evidenzia la riga margine con background warning-soft
            warning_soft = colors.Color(254 / 255, 243 / 255, 199 / 255)
            style_cmds.extend([
                ("BACKGROUND", (0, margine_row_idx), (-1, margine_row_idx), warning_soft),
                ("FONTNAME", (0, margine_row_idx), (0, margine_row_idx), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, margine_row_idx), (0, margine_row_idx), self.COLOR_WARNING),
                ("TEXTCOLOR", (2, margine_row_idx), (2, margine_row_idx), self.COLOR_WARNING),
                ("LINEBEFORE", (0, margine_row_idx), (0, margine_row_idx), 2, self.COLOR_WARNING),
                ("TOPPADDING", (0, margine_row_idx), (-1, margine_row_idx), 7),
                ("BOTTOMPADDING", (0, margine_row_idx), (-1, margine_row_idx), 7),
            ])
        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Assembly section
    # ------------------------------------------------------------------

    def _build_assembly_previews_grid(self, elements, costi_montaggio, previews):
        """Build a grid of 3D preview thumbnails for assemblies."""
        codici = list(costi_montaggio.keys())
        thumbs = []
        for codice in codici:
            path = previews.get(codice)
            if not path or not Path(path).is_file():
                continue
            try:
                img = Image(path, width=50 * mm, height=37 * mm, kind="proportional")
                label = Paragraph(
                    f'<font size=7 name="Helvetica-Bold">{codice}</font>',
                    self.style_small,
                )
                thumbs.append([img, label])
            except Exception as exc:
                logger.warning("Impossibile caricare preview '%s': %s", codice, exc)

        if not thumbs:
            return

        # Layout griglia 3 colonne
        cols_per_row = 3
        rows_img = []
        rows_lbl = []
        for i in range(0, len(thumbs), cols_per_row):
            batch = thumbs[i:i + cols_per_row]
            img_row = [t[0] for t in batch]
            lbl_row = [t[1] for t in batch]
            while len(img_row) < cols_per_row:
                img_row.append("")
                lbl_row.append("")
            rows_img.append(img_row)
            rows_lbl.append(lbl_row)

        avail = _W - 2 * _MARGIN
        col_w = avail / cols_per_row

        # Tabella a 2 righe per ciascun batch (img + label)
        combined_rows = []
        for ir, lr in zip(rows_img, rows_lbl):
            combined_rows.append(ir)
            combined_rows.append(lr)

        t = Table(combined_rows, colWidths=[col_w] * cols_per_row)
        style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        # Applica sfondo alle righe immagine (pari)
        for ri in range(0, len(combined_rows), 2):
            style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), self.COLOR_ROW_ALT))
            style_cmds.append(("BOX", (0, ri), (-1, ri + 1), 0.3, self.COLOR_BORDER))
        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 4 * mm))

    def _build_assembly_section(self, elements, dati):
        """Build assembly details section if assemblies exist."""
        costi_montaggio = dati.get("costi_montaggio", {})
        if not costi_montaggio:
            return

        elements.append(
            Paragraph("Lavorazione Assiemi", self.style_heading)
        )

        # Thumbnails 3D (se disponibili dal wizard)
        previews = dati.get("assembly_previews", {}) or {}
        if previews:
            self._build_assembly_previews_grid(elements, costi_montaggio, previews)

        header = ["Assieme", "Ore", "Montaggio", "Puntatura", "Saldatura", "Totale"]
        table_data = [header]

        totale_assiemi = 0.0
        for codice_ass, mont_data in costi_montaggio.items():
            qty = mont_data.get("qty", 1)
            ore = mont_data.get("ore", 0)
            costo_mont = mont_data.get("costo", 0) * qty
            costo_punt = mont_data.get("costo_puntatura", 0) * qty
            costo_sald = mont_data.get("costo_saldatura_assieme", 0) * qty
            costo_tot = costo_mont + costo_punt + costo_sald
            totale_assiemi += costo_tot

            label = codice_ass
            if qty > 1:
                label = f"{codice_ass} (x{qty})"

            table_data.append(
                [
                    label,
                    f"{ore:.1f}",
                    _eur_plain(costo_mont),
                    _eur_plain(costo_punt),
                    _eur_plain(costo_sald),
                    _eur_plain(costo_tot),
                ]
            )

        table_data.append(
            ["TOTALE ASSIEMI", "", "", "", "", _eur_plain(totale_assiemi)]
        )

        avail = _W - 2 * _MARGIN
        col_widths = [
            avail * 0.30,
            avail * 0.10,
            avail * 0.15,
            avail * 0.15,
            avail * 0.15,
            avail * 0.15,
        ]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), self.COLOR_SUCCESS),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("FONTNAME", (0, 1), (-1, -2), "Helvetica"),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.Color(209/255, 250/255, 229/255)),
                    ("LINEABOVE", (0, -1), (-1, -1), 1, self.COLOR_SUCCESS),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, 0), (-1, 0), 1, self.COLOR_SUCCESS),
                ]
            )
        )
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

        # -- BOM per assieme -------------------------------------------------
        # Per ogni assieme che ha almeno un componente linkato, stampa la sua
        # BOM: articoli DXF figli con qty/pezzo, materiale, spessore, peso,
        # costo base + lavorazioni per pezzo e contributo su 1 assieme.
        # Chiude ogni sezione col PREZZO 1 ASSIEME (intrinseco + Σ contributi).
        self._build_assiemi_bom(elements, costi_montaggio)

    def _build_assiemi_bom(self, elements, costi_montaggio: dict) -> None:
        """Sezione BOM per ciascun assieme (composizione + breakdown costi).

        Il prezzo mostrato è sempre per UN singolo assieme (unitario). La qty
        assieme moltiplica al livello preventivo, non nella BOM.
        """
        has_any_bom = any(
            (m.get('bom_articoli') or m.get('bom_tubolari') or m.get('bom_piastre'))
            for m in costi_montaggio.values()
        )
        if not has_any_bom:
            return

        elements.append(Spacer(1, 2 * mm))
        elements.append(
            Paragraph("BOM · composizione assiemi", self.style_heading)
        )
        elements.append(
            Paragraph(
                "Prezzo unitario per <b>1 assieme</b>. Le quantità sono la composizione "
                "BOM (numero di pezzi in ciascun assieme).",
                self.style_body,
            )
        )
        elements.append(Spacer(1, 2 * mm))

        for codice_ass, mont_data in costi_montaggio.items():
            bom_art = mont_data.get('bom_articoli') or []
            bom_tub = mont_data.get('bom_tubolari') or []
            bom_pia = mont_data.get('bom_piastre') or []
            if not (bom_art or bom_tub or bom_pia):
                continue

            # Titoletto assieme
            elements.append(
                Paragraph(
                    f'<font name="Helvetica-Bold" size=10 color="#3730A3">'
                    f'Assieme {codice_ass}</font>',
                    self.style_body,
                )
            )
            elements.append(Spacer(1, 1 * mm))

            header = ["#", "Tipo", "Codice / descrizione", "Pz/ass", "Materiale",
                      "Sp.", "Peso kg", "Mat.+base €", "Lav. €", "Tot./pz €", "Contrib. €"]
            table_data = [header]
            idx = 1
            contrib_tot = 0.0

            for a in bom_art:
                contrib = float(a.get('contributo_su_1_ass') or 0)
                contrib_tot += contrib
                sp_str = f"{a['spessore_mm']:.1f}" if a.get('spessore_mm') else "—"
                table_data.append([
                    str(idx),
                    "DXF",
                    a.get('codice') or "—",
                    str(a.get('qty_per_ass') or 1),
                    a.get('materiale') or "—",
                    sp_str,
                    f"{a.get('peso_kg_pz', 0):.2f}",
                    _eur_plain(a.get('costo_base_pz') or 0),
                    _eur_plain(a.get('costo_lav_pz') or 0),
                    _eur_plain(a.get('costo_tot_pz') or 0),
                    _eur_plain(contrib),
                ])
                idx += 1

            for t in bom_tub:
                mat_c = float(t.get('costo_materiale') or 0)
                lav_c = float(t.get('costo_taglio_totale') or 0)
                totp = mat_c + lav_c
                contrib_tot += totp
                lung = f"{float(t.get('lunghezza_m') or 0):.2f}m"
                table_data.append([
                    str(idx),
                    "TUB",
                    f"{t.get('profilo') or '—'} ({lung})",
                    "1",
                    (t.get('materiale') or "").upper() or "—",
                    "—",
                    f"{float(t.get('peso_kg') or 0):.2f}",
                    _eur_plain(mat_c),
                    _eur_plain(lav_c),
                    _eur_plain(totp),
                    _eur_plain(totp),
                ])
                idx += 1

            for pl in bom_pia:
                cost = float(pl.get('costo') or 0)
                contrib_tot += cost
                sp_p = f"{float(pl.get('spessore_mm') or 0):.1f}"
                area_p = f"{float(pl.get('area_dm2') or 0):.2f}dm²"
                table_data.append([
                    str(idx),
                    "PIA",
                    f"{area_p} ({(pl.get('materiale') or '').upper()})",
                    "1",
                    (pl.get('materiale') or "").upper() or "—",
                    sp_p,
                    f"{float(pl.get('peso_kg') or 0):.2f}",
                    _eur_plain(cost),
                    "—",
                    _eur_plain(cost),
                    _eur_plain(cost),
                ])
                idx += 1

            # Riga intrinseco (montaggio + saldatura assieme) — non fa parte
            # della BOM di componenti, ma va sommato per il prezzo finale
            intrinseco = (
                float(mont_data.get('costo') or 0)
                + float(mont_data.get('costo_puntatura') or 0)
                + float(mont_data.get('costo_saldatura_assieme') or 0)
            )
            table_data.append([
                "—",
                "ASS",
                "Montaggio + puntatura + saldatura assieme",
                "—", "—", "—", "—", "—", "—", "—",
                _eur_plain(intrinseco),
            ])

            prezzo_1_ass = contrib_tot + intrinseco
            table_data.append([
                "", "", "PREZZO 1 ASSIEME (unitario)", "", "", "", "", "", "", "",
                _eur_plain(prezzo_1_ass),
            ])

            avail = _W - 2 * _MARGIN
            col_widths = [
                avail * 0.04,   # #
                avail * 0.05,   # Tipo
                avail * 0.28,   # Codice
                avail * 0.05,   # Pz/ass
                avail * 0.09,   # Materiale
                avail * 0.05,   # Sp.
                avail * 0.06,   # Peso
                avail * 0.09,   # Mat+base
                avail * 0.07,   # Lav
                avail * 0.10,   # Tot/pz
                avail * 0.12,   # Contrib
            ]
            tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
            tbl.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(238/255, 242/255, 255/255)),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.Color(55/255, 48/255, 163/255)),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("FONTNAME", (0, 1), (-1, -3), "Helvetica"),
                    # Riga intrinseco (penultima): stile stessa gerarchia
                    ("BACKGROUND", (0, -2), (-1, -2), colors.Color(249/255, 250/255, 251/255)),
                    ("FONTNAME", (0, -2), (-1, -2), "Helvetica-Oblique"),
                    # Riga prezzo finale (ultima)
                    ("BACKGROUND", (0, -1), (-1, -1), colors.Color(220/255, 252/255, 231/255)),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("TEXTCOLOR", (0, -1), (-1, -1), colors.Color(22/255, 101/255, 52/255)),
                    ("FONTSIZE", (0, -1), (-1, -1), 8),
                    ("LINEABOVE", (0, -1), (-1, -1), 1, colors.Color(22/255, 101/255, 52/255)),
                    ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (2, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.Color(226/255, 232/255, 240/255)),
                ])
            )
            elements.append(tbl)
            elements.append(Spacer(1, 5 * mm))

    # ------------------------------------------------------------------
    # Tubular section
    # ------------------------------------------------------------------

    def _group_tubolari(self, tubolari_per_assieme: dict) -> tuple:
        """Raggruppa tubolari per profilo + lunghezza (1mm) + taglio.

        Produce distinta di taglio officina-ready cumulando tutti gli assiemi.

        Returns:
            tuple (profiles_list, grand_totals):
                profiles_list: [{profilo, groups: [{idx, len_m, taglio, qty,
                                peso_tot, costo_tot}], total_qty,
                                total_len, total_peso, total_cost}]
                grand_totals: {qty, len, peso, cost}
        """
        by_profilo: dict = {}
        for codice_ass, tub_data in tubolari_per_assieme.items():
            analisi = tub_data.get("analisi", {})
            costi_tub = tub_data.get("costi", {})
            dettaglio = costi_tub.get("dettaglio_tubi", [])
            # Map costi per indice tubo se disponibile
            tubi = analisi.get("tubi", [])
            for i, tubo in enumerate(tubi):
                profilo = tubo.get("profilo", "?")
                lun_m = tubo.get("lunghezza_m", 0)
                lun_mm = round(lun_m * 1000)
                t1 = str(tubo.get("taglio_1", "dritto")).lower()
                t2 = str(tubo.get("taglio_2", "dritto")).lower()
                taglio = t1 if t1 == t2 else f"{t1}/{t2}"
                key = (lun_mm, taglio)

                # Costo singolo tubo (se disponibile dal dettaglio)
                costo_mat = tubo.get("costo_materiale", 0)
                costo_taglio = tubo.get("costo_taglio", 0)
                if i < len(dettaglio):
                    d = dettaglio[i]
                    costo_mat = d.get("costo_materiale", costo_mat)
                    costo_taglio = d.get("costo_taglio", costo_taglio)

                inner = by_profilo.setdefault(profilo, {})
                g = inner.setdefault(key, {
                    "len_m": lun_mm / 1000.0,
                    "taglio": taglio,
                    "qty": 0,
                    "peso_tot": 0.0,
                    "costo_tot": 0.0,
                })
                g["qty"] += 1
                g["peso_tot"] += tubo.get("peso_kg", 0)
                g["costo_tot"] += (costo_mat + costo_taglio)

        profiles_list = []
        running_idx = 1
        for profilo, inner in by_profilo.items():
            groups = sorted(inner.values(), key=lambda g: -g["len_m"])
            for g in groups:
                g["idx"] = running_idx
                running_idx += 1
            total_qty = sum(g["qty"] for g in groups)
            total_len = sum(g["len_m"] * g["qty"] for g in groups)
            total_peso = sum(g["peso_tot"] for g in groups)
            total_cost = sum(g["costo_tot"] for g in groups)
            profiles_list.append({
                "profilo": profilo,
                "groups": groups,
                "total_qty": total_qty,
                "total_len": total_len,
                "total_peso": total_peso,
                "total_cost": total_cost,
            })

        grand = {
            "qty":  sum(p["total_qty"] for p in profiles_list),
            "len":  sum(p["total_len"] for p in profiles_list),
            "peso": sum(p["total_peso"] for p in profiles_list),
            "cost": sum(p["total_cost"] for p in profiles_list),
        }
        return profiles_list, grand

    def _build_tubular_section(self, elements, dati):
        """Build tubolari section as officina-ready 'distinta di taglio'.

        Raggruppa tubolari per profilo + lunghezza + taglio, cumulando tutti
        gli assiemi per una distinta di taglio unica.
        """
        tubolari = dati.get("tubolari_per_assieme", {})
        if not tubolari:
            return

        profiles_list, grand = self._group_tubolari(tubolari)
        if not profiles_list:
            return

        elements.append(
            Paragraph(
                f"Distinta di Taglio Tubolari &middot; {grand['qty']} pezzi "
                f"su {len(profiles_list)} "
                f"{'profilo' if len(profiles_list) == 1 else 'profili'}",
                self.style_heading,
            )
        )

        avail = _W - 2 * _MARGIN

        # Colonne: # | Lunghezza | Qty | Taglio | Tot. m | Tot. kg | Costo
        col_widths = [
            avail * 0.08,   # idx
            avail * 0.18,   # lunghezza
            avail * 0.10,   # qty
            avail * 0.18,   # taglio
            avail * 0.15,   # tot m
            avail * 0.15,   # tot kg
            avail * 0.16,   # costo
        ]

        steel_dark = colors.Color(71 / 255, 85 / 255, 105 / 255)
        primary_soft = colors.Color(238 / 255, 240 / 255, 254 / 255)

        for p in profiles_list:
            # Intestazione profilo
            header_row = Table(
                [[
                    Paragraph(
                        f'<font name="Helvetica-Bold" size=10>{p["profilo"]}</font>',
                        self.style_body_bold,
                    ),
                    Paragraph(
                        f'<font size=8 color="#475569"><b>{p["total_qty"]} pz</b> &middot; '
                        f'{p["total_len"]:.3f} m &middot; {p["total_peso"]:.2f} kg '
                        f'&middot; <font color="#4F46E5"><b>{_eur(p["total_cost"])}</b></font></font>',
                        self.style_small,
                    ),
                ]],
                colWidths=[avail * 0.45, avail * 0.55],
            )
            header_row.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), self.COLOR_ROW_ALT),
                ("BOX", (0, 0), (-1, -1), 0.5, self.COLOR_BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]))

            # Tabella dei tagli
            table_data = [[
                "#", "Lunghezza", "Qty", "Taglio", "Tot. m", "Tot. kg", "Costo",
            ]]
            for g in p["groups"]:
                table_data.append([
                    f'#{g["idx"]}',
                    f'{g["len_m"]:.3f} m',
                    f'\u00D7 {g["qty"]}',
                    g["taglio"],
                    f'{g["len_m"] * g["qty"]:.3f}',
                    f'{g["peso_tot"]:.2f}',
                    _eur_plain(g["costo_tot"]),
                ])

            cut_table = Table(table_data, colWidths=col_widths, repeatRows=1)
            style_cmds = [
                # Header
                ("BACKGROUND", (0, 0), (-1, 0), steel_dark),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 7.5),
                # Body
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                # Column specific
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 1), (0, -1), self.COLOR_PRIMARY),
                ("FONTNAME", (2, 1), (2, -1), "Helvetica-Bold"),
                ("BACKGROUND", (2, 1), (2, -1), primary_soft),
                ("TEXTCOLOR", (2, 1), (2, -1), self.COLOR_PRIMARY),
                # Alignment
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                # Padding
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, steel_dark),
                ("BOX", (0, 0), (-1, -1), 0.3, self.COLOR_BORDER),
            ]
            # Alternating rows per body
            for ri in range(1, len(table_data)):
                if ri % 2 == 0:
                    style_cmds.append(
                        ("BACKGROUND", (0, ri), (1, ri), self.COLOR_ROW_ALT)
                    )
                    style_cmds.append(
                        ("BACKGROUND", (3, ri), (-1, ri), self.COLOR_ROW_ALT)
                    )
            cut_table.setStyle(TableStyle(style_cmds))

            elements.append(KeepTogether([header_row, cut_table]))
            elements.append(Spacer(1, 4 * mm))

        # Grand total banner
        grand_banner = Table(
            [[
                Paragraph(
                    '<font color="#FFFFFF" size=10 name="Helvetica-Bold">'
                    'Totale distinta tubolari</font>',
                    self.style_body_bold,
                ),
                Paragraph(
                    f'<font color="#FFFFFF" size=10 name="Helvetica-Bold">'
                    f'{grand["qty"]} pz &middot; {grand["len"]:.3f} m &middot; '
                    f'{grand["peso"]:.2f} kg &middot; {_eur(grand["cost"])}'
                    '</font>',
                    self.style_body_bold,
                ),
            ]],
            colWidths=[avail * 0.40, avail * 0.60],
        )
        grand_banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.COLOR_PRIMARY),
            ("BOX", (0, 0), (-1, -1), 0, self.COLOR_PRIMARY),
            ("ROUNDEDCORNERS", [6, 6, 6, 6]),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 16),
            ("RIGHTPADDING", (0, 0), (-1, -1), 16),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(grand_banner)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Plate section
    # ------------------------------------------------------------------

    def _build_plate_section(self, elements, dati):
        """Build plate section if present."""
        piastre = dati.get("piastre_per_assieme", {})
        if not piastre:
            return

        elements.append(Paragraph("Piastre", self.style_heading))

        header = ["Assieme", "Spessore", "Area", "Peso", "Costo"]
        table_data = [header]

        for codice_ass, pia_data in piastre.items():
            analisi = pia_data.get("analisi", {})
            costi_pia = pia_data.get("costi", {})
            dettaglio = costi_pia.get("dettaglio_piastre", [])

            for i, piastra in enumerate(analisi.get("piastre", [])):
                sp = piastra.get("spessore_mm", 0)
                area = piastra.get("area_dm2", 0)
                peso = piastra.get("peso_kg", 0)
                costo_p = dettaglio[i].get("costo", 0) if i < len(dettaglio) else 0
                table_data.append(
                    [
                        codice_ass,
                        f"{sp:.1f} mm",
                        f"{area:.2f} dm\u00B2",
                        f"{peso:.1f} kg",
                        _eur_plain(costo_p),
                    ]
                )

            totale_pia = costi_pia.get("totale", 0)
            table_data.append(
                [codice_ass, "Totale", "", "", _eur_plain(totale_pia)]
            )

        avail = _W - 2 * _MARGIN
        col_widths = [
            avail * 0.25,
            avail * 0.20,
            avail * 0.20,
            avail * 0.15,
            avail * 0.20,
        ]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(71/255, 85/255, 105/255)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.Color(71/255, 85/255, 105/255)),
        ]
        for i in range(1, len(table_data)):
            if i % 2 == 0:
                style_cmds.append(
                    ("BACKGROUND", (0, i), (-1, i), self.COLOR_ROW_ALT)
                )
        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Totals box
    # ------------------------------------------------------------------

    @staticmethod
    def _fattore_prezzo(dati):
        """Fattore costo → prezzo: (1+generali%) × (1+ricarico%). Lo calcola il
        backend (`fattore_prezzo`); per dati vecchi si ricostruisce."""
        f = dati.get("fattore_prezzo")
        if f:
            return float(f)
        gen = float(dati.get("costi_generali_pct") or 0)
        mar = float(dati.get("margine") or 0)
        return (1 + gen / 100) * (1 + mar / 100)

    def _build_totals_box(self, elements, dati):
        """Build the final totals box - highlighted and prominent."""
        elements.append(Spacer(1, 4 * mm))

        quantita = dati.get("quantita", 1)
        totale_pezzo = dati.get("totale_pezzo", 0)
        margine = dati.get("margine", 0)
        totale_lotto = dati.get("totale_lotto", 0)
        costo_montaggio = dati.get("costo_montaggio_totale", 0)
        costo_tubolari = dati.get("costo_tubolari_totale", 0)
        costo_piastre = dati.get("costo_piastre_totale", 0)

        # Prezzo = costo × (1+generali%) × (1+ricarico%), per TUTTE le righe:
        # prima il prezzo unitario escludeva i costi generali mentre il TOTALE
        # li includeva, e le righe non sommavano al totale.
        fattore = self._fattore_prezzo(dati)
        prezzo_unitario = totale_pezzo * fattore

        rows = []

        # Unit price row
        rows.append(
            [
                f"Prezzo unitario ({quantita} pz)",
                f"{quantita} x {_eur(prezzo_unitario)}",
                _eur(prezzo_unitario * quantita),
            ]
        )

        if costo_montaggio > 0:
            rows.append(
                ["+ Montaggio assiemi", "", _eur(costo_montaggio * fattore)]
            )

        if costo_tubolari > 0:
            rows.append(["+ Tubolari", "", _eur(costo_tubolari * fattore)])

        if costo_piastre > 0:
            rows.append(["+ Piastre", "", _eur(costo_piastre * fattore)])

        sconto_eur = dati.get("sconto_eur") or 0
        if sconto_eur:
            rows.append([f"Sconto {dati.get('sconto_pct', 0):g}%", "", _eur(sconto_eur)])

        # Separator row (visual)
        rows.append(["", "", ""])

        # Grand total
        rows.append(["TOTALE ORDINE", "", _eur(totale_lotto)])

        avail = _W - 2 * _MARGIN
        col_widths = [avail * 0.40, avail * 0.30, avail * 0.30]
        t = Table(rows, colWidths=col_widths)

        n_rows = len(rows)
        style_cmds = [
            # Whole box background
            ("BACKGROUND", (0, 0), (-1, -2), self.COLOR_LIGHT),
            # Total row
            ("BACKGROUND", (0, -1), (-1, -1), self.COLOR_PRIMARY),
            ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, -1), (-1, -1), 13),
            # Other rows
            ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -2), 9),
            ("TEXTCOLOR", (0, 0), (-1, -2), self.COLOR_DARK),
            # Separator row (make it tiny)
            ("FONTSIZE", (0, -2), (-1, -2), 2),
            ("LINEBELOW", (0, -2), (-1, -2), 1, self.COLOR_BORDER),
            # Alignment
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # Padding
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            # Box border
            ("BOX", (0, 0), (-1, -1), 1.5, self.COLOR_PRIMARY),
            ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ]

        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------
    # Customer line items (PDF cliente — prezzi finali, nessun costo)
    # ------------------------------------------------------------------

    def _build_customer_lines(self, elements, dati):
        """Elenco pezzi per il CLIENTE: prezzo finale per riga + totale.

        Le righe sono precalcolate in `righe_cliente` (dal backend, con margine
        e generali gia' incorporati) e sommano al `totale_lotto`. Qui non si
        vede alcun costo scomposto ne' la percentuale di ricarico.
        """
        righe = dati.get("righe_cliente") or []
        totale_lotto = dati.get("totale_lotto", 0)

        elements.append(Paragraph("Dettaglio fornitura", self.style_heading))

        header = ["Codice", "Descrizione", "Q.ta", "Prezzo unit.", "Importo"]
        table_data = [header]

        if not righe:
            # Fallback: nessuna riga precalcolata → mostra solo il totale
            table_data.append(["Fornitura come da specifica", "", "", "", ""])

        for r in righe:
            qty = r.get("quantita", 1)
            try:
                qty_str = str(int(qty)) if float(qty) == int(qty) else f"{qty}"
            except (TypeError, ValueError):
                qty_str = str(qty)
            table_data.append([
                r.get("codice") or "-",
                r.get("descrizione") or "",
                qty_str,
                _eur_plain(r.get("prezzo_unitario") or 0),
                _eur_plain(r.get("importo") or 0),
            ])

        # Riga totale
        table_data.append(["", "", "", "TOTALE ORDINE", _eur(totale_lotto)])

        avail = _W - 2 * _MARGIN
        col_widths = [
            avail * 0.22,   # codice
            avail * 0.36,   # descrizione
            avail * 0.10,   # q.ta
            avail * 0.16,   # prezzo unit
            avail * 0.16,   # importo
        ]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        style_cmds = [
            # Header
            ("BACKGROUND", (0, 0), (-1, 0), self.COLOR_PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            # Body
            ("FONTNAME", (0, 1), (-1, -2), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("TEXTCOLOR", (0, 1), (-1, -2), self.COLOR_DARK),
            # Total row
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), self.COLOR_PRIMARY_LIGHT),
            ("TEXTCOLOR", (0, -1), (-1, -1), self.COLOR_DARK),
            ("FONTSIZE", (3, -1), (-1, -1), 11),
            ("LINEABOVE", (0, -1), (-1, -1), 1, self.COLOR_PRIMARY),
            ("SPAN", (0, -1), (2, -1)),
            # Alignment
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # Padding
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            # Grid
            ("LINEBELOW", (0, 0), (-1, 0), 1, self.COLOR_PRIMARY),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, self.COLOR_BORDER),
        ]
        # Righe alternate
        for i in range(1, len(table_data) - 1):
            if i % 2 == 0:
                style_cmds.append(
                    ("BACKGROUND", (0, i), (-1, i), self.COLOR_ROW_ALT)
                )
        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        # La nota prezzi ora apre il blocco 'Accettazione' (vedi _build_signature_box),
        # così non resta orfana in cima a una pagina successiva.

    # ------------------------------------------------------------------
    # Cover section (first page summary)
    # ------------------------------------------------------------------

    def _build_cover_section(self, elements, dati, interno=True):
        """Build the cover page summary: client, grand total, composition donut.

        In modalità cliente (interno=False) NON mostra la riga margine, la riga
        KPI e la composizione costi: rivelerebbero costi/ricarico.
        """
        cliente = dati.get("cliente", "")
        numero = dati.get("numero_ordine", "")
        data_str = dati.get("data") or datetime.now().strftime("%d/%m/%Y")
        quantita = dati.get("quantita", 1)
        margine = dati.get("margine", 0)
        totale_lotto = dati.get("totale_lotto", 0)
        totale_pezzo = dati.get("totale_pezzo", 0)

        avail = _W - 2 * _MARGIN

        # --- Cliente/ordine info box ---
        info_rows = []
        if cliente:
            info_rows.append([
                Paragraph("<b>Cliente</b>", self.style_small),
                Paragraph(f"<font size=11><b>{cliente}</b></font>", self.style_body_bold),
            ])
        if numero:
            info_rows.append([
                Paragraph("<b>Rif. Ordine</b>", self.style_small),
                Paragraph(f"<font size=11><b>N. {numero}</b></font>", self.style_body_bold),
            ])
        info_rows.append([
            Paragraph("<b>Data</b>", self.style_small),
            Paragraph(f"<font size=10>{data_str}</font>", self.style_body),
        ])
        # Consegna prevista (in caso di conferma): informazione utile al cliente
        data_consegna = dati.get("data_consegna")
        if data_consegna:
            info_rows.append([
                Paragraph("<b>Consegna prevista</b>", self.style_small),
                Paragraph(f"<font size=10><b>{data_consegna}</b></font>", self.style_body_bold),
            ])
        # Quantità: solo nell'interno. Al cliente è ridondante (le quantità dei
        # pezzi sono già nel "Dettaglio fornitura") e confonde quando è 1.
        if interno:
            info_rows.append([
                Paragraph("<b>Quantita'</b>", self.style_small),
                Paragraph(f"<font size=10>{quantita} pz</font>", self.style_body),
            ])
        if margine > 0 and interno:
            info_rows.append([
                Paragraph("<b>Margine applicato</b>", self.style_small),
                Paragraph(
                    f'<font size=10 color="#D97706"><b>+{margine:.1f}%</b></font>',
                    self.style_body_bold,
                ),
            ])

        info_table = Table(info_rows, colWidths=[35 * mm, avail * 0.55 - 35 * mm])
        info_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.3, self.COLOR_BORDER),
                ]
            )
        )

        # --- Grand total block (right side) ---
        # Stile dedicato per il numero grande: interlinea adeguata al font 22pt,
        # altrimenti sfora e si accavalla sulla riga "Prezzo unitario".
        style_total_big = ParagraphStyle(
            "TotalBig", parent=self.style_body_bold,
            fontName="Helvetica-Bold", fontSize=22, leading=27,
            textColor=colors.white,
        )
        total_block_rows = [
            [Paragraph(
                '<font size=8 color="#FFFFFF"><b>TOTALE ORDINE</b></font>',
                self.style_small,
            )],
            [Paragraph(_eur(totale_lotto), style_total_big)],
        ]
        # "Prezzo unitario" (= totale pezzo PRIMA del margine) solo nell'interno:
        # al cliente rivelerebbe il costo. Nel cliente il box mostra solo il totale.
        if interno:
            total_block_rows.append([Paragraph(
                f'<font size=8 color="#FFFFFF">Prezzo unitario: {_eur(totale_pezzo)}</font>',
                self.style_small,
            )])
        total_table = Table(total_block_rows, colWidths=[avail * 0.42])
        total_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), self.COLOR_PRIMARY),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 18),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 18),
                    ("TOPPADDING", (0, 0), (0, 0), 14),
                    ("BOTTOMPADDING", (0, -1), (0, -1), 14),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0, self.COLOR_PRIMARY),
                    ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                ]
            )
        )

        # --- Compose info + total side by side ---
        combo = Table(
            [[info_table, total_table]],
            colWidths=[avail * 0.55, avail * 0.45],
        )
        combo.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        elements.append(combo)
        elements.append(Spacer(1, 8 * mm))

        # Cliente: cover finisce qui (niente KPI decomposti né composizione costi)
        if not interno:
            return

        # --- KPI row ---
        n_articoli = len(dati.get("articoli", []))
        costi_montaggio = dati.get("costi_montaggio", {})
        n_assiemi = len(costi_montaggio)
        sald_mt_tot = sum(m.get("saldatura_mt", 0) for m in costi_montaggio.values())
        tubolari_per_asm = dati.get("tubolari_per_assieme", {})
        n_tubi = sum(
            len(t.get("analisi", {}).get("tubi", []))
            for t in tubolari_per_asm.values()
        )

        kpi_cells = [
            [
                Paragraph('<font size=7 color="#94A3B8"><b>ARTICOLI</b></font>', self.style_small),
                Paragraph('<font size=7 color="#94A3B8"><b>ASSIEMI</b></font>', self.style_small),
                Paragraph('<font size=7 color="#94A3B8"><b>SALDATURA</b></font>', self.style_small),
                Paragraph('<font size=7 color="#94A3B8"><b>TUBOLARI</b></font>', self.style_small),
            ],
            [
                Paragraph(f'<font size=14 name="Helvetica-Bold">{n_articoli}</font>', self.style_body_bold),
                Paragraph(f'<font size=14 name="Helvetica-Bold">{n_assiemi}</font>', self.style_body_bold),
                Paragraph(f'<font size=14 name="Helvetica-Bold">{sald_mt_tot:.1f}</font> <font size=8 color="#94A3B8">mt</font>', self.style_body_bold),
                Paragraph(f'<font size=14 name="Helvetica-Bold">{n_tubi}</font>', self.style_body_bold),
            ],
        ]
        kpi_table = Table(
            kpi_cells,
            colWidths=[avail * 0.25] * 4,
        )
        kpi_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), self.COLOR_LIGHT),
                    ("BOX", (0, 0), (-1, -1), 0.5, self.COLOR_BORDER),
                    ("LINEAFTER", (0, 0), (2, -1), 0.5, self.COLOR_BORDER),
                    ("TOPPADDING", (0, 0), (-1, 0), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                    ("TOPPADDING", (0, 1), (-1, 1), 2),
                    ("BOTTOMPADDING", (0, 1), (-1, 1), 12),
                    ("LEFTPADDING", (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        elements.append(kpi_table)
        elements.append(Spacer(1, 8 * mm))

        # --- Composition donut chart ---
        cost_items = self._collect_all_costs(dati)
        if cost_items:
            elements.append(Paragraph("Composizione costi", self.style_heading))
            elements.append(Spacer(1, 2 * mm))

            # Build donut (Pie) drawing
            d = Drawing(avail * 0.50, 140)
            pie = Pie()
            pie.x = 20
            pie.y = 5
            pie.width = 130
            pie.height = 130
            pie.data = [v for _, v in cost_items]
            pie.labels = None
            pie.sideLabels = 0
            pie.simpleLabels = 1
            pie.slices.strokeColor = colors.white
            pie.slices.strokeWidth = 1.5
            slice_colors = [
                self.COLOR_PRIMARY,
                self.COLOR_SUCCESS,
                self.COLOR_WARNING,
                colors.Color(99 / 255, 102 / 255, 241 / 255),
                colors.Color(168 / 255, 85 / 255, 247 / 255),
                colors.Color(14 / 255, 165 / 255, 233 / 255),
                colors.Color(244 / 255, 63 / 255, 94 / 255),
                colors.Color(236 / 255, 72 / 255, 153 / 255),
                colors.Color(20 / 255, 184 / 255, 166 / 255),
                colors.Color(249 / 255, 115 / 255, 22 / 255),
            ]
            for i in range(len(cost_items)):
                pie.slices[i].fillColor = slice_colors[i % len(slice_colors)]
            d.add(pie)

            # Legend table
            total_costi = sum(v for _, v in cost_items)
            legend_rows = []
            for i, (name, val) in enumerate(cost_items):
                p = (val / total_costi * 100) if total_costi > 0 else 0
                col = slice_colors[i % len(slice_colors)]
                col_box = Table([[""]], colWidths=[8], rowHeights=[8])
                col_box.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (0, 0), col),
                            ("BOX", (0, 0), (0, 0), 0, col),
                            ("TOPPADDING", (0, 0), (0, 0), 0),
                            ("BOTTOMPADDING", (0, 0), (0, 0), 0),
                            ("LEFTPADDING", (0, 0), (0, 0), 0),
                            ("RIGHTPADDING", (0, 0), (0, 0), 0),
                        ]
                    )
                )
                legend_rows.append([
                    col_box,
                    Paragraph(f'<font size=8>{name}</font>', self.style_body),
                    Paragraph(f'<font size=8 color="#94A3B8">{p:.1f}%</font>', self.style_small),
                    Paragraph(f'<font size=8 name="Helvetica-Bold">{_eur_plain(val)}</font>', self.style_body_bold),
                ])

            legend_table = Table(
                legend_rows,
                colWidths=[14, avail * 0.22, 32, 50],
            )
            legend_table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("LEFTPADDING", (0, 0), (-1, -1), 2),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )

            donut_row = Table(
                [[d, legend_table]],
                colWidths=[avail * 0.48, avail * 0.52],
            )
            donut_row.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            elements.append(donut_row)

    def _collect_all_costs(self, dati) -> list:
        """Aggregate all cost categories for donut chart."""
        items = []
        mapping = [
            ("Materiale", dati.get("costo_materiale", 0)),
            ("Piegatura", dati.get("costo_piegatura", 0)),
            ("Saldatura", dati.get("costo_saldatura", 0)),
            ("Filettatura", dati.get("costo_filettatura", 0)),
            ("Svasatura", dati.get("costo_svasatura", 0)),
            ("Montaggio", dati.get("costo_montaggio_totale", 0)),
            ("Tubolari", dati.get("costo_tubolari_totale", 0)),
            ("Piastre", dati.get("costo_piastre_totale", 0)),
        ]
        # Compute materiale from articoli if not directly provided
        if mapping[0][1] == 0:
            articoli = dati.get("articoli", [])
            mat = sum(a.get("costo", 0) for a in articoli)
            mapping[0] = ("Materiale", mat)
        for name, val in mapping:
            if val > 0:
                items.append((name, val))
        return items

    # ------------------------------------------------------------------
    # Signature / acceptance box
    # ------------------------------------------------------------------

    def _build_signature_box(self, elements, dati, interno=False):
        """Blocco 'condizioni + accettazione' coeso.

        Tutto il blocco (nota prezzi + Accettazione + firma) è tenuto insieme con
        KeepTogether: non si spezza tra pagine e la nota non resta orfana in cima.
        Spaziatura equilibrata per un'aria pulita anche su pagina poco piena.
        """
        avail = _W - 2 * _MARGIN
        block = []

        # Nota prezzi (solo cliente): apre il blocco condizioni/accettazione
        if not interno:
            block.append(
                Paragraph(
                    "Prezzi in EUR, IVA esclusa. Preventivo salvo conferma "
                    "disponibilita' materiali.",
                    self.style_note,
                )
            )
            block.append(Spacer(1, 10 * mm))

        block.append(Paragraph("Accettazione", self.style_heading))
        block.append(Spacer(1, 4 * mm))

        # Three columns: Data | Firma cliente | Timbro
        empty_line = '<font size=7 color="#94A3B8">___________________________</font>'
        rows = [
            [
                Paragraph('<font size=8 color="#94A3B8"><b>DATA ACCETTAZIONE</b></font>', self.style_small),
                Paragraph('<font size=8 color="#94A3B8"><b>FIRMA CLIENTE</b></font>', self.style_small),
                Paragraph('<font size=8 color="#94A3B8"><b>TIMBRO</b></font>', self.style_small),
            ],
            [
                Paragraph(empty_line, self.style_body),
                Paragraph(empty_line, self.style_body),
                Paragraph(empty_line, self.style_body),
            ],
        ]
        t = Table(rows, colWidths=[avail / 3] * 3, rowHeights=[34, 72])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), self.COLOR_LIGHT),
                    ("BOX", (0, 0), (-1, -1), 0.5, self.COLOR_BORDER),
                    ("LINEAFTER", (0, 0), (1, -1), 0.5, self.COLOR_BORDER),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.COLOR_BORDER),
                    # Etichette con più aria dai bordi cella (sopra/sotto/lati)
                    ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, 0), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
                    # Riga firma alta: la linea sta in fondo → ben staccata dalle
                    # etichette, con spazio vero per scrivere data/firma/timbro.
                    ("TOPPADDING", (0, 1), (-1, 1), 44),
                    ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 16),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                    ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
                    ("ALIGN", (0, 1), (-1, 1), "CENTER"),
                ]
            )
        )
        block.append(t)
        block.append(Spacer(1, 5 * mm))
        block.append(
            Paragraph(
                '<font size=7 color="#94A3B8"><i>La firma del cliente costituisce accettazione del preventivo secondo i termini e le condizioni indicate.</i></font>',
                self.style_small,
            )
        )

        # Respiro sopra + blocco unico che non si separa
        elements.append(Spacer(1, 12 * mm))
        elements.append(KeepTogether(block))

    # ------------------------------------------------------------------
    # Notes section
    # ------------------------------------------------------------------

    def _build_notes(self, elements, dati):
        """Add notes section if present."""
        note = dati.get("note", "")
        if not note:
            return

        elements.append(Paragraph("Note", self.style_heading))
        # Light background box via table
        note_text = note.replace("\n", "<br/>")
        para = Paragraph(note_text, self.style_note)
        t = Table([[para]], colWidths=[_W - 2 * _MARGIN])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), self.COLOR_LIGHT),
                    ("BOX", (0, 0), (0, 0), 0.5, self.COLOR_BORDER),
                    ("TOPPADDING", (0, 0), (0, 0), 8),
                    ("BOTTOMPADDING", (0, 0), (0, 0), 8),
                    ("LEFTPADDING", (0, 0), (0, 0), 10),
                    ("RIGHTPADDING", (0, 0), (0, 0), 10),
                ]
            )
        )
        elements.append(t)
        elements.append(Spacer(1, 4 * mm))
