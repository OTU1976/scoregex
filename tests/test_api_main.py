"""
tests/test_api_main.py — Tests de l'API (api/main.py).

AJOUTÉ le 22/07/2026 (intégration Sentry). Deux objectifs :
1. Vérifier que api/main.py s'importe sans erreur (smoke test) — la CI
   installe maintenant api/requirements.txt en plus de pytest/numpy pour
   rendre ce test possible (voir .github/workflows/ci.yml).
2. Verrouiller le comportement du scrubber Sentry (_sentry_before_send) :
   aucun secret d'authentification (JWT Supabase, cookie, clé API) ne doit
   jamais être transmis à Sentry, même si send_default_pii venait à être
   réactivé par erreur un jour.

Lancer localement :  pytest tests/ -v
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# SENTRY_DSN volontairement absent/vide pendant les tests : api.main doit
# s'importer proprement dans les deux cas (avec ou sans DSN configuré) —
# voir la logique `if SENTRY_DSN:` dans api/main.py.
os.environ.pop("SENTRY_DSN", None)

from api.main import _sentry_before_send  # noqa: E402


def test_api_main_simporte_sans_erreur():
    """Smoke test : le module api.main se charge sans exception, même sans
    SENTRY_DSN configuré (cas normal en environnement de dev/CI)."""
    import api.main as m
    assert m.VERSION == "3.14.0"


def test_sentry_scrub_retire_authorization():
    """Le header Authorization (JWT Supabase réel sur /estimations/*) ne
    doit JAMAIS être transmis tel quel à Sentry."""
    event = {"request": {"headers": {"Authorization": "Bearer eyJ...secret", "X-Custom": "ok"}}}
    result = _sentry_before_send(event, {})
    assert result["request"]["headers"]["Authorization"] == "[retire_avant_envoi_sentry]"
    assert result["request"]["headers"]["X-Custom"] == "ok"   # les headers non sensibles restent intacts


def test_sentry_scrub_retire_cookie_et_apikey():
    """Cookie et apikey (clé Supabase) doivent aussi être retirés."""
    event = {"request": {"headers": {"Cookie": "session=abc", "apikey": "supabase-anon-key-reelle"}}}
    result = _sentry_before_send(event, {})
    assert result["request"]["headers"]["Cookie"] == "[retire_avant_envoi_sentry]"
    assert result["request"]["headers"]["apikey"] == "[retire_avant_envoi_sentry]"


def test_sentry_scrub_ne_plante_pas_si_pas_de_request():
    """Un évènement sans clé 'request' (ex: erreur hors requête HTTP) ne
    doit jamais faire planter le hook before_send lui-même."""
    event = {"message": "erreur generique sans contexte HTTP"}
    result = _sentry_before_send(event, {})
    assert result == event
