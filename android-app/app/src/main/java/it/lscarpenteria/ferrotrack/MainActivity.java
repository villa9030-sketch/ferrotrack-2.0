package it.lscarpenteria.ferrotrack;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * FerroTrack LS - app per i tablet (Tablet ore, Tablet officina).
 *
 * Una sola app per tutti i tablet: al primo avvio si sceglie l'indirizzo del
 * server e la postazione, e restano salvati. Per cambiarli: 5 tocchi rapidi
 * nell'angolo in alto a sinistra, oppure "Impostazioni" nella schermata che
 * compare quando il server non risponde.
 *
 * Se il server non risponde (riavvio, PC spento, rete) non resta una pagina
 * bianca: compare un avviso e l'app riprova da sola ogni 10 secondi.
 */
public class MainActivity extends Activity {

    private static final String PREF = "ferrotrack";
    private static final String SERVER_DEFAULT = "http://192.168.1.10:5000";
    private static final int RIPROVA_MS = 10000;

    private WebView webView;
    private LinearLayout avviso;
    private TextView avvisoTesto;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean inErrore = false;

    // 5 tocchi nell'angolo in alto a sinistra -> impostazioni
    private int tocchi = 0;
    private long primoTocco = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        schermoIntero();
        if (pagina() == null) mostraImpostazioni();
        else mostraApp();
    }

    // ------------------------------------------------------------------ dati
    private SharedPreferences prefs() { return getSharedPreferences(PREF, MODE_PRIVATE); }
    private String server() { return prefs().getString("server", SERVER_DEFAULT); }
    private String pagina() { return prefs().getString("pagina", null); }
    private String indirizzo() {
        String s = server();
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s + "/" + pagina();
    }

    // ------------------------------------------------------------------- app
    private void mostraApp() {
        inErrore = false;
        android.widget.FrameLayout radice = new android.widget.FrameLayout(this);
        webView = new WebView(this);
        radice.addView(webView);

        // Avviso "server non raggiungibile", sopra la pagina
        avviso = colonna();
        avviso.setBackgroundColor(Color.WHITE);
        avviso.setVisibility(View.GONE);
        avvisoTesto = testo("", 22, true);
        avviso.addView(avvisoTesto);
        avviso.addView(testo("Riprovo da solo ogni 10 secondi.", 16, false));
        Button riprova = bottone("Riprova adesso");
        riprova.setOnClickListener(v -> carica());
        avviso.addView(riprova);
        Button imp = bottone("Impostazioni");
        imp.setOnClickListener(v -> mostraImpostazioni());
        avviso.addView(imp);
        radice.addView(avviso);
        setContentView(radice);

        // I cookie tengono la registrazione del tablet (fatta col PIN)
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMediaPlaybackRequiresUserGesture(false);

        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                // si resta sempre sul server FerroTrack, mai nel browser di sistema
                return !r.getUrl().toString().startsWith(server());
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r.isForMainFrame()) erroreRete();
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                if (!inErrore) avviso.setVisibility(View.GONE);
                CookieManager.getInstance().flush();
            }
        });
        carica();
    }

    private void carica() {
        inErrore = false;
        handler.removeCallbacksAndMessages(null);
        webView.loadUrl(indirizzo());
    }

    private void erroreRete() {
        inErrore = true;
        avvisoTesto.setText("FerroTrack non risponde\n" + server()
                + "\n\nIl PC del server e' acceso? Il tablet e' sulla rete dell'officina?");
        avviso.setVisibility(View.VISIBLE);
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(this::carica, RIPROVA_MS);
    }

    // ----------------------------------------------------------- impostazioni
    private void mostraImpostazioni() {
        handler.removeCallbacksAndMessages(null);
        LinearLayout c = colonna();
        c.addView(testo("FerroTrack - impostazioni del tablet", 26, true));
        c.addView(testo("Indirizzo del server", 16, false));
        final EditText srv = new EditText(this);
        srv.setText(server());
        srv.setTextSize(22);
        srv.setSingleLine(true);
        c.addView(srv, larghezza());
        c.addView(testo("Che tablet e' questo?", 16, false));
        String[][] scelte = {
            {"Tablet ore (timbratrice)", "ore.html"},
            {"Tablet officina (ordini e disegni)", "operaio-info.html"},
        };
        for (final String[] sc : scelte) {
            Button b = bottone(sc[0] + (sc[1].equals(pagina()) ? "   (attuale)" : ""));
            b.setOnClickListener(v -> {
                String s = srv.getText().toString().trim();
                if (!s.startsWith("http")) s = "http://" + s;
                prefs().edit().putString("server", s).putString("pagina", sc[1]).apply();
                mostraApp();
            });
            c.addView(b);
        }
        if (pagina() != null) {
            Button annulla = bottone("Annulla");
            annulla.setOnClickListener(v -> mostraApp());
            c.addView(annulla);
        }
        setContentView(c);
    }

    // 5 tocchi rapidi in alto a sinistra aprono le impostazioni (nascosto agli operai)
    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) {
            float lato = 90 * getResources().getDisplayMetrics().density;
            if (ev.getX() < lato && ev.getY() < lato) {
                long ora = System.currentTimeMillis();
                if (ora - primoTocco > 3000) { tocchi = 0; primoTocco = ora; }
                if (++tocchi >= 5) { tocchi = 0; mostraImpostazioni(); return true; }
            } else {
                tocchi = 0;
            }
        }
        return super.dispatchTouchEvent(ev);
    }

    // ---------------------------------------------------------------- comodi
    private LinearLayout colonna() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setGravity(Gravity.CENTER);
        int p = (int) (40 * getResources().getDisplayMetrics().density);
        l.setPadding(p, p, p, p);
        l.setBackgroundColor(Color.WHITE);
        return l;
    }

    private TextView testo(String t, int dim, boolean grassetto) {
        TextView v = new TextView(this);
        v.setText(t);
        v.setTextSize(dim);
        v.setTextColor(Color.parseColor("#101828"));
        v.setGravity(Gravity.CENTER);
        v.setPadding(0, 16, 0, 16);
        if (grassetto) v.setTypeface(Typeface.DEFAULT_BOLD);
        return v;
    }

    private Button bottone(String t) {
        Button b = new Button(this);
        b.setText(t);
        b.setTextSize(20);
        b.setAllCaps(false);
        b.setMinHeight((int) (64 * getResources().getDisplayMetrics().density));
        b.setLayoutParams(larghezza());
        return b;
    }

    private LinearLayout.LayoutParams larghezza() {
        int w = (int) (520 * getResources().getDisplayMetrics().density);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(w, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, 10, 0, 10);
        return lp;
    }

    private void schermoIntero() {
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) schermoIntero();
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    // Indietro: naviga nella pagina, non esce dall'app per sbaglio
    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
    }
}
