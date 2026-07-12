"""
ScoreGex — Plateforme Quantitative d'Évaluation Immobilière
Pays de Gex — Genève Frontalier
Design : noir #0F241A + or #4FA37A + blanc cassé #F5F0E8
"""

import streamlit as st
import requests
import json
import time
import io
import csv

# ── Config page ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ScoreGex — Intelligence Immobilière Quantitative",
    page_icon="🏛️",
    layout="wide",
    # MODIF 11/07/2026 : "expanded" au lieu de "collapsed". Avec "collapsed",
    # la sidebar (seule navigation reellement fonctionnelle -- voir plus bas)
    # etait invisible par defaut, y compris sur mobile. C'est tres
    # probablement pourquoi la page Tarifs semblait "ne pas exister" : elle
    # existait dans le code mais n'etait joignable qu'en ouvrant une sidebar
    # cachee. Corrige ici + barre de nav du haut rendue cliquable ci-dessous.
    initial_sidebar_state="expanded"
)

API_BASE = "https://scoregex.vercel.app"

# Auth Supabase — nécessaires ici (côté Streamlit) pour que l'app puisse
# appeler directement /auth/v1/signup et /auth/v1/token (Supabase Auth),
# séparément des secrets déjà configurés côté GitHub Actions. À ajouter
# dans Streamlit Cloud : Settings > Secrets, PAS dans le repo.
try:
    SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
    SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")
except Exception:
    SUPABASE_URL, SUPABASE_ANON_KEY = "", ""

# TODO Helen : remplace ces deux liens par tes vrais Stripe Payment Links
# avant mise en prod (Stripe Dashboard > Payment Links > créer un lien pour
# le prix "Frontalier Pro 99€/mois" > copier l'URL). Tant que ce n'est pas
# fait, les boutons "Passer Pro" mènent vers une URL Stripe factice — ils ne
# doivent pas être visibles publiquement en l'état.
STRIPE_LINK_FRONTALIER_PRO = "https://buy.stripe.com/REMPLACE_MOI_PAR_TON_LIEN_STRIPE"

# Base Adresse Nationale (BAN) -- API officielle data.gouv.fr, gratuite,
# sans cle requise. Utilisee pour convertir une adresse tapee par
# l'utilisateur en latitude/longitude, afin qu'il n'ait plus besoin de les
# chercher lui-meme sur Google Maps. Documentation :
# https://adresse.data.gouv.fr/api-doc/adresse
BAN_API_URL = "https://api-adresse.data.gouv.fr/search/"


