"""
ScoreGex — Plateforme Quantitative d'Évaluation Immobilière
Pays de Gex — Genève Frontalier
Design : noir #0A0A0A + or #C9A961 + blanc cassé #F5F0E8
"""

import streamlit as st
import requests
import json
import time

# ── Config page ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ScoreGex — Intelligence Immobilière Quantitative",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

API_BASE = "https://scoregex.vercel.app"

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
    background-color: #0A0A0A !important;
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
    border-bottom: 1px solid #1A1A1A;
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
    color: #C9A961;
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
    background: #C9A961;
    color: #0A0A0A;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 0.3rem 0.8rem;
    border-radius: 2px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
}

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
    background: radial-gradient(ellipse 60% 50% at 50% 30%, rgba(201,169,97,0.06) 0%, transparent 70%);
    pointer-events: none;
}
.sg-eyebrow {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    color: #C9A961;
    margin-bottom: 1.5rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
}
.sg-eyebrow::before, .sg-eyebrow::after {
    content: '';
    width: 40px;
    height: 1px;
    background: #C9A961;
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
.sg-h1 em { color: #C9A961; font-style: normal; }
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
    border-top: 1px solid #1A1A1A;
}
.sg-stat { text-align: center; }
.sg-stat-num {
    font-family: 'Playfair Display', serif;
    font-size: 2.2rem;
    font-weight: 700;
    color: #C9A961;
    display: block;
}
.sg-stat-lbl { font-size: 0.75rem; color: #555; letter-spacing: 0.1em; text-transform: uppercase; }

/* Buttons */
.sg-btn-primary {
    background: #C9A961;
    color: #0A0A0A;
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
    border: 1px solid #2A2A2A;
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
    background: #0D0D0D;
    border-top: 1px solid #1A1A1A;
    border-bottom: 1px solid #1A1A1A;
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
    background: #111;
    border: 1px solid #1D1D1D;
    border-radius: 4px;
    padding: 2rem;
    margin-bottom: 1rem;
}
.sg-score-big {
    font-family: 'Playfair Display', serif;
    font-size: 5rem;
    font-weight: 700;
    color: #C9A961;
    line-height: 1;
}
.sg-grade {
    display: inline-block;
    background: rgba(201,169,97,0.15);
    border: 1px solid rgba(201,169,97,0.3);
    color: #C9A961;
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
    border-bottom: 1px solid #1A1A1A;
    font-size: 0.9rem;
}
.sg-metric-label { color: #555; }
.sg-metric-value { color: #F5F0E8; font-weight: 500; }
.sg-metric-value.positive { color: #4CAF50; }
.sg-metric-value.negative { color: #EF5350; }
.sg-metric-value.gold { color: #C9A961; }

/* Deal alert */
.sg-deal-box {
    background: rgba(201,169,97,0.08);
    border: 1px solid rgba(201,169,97,0.25);
    border-radius: 4px;
    padding: 1.25rem 1.5rem;
    margin-top: 1rem;
}
.sg-deal-box.nodeal {
    background: rgba(100,100,100,0.06);
    border-color: #2A2A2A;
}
.sg-deal-title {
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #C9A961;
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
    background: #111 !important;
    border-color: #2A2A2A !important;
    color: #F5F0E8 !important;
}
.stNumberInput input {
    background: #111 !important;
    border-color: #2A2A2A !important;
    color: #F5F0E8 !important;
}
.stButton button {
    background: #C9A961 !important;
    color: #0A0A0A !important;
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
    background: #111;
    border: 1px solid #1D1D1D;
    border-radius: 4px;
    padding: 2rem;
    position: relative;
}
.sg-plan.featured {
    border-color: #C9A961;
    background: #0F0F0F;
}
.sg-plan-badge {
    position: absolute;
    top: -1px;
    left: 50%;
    transform: translateX(-50%);
    background: #C9A961;
    color: #0A0A0A;
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
.sg-plan-feature { font-size: 0.82rem; color: #777; padding: 0.4rem 0; border-bottom: 1px solid #1A1A1A; display: flex; gap: 0.5rem; align-items: center; }
.sg-plan-feature::before { content: '—'; color: #C9A961; font-size: 0.7rem; }

/* Data source tag */
.sg-source-tag {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: #111;
    border: 1px solid #1D1D1D;
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
    background: #0D0D0D;
    border: 1px solid #1D1D1D;
    border-radius: 4px;
    padding: 2rem;
}
.sg-input-title {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: #C9A961;
    margin-bottom: 1.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid #1D1D1D;
}

/* Footer */
.sg-footer {
    background: #050505;
    border-top: 1px solid #1A1A1A;
    padding: 3rem;
    text-align: center;
}
.sg-footer-logo { 
    font-family: 'Playfair Display', serif;
    font-size: 1.2rem;
    color: #C9A961;
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
    background: linear-gradient(90deg, transparent, #C9A961, transparent);
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
    border-bottom: 1px solid #1D1D1D;
    text-align: left;
    font-weight: 500;
}
.sg-table td {
    padding: 0.85rem 1rem;
    border-bottom: 1px solid #141414;
    color: #C0B89A;
}
.sg-table td:first-child { color: #F5F0E8; font-weight: 500; }
.sg-table td:last-child { color: #C9A961; font-family: 'Courier New', monospace; font-size: 1rem; }
.sg-table tr:hover td { background: rgba(201,169,97,0.03); }
</style>
"""), unsafe_allow_html=True)

# ── Navigation ────────────────────────────────────────────────────────────────
st.markdown(html_block("""
<div class="sg-nav">
    <div class="sg-logo">Score<span>Gex</span></div>
    <div class="sg-nav-links">
        <span class="sg-nav-link">Estimer</span>
        <span class="sg-nav-link">Marché</span>
        <span class="sg-nav-link">Tarifs</span>
        <span class="sg-badge">Beta</span>
    </div>
</div>
"""), unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "home"
if "result" not in st.session_state:
    st.session_state.result = None

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
    
    st.markdown("---")
    st.markdown(html_block("""
    <div style="font-size:0.72rem;color:#333;line-height:1.6;">
    Données DVF réelles<br>
    DGFiP 2014–2025<br>
    658 transactions calibrées<br>
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
                <span class="sg-stat-num">658</span>
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
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#C9A961;margin-bottom:1rem;">01 — AVM Quantitatif</div>
            <div style="font-size:1.5rem;font-family:'Playfair Display',serif;color:#F5F0E8;margin-bottom:0.75rem;">Prix de Marché Réel</div>
            <div style="font-size:0.85rem;color:#555;line-height:1.7;">
                Calibré sur les transactions DVF réelles. Ajusté par DPE, surface, commune et position frontalière. 
                Pas une estimation générique — un calcul sur votre bien précis.
            </div>
        </div>
        """), unsafe_allow_html=True)
    
    with col2:
        st.markdown(html_block("""
        <div class="sg-score-card" style="border-color:#C9A961;">
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#C9A961;margin-bottom:1rem;">02 — Score GexScore</div>
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
            <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#C9A961;margin-bottom:1rem;">03 — Deal Alert</div>
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
    
    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_b:
        if st.button("ESTIMER MON BIEN MAINTENANT", use_container_width=True):
            st.session_state.page = "estimate"
            st.rerun()


# ════════════════════════════════════════════════════════════════════════════════
# PAGE : ESTIMATION
# ════════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "estimate":
    
    st.markdown(html_block("""
    <div style="padding: 3rem 3rem 1rem;">
        <p class="sg-eyebrow">Moteur AVM</p>
        <h1 class="sg-section-title">Estimation quantitative</h1>
        <p class="sg-section-sub">Calibré sur 658 transactions DVF réelles DGFiP 2014–2025</p>
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
        
        lat = st.number_input("Latitude", value=46.255, format="%.4f", step=0.001)
        lon = st.number_input("Longitude", value=6.117, format="%.4f", step=0.001)
        
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
            <div class="sg-score-card" style="border-color: {'#C9A961' if score >= 700 else '#2A2A2A'};">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem;">
                    <div>
                        <div style="font-size:0.7rem;font-weight:600;letter-spacing:0.2em;text-transform:uppercase;color:#555;margin-bottom:0.5rem;">GexScore</div>
                        <div class="sg-score-big">{score:.0f}</div>
                        <div class="sg-grade">{grade}</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:0.7rem;color:#555;margin-bottom:0.3rem;">RECOMMANDATION</div>
                        <div style="font-size:0.9rem;color:#C9A961;font-weight:500;">{action}</div>
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
                        <div style="font-size:0.85rem;color:#C9A961;">
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
                    <div style="font-size:0.7rem;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:#C9A961;margin-bottom:1rem;">
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

    st.markdown(html_block("""
    <div style="text-align:center;padding:2rem;margin-top:1rem;">
        <p style="color:#333;font-size:0.8rem;margin-bottom:0.5rem;">
            Pour banques et family offices : tarification sur mesure à partir de 1 990 EUR/mois
        </p>
        <p style="color:#333;font-size:0.78rem;">contact@scoregex.com</p>
    </div>
    """), unsafe_allow_html=True)


# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(html_block("""
<div class="sg-divider"></div>
<div class="sg-footer">
    <div class="sg-footer-logo">ScoreGex</div>
    <div class="sg-footer-links">
        <span class="sg-footer-link">Mentions légales</span>
        <span class="sg-footer-link">Politique de confidentialité</span>
        <span class="sg-footer-link">CGU</span>
        <span class="sg-footer-link">API docs</span>
    </div>
    <div class="sg-footer-copy">
        © 2026 Steelldy SAS — Pays de Gex, France<br>
        Données : DVF DGFiP Open Data, Licence Ouverte 2.0<br>
        Estimations indicatives AVM ±8%. Ne remplace pas l'expertise d'un professionnel immobilier.
    </div>
</div>
"""), unsafe_allow_html=True)
