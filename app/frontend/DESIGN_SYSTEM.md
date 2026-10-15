# 🎨 LS FerroTrack Design System v1.0

## Documento di Riferimento Stilistico

Questo documento definisce lo **stile di riferimento univoco** per l'intero progetto. Tutte le pagine (impiegata.html, officina.html, laser-v2.html, etc.) devono seguire esattamente questi criteri.

---

## 📐 CSS Foundation

### :root Variables (Colori e Tema)
```css
:root {
  --bg: #fafafa;
  --white: #ffffff;
  --black: #000000;
  --green: #1a7a48;
  --green-light: #22a060;
  --gray-text: #666666;
  --gray-muted: #999999;
  --border: #e5e5e5;
  --blue: #1e40af;
  --blue-bg: #dbeafe;
  --red: #991b1b;
  --red-bg: #fee2e2;
  --orange: #92400e;
  --orange-bg: #fef3c7;
  --green-badge: #166534;
  --green-badge-bg: #dcfce7;
}
```

---

## 🖼️ Componenti Standard

### 1. TOPBAR (Navigazione Fissa)
- **Altezza**: 64px
- **Position**: fixed (top: 0, z-index: 1000)
- **Padding**: 0 48px
- **Componenti**:
  - `.logo-section` - Logo + Testo
  - `.topbar-actions` - Pulsanti Home e Logout

### 2. TAB BAR (Navigazione Secondaria)
- **Position**: fixed (top: 64px, z-index: 999)
- **Stile**: Tab con underline green all'attivo
- **Classes**: `.tab-item.active` / `.tab-item`

### 3. FORM ELEMENTS

#### Label
- `.form-label`: 12px, uppercase, 600 weight, gray-text color

#### Input
- `.form-input`: 40px height, 13px font-size, border-color green on focus
- `.form-input:focus`: green border + shadow 3px rgba(26,122,72,0.1)

#### Button Upload (WCAG 2.1)
- `.upload-button`: min-height 48px, 2px border
- `.upload-button-pdf`: green border, green text
- `.upload-button-pdf:hover`: green background, white text
- `.upload-button-dxf`: blue border, blue text

#### Feedback Status
- `.upload-status.success`: green background (#dcfce7), green text (#166534)
- `.upload-status.error`: red background (#fee2e2), red text (#991b1b)
- Auto-hide dopo 4s

#### Progress Bar
- `.progress-bar`: 6px height, gray background
- `.progress-fill`: gradient blue, animated width

### 4. CARD COMPONENTS
- `.form-card`: 700px width, 36px padding, 12px border-radius, subtle shadow
- `.dest-option`: 56px height, border 2px, selected states con colori distintivi

### 5. BUTTONS
- `.btn-home`: green background, 8px 16px padding
- `.btn-logout`: white background, gray border
- `.btn-submit`: green background, 48px height, 100% width

---

## 🎯 Design Principles

1. **Colori Coerenti**: Sempre usare le variabili CSS (:root)
2. **Spaziatura**:
   - Gap tra elementi: 24px (form-row), 28px (form-card gap)
   - Padding interno: 36px (card), 10px (form-group)
3. **Tipografia**:
   - Body: Inter, sans-serif
   - Heading: Geist, sans-serif
   - Font weight: 400-600-700-900
4. **Accessibilità**:
   - Min height button: 48px (WCAG touch target)
   - Min contrast: 3:1 (WCAG AA)
   - Focus state: visible outline 3px
5. **Animazioni**:
   - Transition: all 0.2s ease
   - Hover: opacity o color changes
   - Active: transform scale(0.98)

---

## 📋 HTML Structure Standard

Tutte le pagine devono avere:

```html
<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LS FerroTrack — [Role/Page]</title>
  <link href="/vendor/fonts/fonts.css" rel="stylesheet">
  <style>
    /* CSS da impiegata.html - IDENTICO */
  </style>
</head>
<body>
  <!-- TOPBAR -->
  <div class="topbar">
    <a class="logo-section" href="/impiegata.html">
      <div class="logo-icon">⭐</div>
      <div class="logo-text">
        <div class="logo-main">FerroTrack</div>
        <div class="logo-sub">LS</div>
      </div>
    </a>
    <div class="topbar-actions">
      <button class="btn-home" onclick="window.location.href='/impiegata.html'">Home</button>
      <button class="btn-logout" onclick="logout()">Esci</button>
    </div>
  </div>

  <!-- TAB BAR (se necessario) -->
  <div class="tab-bar">
    <!-- tab items -->
  </div>

  <!-- CONTENT -->
  <!-- ... -->

  <script>
    // JavaScript condiviso
  </script>
</body>
</html>
```

---

## ✅ Checklist di Conformità

- [ ] CSS :root variables identici a impiegata.html
- [ ] Topbar identica (layout, colori, componenti)
- [ ] Tutti i bottoni usano le classi standard (.btn-home, .btn-logout, .upload-button)
- [ ] Form elements usano .form-input, .form-label, .form-group, .form-row
- [ ] Colori presi sempre da :root, mai hardcoded
- [ ] Focus states visibili (outline 3px)
- [ ] Min 48px touch target per bottoni interattivi
- [ ] Animazioni smooth (0.2s ease)
- [ ] Spazi e padding coerenti (36px card, 28px gap)
- [ ] Font: Inter (body), Geist (heading)

---

## 📌 File di Riferimento

**impiegata.html** è il file di riferimento assoluto. In caso di dubbio su styling, consultare questo file.

Ultima aggiornamento: 2026-03-04