def geocode_adresse(query: str, limit: int = 5):
    """Convertit une adresse texte en coordonnees GPS via la BAN. Retourne
    une liste [(label_complet, lat, lon), ...], vide si aucun resultat ou si
    l'API est injoignable -- jamais de coordonnee inventee en remplacement."""
    if not query or len(query.strip()) < 3:
        return []
    try:
        resp = requests.get(
            BAN_API_URL,
            params={"q": query.strip(), "limit": limit, "autocomplete": 1},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            coords = feat.get("geometry", {}).get("coordinates")  # [lon, lat]
            if coords and len(coords) == 2:
                lon_c, lat_c = coords
                results.append((props.get("label", query), lat_c, lon_c))
        return results
    except Exception:
        return []

def html_block(s: str) -> str:
    """Supprime l'indentation Python de chaque ligne avant de passer le HTML
    à st.markdown(). Sans cela, Markdown interprete les lignes indentees
    (>=4 espaces, frequent quand le bloc est imbrique dans des if/else)
    comme un bloc de code et les affiche telles quelles au lieu de les
    rendre en HTML — c'est le bug qui affichait le code source a l'ecran."""
    return "\n".join(line.strip() for line in s.strip("\n").splitlines())


# ── CSS Global ────────────────────────────────────────────────────────────────
st.markdown(html_block("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Playfair+Display:wght@400;700&display=swap');

/* Reset & base */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body, [data-testid="stAppViewContainer"] {
    background-color: #0F241A !important;
    color: #F5F0E8 !important;
    font-family: 'Inter', sans-serif;
}
[data-testid="stHeader"] { background: transparent !important; }
[data-testid="stSidebar"] { background: #111111 !important; }
.block-container { padding: 0 !important; max-width: 100% !important; }
section[data-testid="stMain"] > div { padding: 0 !important; }

/* Hide Streamlit branding */
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stToolbar"] { display: none; }

/* Typography */
.sg-display { font-family: 'Playfair Display', serif; }
.sg-mono { font-family: 'Courier New', monospace; }

/* Navigation */
.sg-nav {
    background: rgba(10,10,10,0.95);
    border-bottom: 1px solid #1E3A29;
    padding: 0 3rem;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(12px);
}
.sg-logo {
    font-family: 'Playfair Display', serif;
    font-size: 1.4rem;
    font-weight: 700;
    color: #4FA37A;
    letter-spacing: 0.05em;
}
.sg-logo span { color: #F5F0E8; font-weight: 300; }
.sg-nav-links { display: flex; gap: 2rem; align-items: center; }
.sg-nav-link {
    color: #888;
    text-decoration: none;
    font-size: 0.85rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    transition: color 0.2s;
}
.sg-badge {
    background: #4FA37A;
    color: #0F241A;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 0.3rem 0.8rem;
    border-radius: 2px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
}

/* MODIF 11/07/2026 : barre de nav du haut rendue fonctionnelle.
   Avant, "Estimer / Marché / Tarifs" étaient des <span> HTML statiques sans
   aucune action associée -- ça ressemblait à un menu cliquable mais ça ne
   menait nulle part. Remplacés par de vrais st.button() dans un
   st.container(key="topnav"), stylés ici pour ressembler à des liens de nav
   plutôt qu'à des boutons dorés pleins. */
div.st-key-topnav {
    background: rgba(10,10,10,0.96);
    border-bottom: 1px solid #1E3A29;
    padding: 0.6rem 2rem 0.3rem;
    position: sticky;
    top: 0;
    z-index: 999;
    backdrop-filter: blur(12px);
}
div.st-key-topnav .stButton button {
    background: transparent !important;
    color: #999 !important;
    border: none !important;
    box-shadow: none !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    padding: 0.5rem 0.3rem !important;
}
div.st-key-topnav .stButton button:hover {
    color: #4FA37A !important;
    background: rgba(79,163,122,0.08) !important;
}
div.st-key-topnav .stButton button:focus:not(:active) {
    color: #4FA37A !important;
    border: none !important;
    box-shadow: none !important;
}

/* MODIF 12/07/2026 : liens du footer rendus fonctionnels (meme bug que
   l'ancienne top-nav -- c'etaient des <span> statiques sans destination). */
div.st-key-footernav .stButton button {
    background: transparent !important;
    color: #777 !important;
    border: none !important;
    box-shadow: none !important;
    font-size: 0.78rem !important;
    font-weight: 400 !important;
    width: auto !important;
}
div.st-key-footernav .stButton button:hover {
    color: #4FA37A !important;
    background: transparent !important;
}
div.st-key-footernav a[kind="secondary"], div.st-key-footernav .stLinkButton a {
    background: transparent !important;
    color: #777 !important;
    border: none !important;
    box-shadow: none !important;
    font-size: 0.78rem !important;
    font-weight: 400 !important;
}
div.st-key-footernav a[kind="secondary"]:hover, div.st-key-footernav .stLinkButton a:hover {
    color: #4FA37A !important;
}

/* Bannière d'avertissement sur les pages legales (brouillon, pas relu par un juriste) */
.sg-legal-disclaimer {
    background: rgba(79,163,122,0.08);
    border: 1px solid rgba(79,163,122,0.3);
    border-radius: 4px;
    padding: 1rem 1.5rem;
    font-size: 0.82rem;
    color: #9ab8a8;
    margin-bottom: 2rem;
    line-height: 1.6;
}
.sg-legal-body {
    font-size: 0.9rem;
    color: #C0B89A;
    line-height: 1.8;
}
.sg-legal-body h3 {
    font-family: 'Playfair Display', serif;
    color: #F5F0E8;
    font-size: 1.2rem;
    margin: 2rem 0 0.75rem;
}
.sg-legal-body strong { color: #F5F0E8; }

/* Hero */
.sg-hero {
    min-height: 90vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 4rem 2rem;
    position: relative;
    overflow: hidden;
}
.sg-hero::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(ellipse 60% 50% at 50% 30%, rgba(79,163,122,0.06) 0%, transparent 70%);
    pointer-events: none;
}
.sg-eyebrow {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    color: #4FA37A;
    margin-bottom: 1.5rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
}
.sg-eyebrow::before, .sg-eyebrow::after {
    content: '';
    width: 40px;
    height: 1px;
    background: #4FA37A;
    opacity: 0.5;
}
.sg-h1 {
    font-family: 'Playfair Display', serif;
    font-size: clamp(2.8rem, 6vw, 5.5rem);
    font-weight: 700;
    line-height: 1.08;
    color: #F5F0E8;
    max-width: 900px;
    margin-bottom: 1.5rem;
}
.sg-h1 em { color: #4FA37A; font-style: normal; }
.sg-sub {
    font-size: 1.1rem;
    color: #777;
    max-width: 560px;
    line-height: 1.7;
    margin-bottom: 3rem;
    font-weight: 300;
}
.sg-stats-row {
    display: flex;
    gap: 3rem;
    align-items: center;
    margin-top: 3rem;
    padding-top: 3rem;
    border-top: 1px solid #1E3A29;
}
.sg-stat { text-align: center; }
.sg-stat-num {
    font-family: 'Playfair Display', serif;
    font-size: 2.2rem;
    font-weight: 700;
    color: #4FA37A;
    display: block;
}
.sg-stat-lbl { font-size: 0.75rem; color: #555; letter-spacing: 0.1em; text-transform: uppercase; }

/* Buttons */
.sg-btn-primary {
    background: #4FA37A;
    color: #0F241A;
    border: none;
    padding: 0.9rem 2.2rem;
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    cursor: pointer;
    border-radius: 2px;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
}
.sg-btn-ghost {
    background: transparent;
    color: #F5F0E8;
    border: 1px solid #2C4C38;
    padding: 0.9rem 2.2rem;
    font-size: 0.85rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    cursor: pointer;
    border-radius: 2px;
    transition: all 0.2s;
}

/* Section */
.sg-section {
    padding: 6rem 3rem;
    max-width: 1200px;
    margin: 0 auto;
}
.sg-section-full {
    padding: 6rem 3rem;
    background: #112318;
    border-top: 1px solid #1E3A29;
    border-bottom: 1px solid #1E3A29;
}
.sg-section-title {
    font-family: 'Playfair Display', serif;
    font-size: 2.4rem;
    color: #F5F0E8;
    margin-bottom: 0.75rem;
}
.sg-section-sub { font-size: 0.95rem; color: #555; margin-bottom: 3rem; }

/* Score card */
.sg-score-card {
    background: #13291D;
    border: 1px solid #1D3826;
    border-radius: 4px;
    padding: 2rem;
    margin-bottom: 1rem;
}
.sg-score-big {
    font-family: 'Playfair Display', serif;
    font-size: 5rem;
    font-weight: 700;
    color: #4FA37A;
    line-height: 1;
}
.sg-grade {
    display: inline-block;
    background: rgba(79,163,122,0.15);
    border: 1px solid rgba(79,163,122,0.3);
    color: #4FA37A;
    font-size: 1rem;
    font-weight: 700;
    padding: 0.3rem 0.8rem;
    border-radius: 2px;
    letter-spacing: 0.1em;
    margin-top: 0.5rem;
}
.sg-metric {
    display: flex;
    justify-content: space-between;
    padding: 0.75rem 0;
    border-bottom: 1px solid #1E3A29;
    font-size: 0.9rem;
}
.sg-metric-label { color: #555; }
.sg-metric-value { color: #F5F0E8; font-weight: 500; }
.sg-metric-value.positive { color: #4CAF50; }
.sg-metric-value.negative { color: #EF5350; }
.sg-metric-value.gold { color: #4FA37A; }

/* Deal alert */
.sg-deal-box {
    background: rgba(79,163,122,0.08);
    border: 1px solid rgba(79,163,122,0.25);
    border-radius: 4px;
    padding: 1.25rem 1.5rem;
    margin-top: 1rem;
}
.sg-deal-box.nodeal {
    background: rgba(100,100,100,0.06);
    border-color: #2C4C38;
}
.sg-deal-title {
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #4FA37A;
    margin-bottom: 0.4rem;
}
.sg-deal-title.nodeal { color: #555; }

/* Form styling */
.stSelectbox label, .stSlider label, .stNumberInput label {
    color: #777 !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
}
.stSelectbox > div > div {
    background: #13291D !important;
    border-color: #2C4C38 !important;
    color: #F5F0E8 !important;
}
.stNumberInput input {
    background: #13291D !important;
    border-color: #2C4C38 !important;
    color: #F5F0E8 !important;
}
.stButton button {
    background: #4FA37A !important;
    color: #0F241A !important;
    border: none !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    border-radius: 2px !important;
    padding: 0.75rem 1.5rem !important;
    width: 100% !important;
}

/* Pricing */
.sg-pricing-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1.5rem;
    max-width: 900px;
    margin: 0 auto;
}
.sg-plan {
    background: #13291D;
    border: 1px solid #1D3826;
    border-radius: 4px;
    padding: 2rem;
    position: relative;
}
.sg-plan.featured {
    border-color: #4FA37A;
    background: #122619;
}
.sg-plan-badge {
    position: absolute;
    top: -1px;
    left: 50%;
    transform: translateX(-50%);
    background: #4FA37A;
    color: #0F241A;
    font-size: 0.65rem;
    font-weight: 700;
    padding: 0.25rem 1rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
}
.sg-plan-name { font-size: 0.75rem; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: #555; margin-bottom: 0.75rem; }
.sg-plan-price { font-family: 'Playfair Display', serif; font-size: 2.8rem; color: #F5F0E8; margin-bottom: 0.25rem; }
.sg-plan-price span { font-size: 1rem; font-family: 'Inter', sans-serif; color: #555; }
.sg-plan-desc { font-size: 0.85rem; color: #555; margin-bottom: 1.5rem; line-height: 1.5; }
.sg-plan-feature { font-size: 0.82rem; color: #777; padding: 0.4rem 0; border-bottom: 1px solid #1E3A29; display: flex; gap: 0.5rem; align-items: center; }
.sg-plan-feature::before { content: '—'; color: #4FA37A; font-size: 0.7rem; }

/* Data source tag */
.sg-source-tag {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: #13291D;
    border: 1px solid #1D3826;
    border-radius: 2px;
    padding: 0.3rem 0.7rem;
    font-size: 0.72rem;
    color: #555;
    letter-spacing: 0.05em;
    margin-bottom: 0.5rem;
}
.sg-source-dot { width: 6px; height: 6px; border-radius: 50%; background: #4CAF50; }

/* Input section */
.sg-input-section {
    background: #112318;
    border: 1px solid #1D3826;
    border-radius: 4px;
    padding: 2rem;
}
.sg-input-title {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: #4FA37A;
    margin-bottom: 1.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid #1D3826;
}

/* Footer */
.sg-footer {
    background: #0A1911;
    border-top: 1px solid #1E3A29;
    padding: 3rem;
    text-align: center;
}
.sg-footer-logo {
    font-family: 'Playfair Display', serif;
    font-size: 1.2rem;
    color: #4FA37A;
    margin-bottom: 1rem;
}
.sg-footer-copy { font-size: 0.8rem; color: #333; }
.sg-footer-links {
    display: flex;
    justify-content: center;
    gap: 2rem;
    margin: 1rem 0;
}
.sg-footer-link { font-size: 0.78rem; color: #444; }

/* Divider */
.sg-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, #4FA37A, transparent);
    opacity: 0.2;
    margin: 0;
}

/* Communes table */
.sg-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.88rem;
}
.sg-table th {
    color: #555;
    font-size: 0.7rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #1D3826;
    text-align: left;
    font-weight: 500;
}
.sg-table td {
    padding: 0.85rem 1rem;
    border-bottom: 1px solid #16301F;
    color: #C0B89A;
}
.sg-table td:first-child { color: #F5F0E8; font-weight: 500; }
.sg-table td:last-child { color: #4FA37A; font-family: 'Courier New', monospace; font-size: 1rem; }
.sg-table tr:hover td { background: rgba(79,163,122,0.03); }
</style>
"""), unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "home"
if "result" not in st.session_state:
    st.session_state.result = None

# ── Navigation ────────────────────────────────────────────────────────────────
# MODIF 11/07/2026 : anciennement du HTML statique (<span>) sans aucune
# action -- remplace par de vrais boutons Streamlit qui changent
# st.session_state.page, stylés en CSS ci-dessus pour garder l'apparence
# d'une barre de navigation plutot que des boutons pleins.
with st.container(key="topnav"):
    nav_logo, nav_home, nav_est, nav_mkt, nav_price, nav_dash, nav_badge = st.columns(
        [2.2, 1, 1, 1, 1, 1.3, 1]
    )
    with nav_logo:
        st.markdown(
            '<div class="sg-logo" style="padding-top:0.55rem;">Score<span>Gex</span></div>',
            unsafe_allow_html=True,
        )
    with nav_home:
        if st.button("Accueil", key="nav_btn_home", use_container_width=True):
            st.session_state.page = "home"
            st.rerun()
    with nav_est:
        if st.button("Estimer", key="nav_btn_estimate", use_container_width=True):
            st.session_state.page = "estimate"
            st.rerun()
    with nav_mkt:
        if st.button("Marché", key="nav_btn_market", use_container_width=True):
            st.session_state.page = "market"
            st.rerun()
    with nav_price:
        if st.button("Tarifs", key="nav_btn_pricing", use_container_width=True):
            st.session_state.page = "pricing"
            st.rerun()
    with nav_dash:
        if st.button("Dashboard", key="nav_btn_dashboard", use_container_width=True):
            st.session_state.page = "dashboard"
            st.rerun()
    with nav_badge:
        st.markdown(
            '<div style="text-align:right;padding-top:0.6rem;"><span class="sg-badge">Beta</span></div>',
            unsafe_allow_html=True,
        )

# ── Sidebar navigation ────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Navigation")
    if st.button("🏛️  Accueil", use_container_width=True):
        st.session_state.page = "home"
    if st.button("📊  Estimer un bien", use_container_width=True):
        st.session_state.page = "estimate"
    if st.button("📈  Prix du marché", use_container_width=True):
        st.session_state.page = "market"
    if st.button("💳  Tarifs", use_container_width=True):
        st.session_state.page = "pricing"
    if st.button("📁  Dashboard B2B", use_container_width=True):
        st.session_state.page = "dashboard"

    st.markdown("---")
    st.markdown("### Compte")

    if "auth" not in st.session_state:
        st.session_state.auth = None

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.caption("⚠ Authentification non configurée (secrets SUPABASE_URL / SUPABASE_ANON_KEY manquants côté app).")
    elif st.session_state.auth:
        st.caption(f"Connecté : {st.session_state.auth['email']}")
        if st.button("Se déconnecter", use_container_width=True, key="btn_logout"):
            st.session_state.auth = None
            st.session_state.dashboard_data = None
            st.rerun()
    else:
        tab_login, tab_signup = st.tabs(["Connexion", "Créer un compte"])

        with tab_login:
            login_email = st.text_input("Email", key="login_email")
            login_pwd = st.text_input("Mot de passe", type="password", key="login_pwd")
            if st.button("Se connecter", key="btn_login", use_container_width=True):
                try:
                    r = requests.post(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/token",
                        params={"grant_type": "password"},
                        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                        json={"email": login_email, "password": login_pwd},
                        timeout=10,
                    )
                    body = r.json()
                    if r.status_code == 200 and body.get("access_token"):
                        st.session_state.auth = {
                            "access_token": body["access_token"],
                            "email": body.get("user", {}).get("email", login_email),
                            "user_id": body.get("user", {}).get("id"),
                        }
                        st.rerun()
                    else:
                        st.error(f"Connexion refusée : {body.get('error_description') or body.get('msg') or r.status_code}")
                except Exception as e:
                    st.error(f"Connexion impossible : {e}")

        with tab_signup:
            signup_email = st.text_input("Email", key="signup_email")
            signup_pwd = st.text_input("Mot de passe (8 caractères min.)", type="password", key="signup_pwd")
            if st.button("Créer mon compte", key="btn_signup", use_container_width=True):
                try:
                    r = requests.post(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/signup",
                        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                        json={"email": signup_email, "password": signup_pwd},
                        timeout=10,
                    )
                    body = r.json()
                    if r.status_code in (200, 201) and body.get("access_token"):
                        st.session_state.auth = {
                            "access_token": body["access_token"],
                            "email": body.get("user", {}).get("email", signup_email),
                            "user_id": body.get("user", {}).get("id"),
                        }
                        st.success("Compte créé et connecté.")
                        st.rerun()
                    elif r.status_code in (200, 201):
                        st.success("Compte créé — vérifie ta boîte mail pour confirmer l'adresse avant de te connecter.")
                    else:
                        st.error(f"Échec de la création : {body.get('error_description') or body.get('msg') or r.status_code}")
                except Exception as e:
                    st.error(f"Connexion impossible : {e}")

    st.markdown("---")
    st.markdown(html_block("""
    <div style="font-size:0.72rem;color:#333;line-height:1.6;">
    Données DVF réelles<br>
    DGFiP 2014–2025<br>
    747 transactions calibrées<br>
    8 communes Pays de Gex
    </div>
    """), unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════════════════════════
# PAGE : HOME
# ════════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "home":

    # Hero
    st.markdown(html_block("""
    <div class="sg-hero">
        <div class="sg-eyebrow">Intelligence Immobilière Quantitative</div>
        <h1 class="sg-h1">
            L'immobilier du Pays de Gex<br>est un <em>dérivé du Franc Suisse</em>
        </h1>
        <p class="sg-sub">
            127 modèles calibrés sur les transactions DVF réelles 2014–2025.
            Le seul AVM qui intègre le différentiel CHF/EUR,
            le désert médical, et la donnée frontalière genevoise.
        </p>
        <div class="sg-stats-row">
            <div class="sg-stat">
                <span class="sg-stat-num">747</span>
                <span class="sg-stat-lbl">Transactions réelles</span>
            </div>
            <div class="sg-stat">
                <span class="sg-stat-num">8</span>
                <span class="sg-stat-lbl">Communes Gex</span>
            </div>
            <div class="sg-stat">
                <span class="sg-stat-num">92k</span>
                <span class="sg-stat-lbl">Frontaliers actifs</span>
            </div>
            <div class="sg-stat">
                <span class="sg-stat-num">×2,1</span>
                <span class="sg-stat-lbl">Différentiel CHF/EUR</span>
            </div>
        </div>
    </div>
    <div class="sg-divider"></div>
    """), unsafe_allow_html=True)

    # Features
    st.markdown(html_block("""
    <div class="sg-section">
        <div style="text-align:center; margin-bottom: 4rem;">
            <p class="sg-eyebrow" style="justify-content:center;">Ce que ScoreGex calcule</p>
            <h2 class="sg-section-title" style="text-align:center;">Trois niveaux d'intelligence</h2>
        </div>
    </div>
    """), unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(html_block("""
        <div class="sg-score-card">
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#4FA37A;margin-bottom:1rem;">01 — AVM Quantitatif</div>
            <div style="font-size:1.5rem;font-family:'Playfair Display',serif;color:#F5F0E8;margin-bottom:0.75rem;">Prix de Marché Réel</div>
            <div style="font-size:0.85rem;color:#555;line-height:1.7;">
                Calibré sur les transactions DVF réelles. Ajusté par DPE, surface, commune et position frontalière.
                Pas une estimation générique — un calcul sur votre bien précis.
            </div>
        </div>
        """), unsafe_allow_html=True)

    with col2:
        st.markdown(html_block("""
        <div class="sg-score-card" style="border-color:#4FA37A;">
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#4FA37A;margin-bottom:1rem;">02 — Score GexScore</div>
            <div style="font-size:1.5rem;font-family:'Playfair Display',serif;color:#F5F0E8;margin-bottom:0.75rem;">Indice 0–1000</div>
            <div style="font-size:0.85rem;color:#555;line-height:1.7;">
                Score composite intégrant localisation frontalière, DPE, désert médical,
                bruit A40/GVA, et score ESG. Grade de AAA à CCC, comme une notation obligataire.
            </div>
        </div>
        """), unsafe_allow_html=True)

    with col3:
        st.markdown(html_block("""
        <div class="sg-score-card">
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#4FA37A;margin-bottom:1rem;">03 — Deal Alert</div>
            <div style="font-size:1.5rem;font-family:'Playfair Display',serif;color:#F5F0E8;margin-bottom:0.75rem;">Signal Temps Réel</div>
            <div style="font-size:0.85rem;color:#555;line-height:1.7;">
                Détection automatique des biens sous-évalués vs le marché DVF réel.
                Signal Deal si écart > 5%. Quantification de l'économie potentielle en EUR.
            </div>
        </div>
        """), unsafe_allow_html=True)

    st.markdown('<div class="sg-divider"></div>', unsafe_allow_html=True)

    # CTA
    st.markdown(html_block("""
    <div style="text-align:center;padding:5rem 2rem;">
        <p class="sg-eyebrow" style="justify-content:center;">Commencer maintenant</p>
        <h2 style="font-family:'Playfair Display',serif;font-size:2rem;color:#F5F0E8;margin-bottom:1rem;">
            3 estimations gratuites
        </h2>
        <p style="color:#555;font-size:0.95rem;margin-bottom:2rem;">Aucune carte bancaire requise. Accès immédiat.</p>
    </div>
    """), unsafe_allow_html=True)

    # MODIF 11/07/2026 : ajout d'un 2e CTA "Passer Pro" a cote du CTA gratuit
    # existant (proposition doc audit Meta, partie legitime -- il manquait
    # bien un chemin direct vers le paiement depuis la home, meme si la
    # page Tarifs elle-meme existait deja et n'etait pas vide).
    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_b:
        btn_free, btn_pro = st.columns(2)
        with btn_free:
            if st.button("ESTIMER GRATUITEMENT 3X", use_container_width=True):
                st.session_state.page = "estimate"
                st.rerun()
        with btn_pro:
            st.link_button("PASSER PRO 99€/MOIS", STRIPE_LINK_FRONTALIER_PRO, use_container_width=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : ESTIMATION
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "estimate":

    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;">
        <p class="sg-eyebrow">Moteur AVM</p>
        <h1 class="sg-section-title">Estimation quantitative</h1>
        <p class="sg-section-sub">Calibré sur 747 transactions DVF réelles DGFiP 2014–2025</p>
    </div>
    """), unsafe_allow_html=True)

    col_form, col_result = st.columns([1, 1], gap="large")

    with col_form:
        st.markdown('<div style="padding: 0 1.5rem;">', unsafe_allow_html=True)
        st.markdown('<div class="sg-input-section">', unsafe_allow_html=True)
        st.markdown('<div class="sg-input-title">Paramètres du bien</div>', unsafe_allow_html=True)

        commune = st.selectbox(
            "Commune",
            ["Ferney-Voltaire", "Gex", "Saint-Genis-Pouilly",
             "Prevessin-Moens", "Ornex", "Cessy", "Thoiry", "Sergy"],
            index=0
        )

        surface = st.number_input("Surface habitable (m²)", min_value=10, max_value=500, value=85, step=5)

        dpe = st.selectbox("Étiquette DPE", ["A", "B", "C", "D", "E", "F", "G"], index=2)

        prix_annonce = st.number_input(
            "Prix annoncé EUR (optionnel — pour Deal Alert)",
            min_value=0, max_value=5000000, value=0, step=10000
        )

        # MODIF 11/07/2026 : remplace la saisie manuelle lat/lon (qui forçait
        # a passer par Google Maps) par une recherche d'adresse. lat/lon
        # restent necessaires cote moteur (score Frontalier = vrais appels
        # OSRM + OSM Overpass dans api/main.py, PAS decoratif) -- on ne les
        # supprime donc pas, on evite juste de demander a l'utilisateur de
        # les connaitre par coeur. Geocodage via l'API officielle et
        # gratuite Base Adresse Nationale (BAN, data.gouv.fr).
        adresse_query = st.text_input(
            "Adresse du bien",
            placeholder="Ex : 12 rue de Genève, Ferney-Voltaire",
            help="Les coordonnées GPS sont calculées automatiquement à partir de l'adresse (source : Base Adresse Nationale, data.gouv.fr)."
        )

        lat, lon = 46.255, 6.117  # centre approx. Pays de Gex, utilisé tant qu'aucune adresse n'est confirmée
        source_coords = "centre_zone_par_defaut"

        if adresse_query and len(adresse_query.strip()) >= 3:
            suggestions = geocode_adresse(adresse_query)
            if suggestions:
                labels = [s[0] for s in suggestions]
                choix = st.selectbox("Confirmez l'adresse exacte", labels, key="adresse_choix")
                idx = labels.index(choix)
                _, lat, lon = suggestions[idx]
                source_coords = "adresse_ban"
            else:
                st.warning(
                    "Adresse non reconnue par la Base Adresse Nationale — "
                    "vérifiez l'orthographe, ou utilisez la saisie manuelle ci-dessous."
                )

        with st.expander("Coordonnées GPS manuelles (si l'adresse n'est pas reconnue)"):
            lat_manuel = st.number_input("Latitude", value=46.255, format="%.5f", step=0.0001, key="lat_manual")
            lon_manuel = st.number_input("Longitude", value=6.117, format="%.5f", step=0.0001, key="lon_manual")
            if st.checkbox("Utiliser ces coordonnées manuelles à la place de l'adresse"):
                lat, lon = lat_manuel, lon_manuel
                source_coords = "manuel"

        if source_coords == "centre_zone_par_defaut":
            st.caption("⚠ Aucune adresse confirmée — le calcul utilisera le centre approximatif du Pays de Gex (précision réduite pour le score Frontalier).")

        st.markdown('</div>', unsafe_allow_html=True)

        if st.button("CALCULER LE GEXSCORE", use_container_width=True):
            with st.spinner("Calcul en cours..."):
                try:
                    payload = {
                        "lat": lat,
                        "lon": lon,
                        "surface_m2": float(surface),
                        "dpe_note": dpe,
                        "commune": commune,
                        "zone_id": "gex_001"
                    }
                    if prix_annonce > 0:
                        payload["prix_annonce"] = float(prix_annonce)

                    r = requests.post(f"{API_BASE}/estimate", json=payload, timeout=10)
                    if r.status_code == 200:
                        st.session_state.result = r.json()
                    else:
                        st.error(f"Erreur API : {r.status_code}")
                except Exception as e:
                    st.error(f"Connexion impossible : {e}")

        st.markdown('</div>', unsafe_allow_html=True)

    with col_result:
        st.markdown('<div style="padding: 0 1.5rem;">', unsafe_allow_html=True)

        if st.session_state.result:
            res = st.session_state.result
            gs  = res.get("gexscore", {})
            avm = res.get("avm", {})
            deal = res.get("deal_alert")

            score = gs.get("score", 0)
            grade = gs.get("grade", "—")
            action = gs.get("action", "—")
            prix_estime = avm.get("prix_estime_eur", 0)
            prix_m2 = avm.get("prix_m2_estime", 0)
            prix_m2_dvf = avm.get("prix_m2_zone_median_dvf", 0)
            adj_dpe = avm.get("ajustement_dpe_pct", 0)

            # Score principal
            st.markdown(html_block(f"""
            <div class="sg-score-card" style="border-color: {'#4FA37A' if score >= 700 else '#2C4C38'};">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem;">
                    <div>
                        <div style="font-size:0.7rem;font-weight:600;letter-spacing:0.2em;text-transform:uppercase;color:#555;margin-bottom:0.5rem;">GexScore</div>
                        <div class="sg-score-big">{score:.0f}</div>
                        <div class="sg-grade">{grade}</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:0.7rem;color:#555;margin-bottom:0.3rem;">RECOMMANDATION</div>
                        <div style="font-size:0.9rem;color:#4FA37A;font-weight:500;">{action}</div>
                    </div>
                </div>

                <div>
                    <div class="sg-source-tag">
                        <div class="sg-source-dot"></div>
                        DVF réel DGFiP 2014–2025
                    </div>
                </div>

                <div class="sg-metric">
                    <span class="sg-metric-label">Prix estimé</span>
                    <span class="sg-metric-value gold">{prix_estime:,.0f} EUR</span>
                </div>
                <div class="sg-metric">
                    <span class="sg-metric-label">Prix / m²</span>
                    <span class="sg-metric-value">{prix_m2:,.0f} EUR/m²</span>
                </div>
                <div class="sg-metric">
                    <span class="sg-metric-label">Médiane DVF zone</span>
                    <span class="sg-metric-value">{prix_m2_dvf:,.0f} EUR/m²</span>
                </div>
                <div class="sg-metric">
                    <span class="sg-metric-label">Ajustement DPE {dpe}</span>
                    <span class="sg-metric-value {'positive' if adj_dpe >= 0 else 'negative'}">{adj_dpe:+.1f}%</span>
                </div>
            </div>
            """), unsafe_allow_html=True)

            # Deal Alert
            if deal:
                is_deal = deal.get("is_deal", False)
                disc = deal.get("discount_pct", 0)
                eco = deal.get("economie_potentielle_eur", 0)

                if is_deal:
                    st.markdown(html_block(f"""
                    <div class="sg-deal-box">
                        <div class="sg-deal-title">⚡ DEAL ALERT DÉTECTÉ</div>
                        <div style="font-family:'Playfair Display',serif;font-size:1.6rem;color:#F5F0E8;margin:0.3rem 0;">
                            {abs(disc):.1f}% sous le marché
                        </div>
                        <div style="font-size:0.85rem;color:#4FA37A;">
                            Économie potentielle : {eco:,.0f} EUR
                        </div>
                    </div>
                    """), unsafe_allow_html=True)
                else:
                    st.markdown(html_block(f"""
                    <div class="sg-deal-box nodeal">
                        <div class="sg-deal-title nodeal">ANALYSE PRIX ANNONCÉ</div>
                        <div style="font-size:0.9rem;color:#555;margin-top:0.3rem;">
                            {'Bien annoncé ' + str(abs(disc)) + '% au-dessus du marché DVF' if disc < 0 else 'Prix cohérent avec le marché DVF'}
                        </div>
                    </div>
                    """), unsafe_allow_html=True)

            # Dashboard — sauvegarde optionnelle du bien estimé (nécessite un compte)
            st.markdown('<div class="sg-input-section" style="margin-top:1rem;">', unsafe_allow_html=True)
            st.markdown('<div class="sg-input-title">Dashboard (optionnel)</div>', unsafe_allow_html=True)
            if not st.session_state.get("auth"):
                st.info("Connecte-toi ou crée un compte (barre latérale, section \"Compte\") pour enregistrer ce bien dans ton Dashboard.")
            else:
                if st.button("Enregistrer au dashboard", use_container_width=True, key="btn_save_dashboard"):
                    try:
                        save_payload = {
                            "commune": commune,
                            "adresse": adresse_query or None,
                            "lat": lat,
                            "lon": lon,
                            "surface_m2": surface,
                            "dpe_note": dpe,
                            "prix_estime_eur": prix_estime,
                            "prix_m2_estime": prix_m2,
                            "gexscore": score,
                            "grade": grade,
                            "prix_annonce_eur": prix_annonce if prix_annonce > 0 else None,
                            "is_deal": deal.get("is_deal") if deal else None,
                        }
                        auth_headers = {"Authorization": f"Bearer {st.session_state.auth['access_token']}"}
                        r_save = requests.post(f"{API_BASE}/estimations/sauvegarder", json=save_payload, headers=auth_headers, timeout=10)
                        if r_save.status_code == 200:
                            st.success("Bien enregistré dans le Dashboard.")
                        elif r_save.status_code == 401:
                            st.error("Session expirée — reconnecte-toi (barre latérale).")
                            st.session_state.auth = None
                        else:
                            st.error(f"Erreur d'enregistrement : {r_save.status_code} — {r_save.text[:200]}")
                    except Exception as e:
                        st.error(f"Connexion impossible : {e}")
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown(html_block("""
            <div class="sg-score-card" style="text-align:center;padding:4rem 2rem;">
                <div style="font-size:3rem;margin-bottom:1rem;opacity:0.2;">◈</div>
                <div style="font-size:0.8rem;color:#333;letter-spacing:0.1em;text-transform:uppercase;">
                    Remplissez le formulaire<br>et lancez le calcul
                </div>
            </div>
            """), unsafe_allow_html=True)

        st.markdown('</div>', unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : DASHBOARD B2B
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "dashboard":

    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;">
        <p class="sg-eyebrow">Dashboard B2B</p>
        <h1 class="sg-section-title">Vue agrégée — mes biens estimés</h1>
        <p class="sg-section-sub">Plan B2B Agences · classement par email de compte (MVP, voir avertissement ci-dessous)</p>
    </div>
    """), unsafe_allow_html=True)

    st.markdown('<div style="padding: 0 3rem 3rem;">', unsafe_allow_html=True)

    if not st.session_state.get("auth"):
        st.warning(
            "Connecte-toi ou crée un compte (barre latérale, section \"Compte\") pour accéder à ton Dashboard. "
            "Chaque compte ne voit que ses propres biens sauvegardés (authentification Supabase réelle, "
            "protégée par Row Level Security)."
        )
    else:
        st.caption(f"Connecté : {st.session_state.auth['email']}")

        if st.button("Charger mes biens", key="btn_load_dashboard"):
            try:
                auth_headers = {"Authorization": f"Bearer {st.session_state.auth['access_token']}"}
                r = requests.get(f"{API_BASE}/estimations", headers=auth_headers, timeout=10)
                if r.status_code == 200:
                    st.session_state.dashboard_data = r.json().get("estimations", [])
                elif r.status_code == 401:
                    st.error("Session expirée — reconnecte-toi (barre latérale).")
                    st.session_state.auth = None
                    st.session_state.dashboard_data = None
                else:
                    st.error(f"Erreur API : {r.status_code}")
                    st.session_state.dashboard_data = None
            except Exception as e:
                st.error(f"Connexion impossible : {e}")
                st.session_state.dashboard_data = None

    data = st.session_state.get("dashboard_data")

    if data:
        colonnes = ["created_at", "commune", "adresse", "surface_m2", "dpe_note",
                    "prix_estime_eur", "gexscore", "grade", "prix_annonce_eur", "is_deal"]
        entetes = ["Date", "Commune", "Adresse", "Surface m²", "DPE",
                   "Prix estimé EUR", "GexScore", "Grade", "Prix annoncé EUR", "Deal"]

        lignes_html = ""
        for row in data:
            cells = ""
            for c in colonnes:
                val = row.get(c)
                if val is None:
                    val = "—"
                elif c == "prix_estime_eur" or c == "prix_annonce_eur":
                    val = f"{val:,.0f}"
                elif c == "is_deal":
                    val = "Oui" if val else "Non"
                elif c == "created_at":
                    val = str(val)[:10]
                cells += f"<td>{val}</td>"
            lignes_html += f"<tr>{cells}</tr>"

        entetes_html = "".join(f"<th>{e}</th>" for e in entetes)

        st.markdown(
            f'<table class="sg-table"><thead><tr>{entetes_html}</tr></thead>'
            f'<tbody>{lignes_html}</tbody></table>',
            unsafe_allow_html=True
        )
        st.caption(f"{len(data)} bien(s) enregistré(s) pour {st.session_state.auth['email']}.")

        # Export CSV — stdlib uniquement (io/csv), pas de dépendance pandas ajoutée
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=colonnes, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow(row)
        st.download_button(
            "Exporter en CSV",
            buf.getvalue().encode("utf-8"),
            file_name="scoregex_estimations.csv",
            mime="text/csv"
        )
    elif st.session_state.get("auth"):
        st.info("Aucun bien chargé — clique sur \"Charger mes biens\", ou enregistre une estimation depuis la page Estimer.")

    st.markdown('</div>', unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : MARCHÉ
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "market":

    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;">
        <p class="sg-eyebrow">Données DVF Réelles</p>
        <h1 class="sg-section-title">Prix du marché — Pays de Gex</h1>
        <p class="sg-section-sub">Source : DGFiP Demandes de Valeurs Foncières 2014–2025 · Licence Ouverte 2.0</p>
    </div>
    """), unsafe_allow_html=True)

    # Fetch market data
    try:
        r = requests.get(f"{API_BASE}/prix-marche", timeout=8)
        if r.status_code == 200:
            data = r.json()
            communes_data = data.get("communes", {})

            # Table
            st.markdown(html_block("""
            <div style="padding: 0 3rem;">
                <table class="sg-table">
                    <thead>
                        <tr>
                            <th>Commune</th>
                            <th>Transactions</th>
                            <th>Prix médian DVF</th>
                        </tr>
                    </thead>
                    <tbody>
            """), unsafe_allow_html=True)

            rows = ""
            for code, info in sorted(communes_data.items(), key=lambda x: -x[1]["prix_m2_median"]):
                rows += f"""
                    <tr>
                        <td>{info['commune']}</td>
                        <td>{info['nb_transactions']}</td>
                        <td>{info['prix_m2_median']:,.0f} EUR/m²</td>
                    </tr>
                """

            st.markdown(rows + "</tbody></table></div>", unsafe_allow_html=True)

            # Key insight
            st.markdown(html_block("""
            <div style="padding: 3rem;">
                <div class="sg-score-card">
                    <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#4FA37A;margin-bottom:1rem;">
                        Insight ScoreGex
                    </div>
                    <div style="font-size:1rem;color:#F5F0E8;line-height:1.8;">
                        Prévessin-Moëns affiche le prix médian le plus élevé (5 543 EUR/m²),
                        porté par la proximité CERN et la faible distance au poste frontière de Meyrin.
                        Sergy reste le marché le plus accessible (3 480 EUR/m²) avec une connectivité
                        Genève supérieure à 30 minutes.
                    </div>
                </div>
            </div>
            """), unsafe_allow_html=True)
        else:
            st.error("API non disponible")
    except Exception as e:
        st.error(f"Erreur : {e}")


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : TARIFS
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "pricing":

    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 2rem;text-align:center;">
        <p class="sg-eyebrow" style="justify-content:center;">Accès à la plateforme</p>
        <h1 class="sg-section-title" style="text-align:center;">Tarification transparente</h1>
        <p style="color:#555;font-size:0.9rem;">Sans engagement. Sans frais cachés.</p>
    </div>
    """), unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(html_block("""
        <div class="sg-plan">
            <div class="sg-plan-name">Découverte</div>
            <div class="sg-plan-price">0 <span>EUR</span></div>
            <div class="sg-plan-desc">Pour tester la plateforme avant de vous engager.</div>
            <div class="sg-plan-feature">3 estimations gratuites</div>
            <div class="sg-plan-feature">Score GexScore complet</div>
            <div class="sg-plan-feature">Prix marché DVF réel</div>
            <div class="sg-plan-feature">Deal Alert basique</div>
        </div>
        """), unsafe_allow_html=True)
        if st.button("Essayer gratuitement", use_container_width=True, key="pricing_free_btn"):
            st.session_state.page = "estimate"
            st.rerun()

    with col2:
        st.markdown(html_block("""
        <div class="sg-plan featured">
            <div class="sg-plan-badge">Recommandé</div>
            <div class="sg-plan-name">Frontalier Pro</div>
            <div class="sg-plan-price">99 <span>EUR / mois</span></div>
            <div class="sg-plan-desc">Pour les frontaliers actifs sur le marché Gex–Genève.</div>
            <div class="sg-plan-feature">Estimations illimitées</div>
            <div class="sg-plan-feature">Score GexScore + ESG</div>
            <div class="sg-plan-feature">Deal Alert temps réel</div>
            <div class="sg-plan-feature">Rapport PDF téléchargeable</div>
            <div class="sg-plan-feature">Historique 12 mois</div>
            <div class="sg-plan-feature">Support email prioritaire</div>
        </div>
        """), unsafe_allow_html=True)
        # MODIF 11/07/2026 : bouton de paiement reel (Stripe Payment Link).
        # Voir STRIPE_LINK_FRONTALIER_PRO tout en haut du fichier -- a
        # remplacer par le vrai lien avant mise en prod publique.
        st.link_button("Commencer pour 99€/mois", STRIPE_LINK_FRONTALIER_PRO, use_container_width=True)

    with col3:
        st.markdown(html_block("""
        <div class="sg-plan">
            <div class="sg-plan-name">B2B Agences</div>
            <div class="sg-plan-price">490 <span>EUR / mois</span></div>
            <div class="sg-plan-desc">Pour agences immobilières, notaires et conseillers patrimoniaux.</div>
            <div class="sg-plan-feature">Accès API REST illimité</div>
            <div class="sg-plan-feature">Batch 100 biens / jour</div>
            <div class="sg-plan-feature">Rapport PDF branded</div>
            <div class="sg-plan-feature">Score ESG SFDR Art. 8</div>
            <div class="sg-plan-feature">SLA 99.5% uptime</div>
            <div class="sg-plan-feature">Onboarding dédié</div>
        </div>
        """), unsafe_allow_html=True)
        st.link_button("Contactez-nous", "mailto:contact@scoregex.com?subject=ScoreGex%20B2B%20Agences", use_container_width=True)

    st.markdown(html_block("""
    <div style="text-align:center;padding:2rem;margin-top:1rem;">
        <p style="color:#333;font-size:0.8rem;margin-bottom:0.5rem;">
            Pour banques et family offices : tarification sur mesure à partir de 1 990 EUR/mois
        </p>
        <p style="color:#333;font-size:0.78rem;">contact@scoregex.com</p>
    </div>
    """), unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : MENTIONS LEGALES
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "mentions-legales":
    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;max-width:820px;margin:0 auto;">
        <p class="sg-eyebrow">Informations légales</p>
        <h1 class="sg-section-title">Mentions légales</h1>
    </div>
    """), unsafe_allow_html=True)

    st.markdown('<div style="padding: 0 3rem 3rem;max-width:820px;margin:0 auto;">', unsafe_allow_html=True)
    st.markdown(html_block("""
    <div class="sg-legal-disclaimer">
        Brouillon rédigé par l'assistant IA de Steelldy à partir des informations disponibles.
        Les champs marqués [À COMPLÉTER] doivent être renseignés avant publication. Ce texte n'a
        pas été relu par un avocat — une relecture juridique est recommandée avant mise en ligne,
        en particulier pour la conformité RGPD.
    </div>
    <div class="sg-legal-body">

    <h3>Éditeur du site</h3>
    Le site ScoreGex (scoregex.com) est édité par <strong>STEELLDY</strong>, société par actions
    simplifiée (SAS).<br>
    Siège social : 139, rue du Commerce, 01170 Gex, France<br>
    SIREN : 838 252 260<br>
    SIRET : 838 252 260 000 24<br>
    RCS : Chalon-sur-Saône B 838 252 260<br>
    N° TVA intracommunautaire : FR73 838252260<br>
    Capital social : 6 000 euros<br>
    Contact : contact@scoregex.com

    <h3>Directeur de la publication</h3>
    Oleg Turceac

    <h3>Hébergement</h3>
    L'application (interface utilisateur) est hébergée par <strong>Streamlit Community Cloud</strong>,
    opéré par Snowflake Inc., 106 East Babcock Street, Suite 3A, Bozeman, MT 59715, États-Unis.<br><br>
    L'API de calcul est hébergée par <strong>Vercel Inc.</strong>, 340 S Lemon Ave #4133,
    Walnut, CA 91789, États-Unis.<br><br>
    La base de données est hébergée par <strong>Supabase Inc.</strong>, sur une infrastructure
    Amazon Web Services localisée en Irlande (Union européenne, région eu-west-1).

    <h3>Propriété intellectuelle</h3>
    L'ensemble des contenus, algorithmes, marques et éléments graphiques présents sur ScoreGex
    sont la propriété exclusive de Steelldy SAS, sauf mention contraire. Toute reproduction, même
    partielle, sans autorisation préalable est interdite.

    <h3>Nature du service</h3>
    ScoreGex fournit des estimations immobilières indicatives (marge ±8%) issues d'un modèle
    quantitatif (AVM). Ces estimations ne constituent ni une expertise immobilière officielle, ni
    un conseil en investissement, et ne sauraient engager la responsabilité de Steelldy SAS quant
    aux décisions prises sur leur base.

    <h3>Contact</h3>
    Pour toute question relative aux présentes mentions légales : contact@scoregex.com

    </div>
    """), unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : POLITIQUE DE CONFIDENTIALITE
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "confidentialite":
    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;max-width:820px;margin:0 auto;">
        <p class="sg-eyebrow">RGPD</p>
        <h1 class="sg-section-title">Politique de confidentialité</h1>
    </div>
    """), unsafe_allow_html=True)

    st.markdown('<div style="padding: 0 3rem 3rem;max-width:820px;margin:0 auto;">', unsafe_allow_html=True)
    st.markdown(html_block("""
    <div class="sg-legal-disclaimer">
        Brouillon rédigé par l'assistant IA de Steelldy. Les champs [À COMPLÉTER] doivent être
        renseignés. Une relecture par un juriste ou DPO est recommandée avant publication — le
        RGPD prévoit des sanctions significatives en cas de non-conformité.
    </div>
    <div class="sg-legal-body">

    <h3>Responsable de traitement</h3>
    STEELLDY SAS, 139, rue du Commerce, 01170 Gex, France (SIREN 838 252 260), est responsable du
    traitement des données personnelles collectées sur ScoreGex. Contact : contact@scoregex.com

    <h3>Données collectées</h3>
    — <strong>Compte utilisateur</strong> : adresse email et mot de passe. Le mot de passe n'est
    jamais stocké en clair : il est géré et haché par Supabase Auth, notre prestataire
    d'authentification.<br>
    — <strong>Biens sauvegardés</strong> (si vous utilisez le Dashboard) : commune, adresse,
    surface, DPE, prix estimé et paramètres associés du bien que vous choisissez d'enregistrer.<br>
    — <strong>Données techniques</strong> : logs de connexion à des fins de sécurité et de bon
    fonctionnement du service.<br>
    ScoreGex ne collecte aucune donnée de géolocalisation en continu, ni aucune donnée bancaire
    (le paiement des abonnements est traité directement par Stripe, qui ne transmet jamais vos
    coordonnées bancaires à Steelldy SAS).

    <h3>Finalités du traitement</h3>
    Fourniture du service d'estimation immobilière, gestion des comptes utilisateurs et des
    abonnements, amélioration continue du service, sécurité de la plateforme.

    <h3>Base légale</h3>
    Exécution du contrat (conditions générales d'utilisation) pour la fourniture du service ;
    intérêt légitime pour l'amélioration et la sécurité du service.

    <h3>Destinataires des données</h3>
    Vos données ne sont jamais vendues ni louées à des tiers. Elles sont accessibles à Steelldy
    SAS et à ses sous-traitants techniques strictement nécessaires au fonctionnement du service :
    Supabase Inc. (base de données, hébergement UE), Vercel Inc. (hébergement de l'API), Snowflake
    Inc. / Streamlit (hébergement de l'interface), et Stripe Inc. (paiement des abonnements).

    <h3>Durée de conservation</h3>
    Les données de compte sont conservées pendant la durée de vie du compte, puis [À COMPLÉTER —
    durée après suppression, ex. 30 jours] après une demande de suppression, sauf obligation légale
    de conservation plus longue.

    <h3>Vos droits (RGPD)</h3>
    Conformément au Règlement Général sur la Protection des Données, vous disposez d'un droit
    d'accès, de rectification, d'effacement, de portabilité, de limitation et d'opposition
    concernant vos données personnelles. Pour exercer ces droits : contact@scoregex.com.<br><br>
    Vous disposez également du droit d'introduire une réclamation auprès de la CNIL
    (www.cnil.fr) si vous estimez que vos droits ne sont pas respectés.

    <h3>Sécurité</h3>
    L'accès à vos données sauvegardées est protégé par authentification (Supabase Auth) et par des
    règles de sécurité au niveau de la base de données (Row Level Security) garantissant que
    chaque utilisateur ne peut accéder qu'à ses propres données. Les échanges avec le site sont
    chiffrés (HTTPS).

    </div>
    """), unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : CGU
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "cgu":
    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;max-width:820px;margin:0 auto;">
        <p class="sg-eyebrow">Conditions générales</p>
        <h1 class="sg-section-title">Conditions Générales d'Utilisation</h1>
    </div>
    """), unsafe_allow_html=True)

    st.markdown('<div style="padding: 0 3rem 3rem;max-width:820px;margin:0 auto;">', unsafe_allow_html=True)
    st.markdown(html_block("""
    <div class="sg-legal-disclaimer">
        Brouillon rédigé par l'assistant IA de Steelldy. Les champs [À COMPLÉTER] doivent être
        renseignés. Une relecture par un avocat est recommandée avant publication, notamment sur
        la clause de responsabilité et le droit applicable.
    </div>
    <div class="sg-legal-body">

    <h3>Article 1 — Objet</h3>
    Les présentes Conditions Générales d'Utilisation (CGU) régissent l'accès et l'utilisation du
    service ScoreGex, plateforme d'estimation immobilière quantitative pour le Pays de Gex, éditée
    par Steelldy SAS.

    <h3>Article 2 — Accès au service</h3>
    ScoreGex propose trois formules d'accès, détaillées sur la page Tarifs : une offre Découverte
    gratuite avec un nombre limité d'estimations, une offre Frontalier Pro par abonnement mensuel,
    et une offre B2B Agences par abonnement mensuel avec accès API. Les tarifs en vigueur sont ceux
    affichés sur la page Tarifs au moment de la souscription.

    <h3>Article 3 — Compte utilisateur</h3>
    L'accès au Dashboard nécessite la création d'un compte (email et mot de passe). L'utilisateur
    est seul responsable de la confidentialité de ses identifiants et de toute activité effectuée
    depuis son compte.

    <h3>Article 4 — Nature des estimations</h3>
    Les estimations fournies par ScoreGex sont calculées par un modèle quantitatif (AVM) calibré
    sur des données réelles de transactions (DVF, DGFiP), avec une marge d'erreur indicative de
    ±8%. Ces estimations constituent une aide à la décision et <strong>ne remplacent en aucun cas
    l'expertise d'un professionnel de l'immobilier</strong>, ni une évaluation notariale ou
    bancaire. Steelldy SAS ne saurait être tenue responsable des décisions prises sur la base de
    ces estimations.

    <h3>Article 5 — Propriété intellectuelle</h3>
    Les algorithmes, la marque ScoreGex, et l'ensemble des contenus du site sont la propriété
    exclusive de Steelldy SAS. Toute reproduction ou utilisation non autorisée est interdite.

    <h3>Article 6 — Résiliation</h3>
    L'utilisateur peut demander la suppression de son compte et de ses données à tout moment en
    contactant contact@scoregex.com. La résiliation d'un abonnement payant s'effectue [À
    COMPLÉTER — modalités précises selon la configuration Stripe choisie].

    <h3>Article 7 — Responsabilité</h3>
    Steelldy SAS s'efforce d'assurer un service disponible et fiable, sans garantie de continuité
    absolue. La responsabilité de Steelldy SAS ne saurait être engagée en cas d'indisponibilité
    temporaire du service, d'erreur dans les données sources (DVF, DGFiP) sur lesquelles s'appuie
    le modèle, ou de décision prise par l'utilisateur sur la base d'une estimation.

    <h3>Article 8 — Droit applicable</h3>
    Les présentes CGU sont soumises au droit français. Tout litige relève de la compétence des
    tribunaux de Chalon-sur-Saône (ville d'immatriculation RCS de STEELLDY), sauf disposition
    légale contraire applicable aux consommateurs, qui bénéficient des règles de compétence
    territoriale protectrices prévues par le droit de la consommation.

    <h3>Article 9 — Modification des CGU</h3>
    Steelldy SAS se réserve le droit de modifier les présentes CGU à tout moment. Les utilisateurs
    seront informés de toute modification substantielle par email ou notification sur le site.

    </div>
    """), unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(html_block("""
<div class="sg-divider"></div>
<div class="sg-footer">
    <div class="sg-footer-logo">ScoreGex</div>
</div>
"""), unsafe_allow_html=True)

# MODIF 12/07/2026 : les 4 liens ci-dessous etaient des <span> HTML statiques
# sans destination -- meme bug que l'ancienne top-nav. Remplaces par de vrais
# boutons/lien. "API docs" pointe vers la doc Swagger auto-generee par
# FastAPI (deja live sur l'API, /docs -- rien a construire, juste a lier).
with st.container(key="footernav"):
    fl1, fl2, fl3, fl4, fl5 = st.columns([1, 1, 1, 1, 1])
    with fl2:
        if st.button("Mentions légales", key="footer_mentions", use_container_width=True):
            st.session_state.page = "mentions-legales"
            st.rerun()
    with fl3:
        if st.button("Politique de confidentialité", key="footer_confidentialite", use_container_width=True):
            st.session_state.page = "confidentialite"
            st.rerun()
    with fl4:
        if st.button("CGU", key="footer_cgu", use_container_width=True):
            st.session_state.page = "cgu"
            st.rerun()
    with fl5:
        st.link_button("API docs", f"{API_BASE}/docs", use_container_width=True)

st.markdown(html_block("""
<div class="sg-footer" style="padding-top:0;">
    <div class="sg-footer-copy">
        © 2026 Steelldy SAS — Pays de Gex, France<br>
        Données : DVF DGFiP Open Data, Licence Ouverte 2.0<br>
        Estimations indicatives AVM ±8%. Ne remplace pas l'expertise d'un professionnel immobilier.
    </div>
</div>
"""), unsafe_allow_html=True)
