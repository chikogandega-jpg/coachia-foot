"""
CoachIA Foot — Backend API
===========================
Reçoit les données du formulaire, appelle le pipeline de génération,
et renvoie le lien du PDF.

Dépendances : flask, anthropic, psycopg2 (ou sqlite3 pour un MVP local)
"""

from flask import Flask, request, jsonify, send_from_directory
from datetime import date
import sqlite3
import os

from pipeline_seance import generer_contenu_seance, generer_pdf  # réutilise le script précédent

app = Flask(__name__, static_folder="static")
DB_PATH = "coachia.db"


# ---------------------------------------------------------------------------
# Initialisation de la base (à lancer une fois)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        logo_url TEXT,
        couleur_primaire TEXT DEFAULT '#0B2545',
        couleur_secondaire TEXT DEFAULT '#2E8B57',
        formation TEXT DEFAULT '4-3-3',
        formule TEXT DEFAULT 'solo',          -- solo | club | ligue
        date_debut_essai DATE,
        actif BOOLEAN DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS seances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        club_id INTEGER NOT NULL REFERENCES clubs(id),
        date_seance DATE NOT NULL,
        effectif_dispo INTEGER,
        adversaire TEXT,
        focus TEXT,
        exercices_json TEXT,        -- contenu généré par l'IA, pour la mémoire du club
        pdf_path TEXT,
        cree_le TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_seances_club ON seances(club_id, date_seance);
    """)
    conn.commit()
    conn.close()


def get_historique_exercices(club_id: int, limite: int = 15) -> list:
    """Récupère les titres d'exercices récents pour éviter les répétitions."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT exercices_json FROM seances WHERE club_id = ? ORDER BY date_seance DESC LIMIT ?",
        (club_id, limite)
    ).fetchall()
    conn.close()

    import json
    titres = []
    for (exercices_json,) in rows:
        if exercices_json:
            for ex in json.loads(exercices_json):
                titres.append(ex.get("titre"))
    return titres


def get_ou_creer_club(email: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    club = conn.execute("SELECT * FROM clubs WHERE email = ?", (email,)).fetchone()

    if club is None:
        # Club de démo créé automatiquement au premier essai — à remplacer
        # par un vrai formulaire d'inscription une fois les premiers clients validés.
        conn.execute(
            """INSERT INTO clubs (nom, email, formation, couleur_primaire, couleur_secondaire)
               VALUES (?, ?, ?, ?, ?)""",
            ("Diogountouro FC", email, "4-3-3", "#0B2545", "#2E8B57")
        )
        conn.commit()
        club = conn.execute("SELECT * FROM clubs WHERE email = ?", (email,)).fetchone()

    conn.close()
    return dict(club)


# ---------------------------------------------------------------------------
# Route principale appelée par le formulaire
# ---------------------------------------------------------------------------

@app.route("/")
def accueil():
    return send_from_directory("static", "formulaire.html")


@app.route("/vente")
def page_vente():
    return send_from_directory("static", "page-vente.html")


@app.route("/api/generer-seance", methods=["POST"])
def api_generer_seance():
    data = request.get_json()

    # En prod : récupérer le club via la session/l'auth, pas un champ du formulaire
    email_club = data.get("email", "demo@diogountourofc.fr")
    club = get_ou_creer_club(email_club)

    contexte_semaine = {
        "effectif_dispo": int(data["effectif_dispo"]),
        "duree_totale_min": int(data["duree_totale_min"]),
        "date_seance": data.get("date_seance") or str(date.today()),
        "adversaire": data.get("adversaire") or "non précisé",
    }

    historique = get_historique_exercices(club["id"])

    contenu = generer_contenu_seance(club, contexte_semaine, historique)
    pdf_path = generer_pdf(club, contenu, contexte_semaine)

    # Sauvegarde pour la mémoire du club
    import json
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO seances (club_id, date_seance, effectif_dispo, adversaire, focus, exercices_json, pdf_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (club["id"], contexte_semaine["date_seance"], contexte_semaine["effectif_dispo"],
         contexte_semaine["adversaire"], data.get("focus"), json.dumps(contenu["exercices"]), str(pdf_path))
    )
    conn.commit()
    conn.close()

    return jsonify({"pdf_url": f"/pdf/{pdf_path.name}"})


@app.route("/pdf/<filename>")
def servir_pdf(filename):
    return send_from_directory("output", filename)


# Initialisation de la base au chargement du module — nécessaire car gunicorn
# n'exécute jamais le bloc `if __name__ == "__main__"` ci-dessous.
init_db()
os.makedirs("output", exist_ok=True)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
