"""
CoachIA Foot — Backend API
===========================
Reçoit les données du formulaire, appelle le pipeline de génération,
et renvoie le lien du PDF.

Dépendances : flask, anthropic, psycopg2 (ou sqlite3 pour un MVP local)
"""

from flask import Flask, request, jsonify, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date
import sqlite3
import os

from pipeline_seance import generer_contenu_seance, generer_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-moi-en-prod")
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
        mot_de_passe_hash TEXT NOT NULL,
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


def creer_club(nom: str, email: str, mot_de_passe: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    hash_mdp = generate_password_hash(mot_de_passe)
    conn.execute(
        """INSERT INTO clubs (nom, email, mot_de_passe_hash, formation, couleur_primaire, couleur_secondaire)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (nom, email, hash_mdp, "4-3-3", "#0B2545", "#2E8B57")
    )
    conn.commit()
    club = conn.execute("SELECT * FROM clubs WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(club)


def verifier_identifiants(email: str, mot_de_passe: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    club = conn.execute("SELECT * FROM clubs WHERE email = ?", (email,)).fetchone()
    conn.close()
    if club and check_password_hash(club["mot_de_passe_hash"], mot_de_passe):
        return dict(club)
    return None


def get_club_connecte() -> dict | None:
    club_id = session.get("club_id")
    if not club_id:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    club = conn.execute("SELECT * FROM clubs WHERE id = ?", (club_id,)).fetchone()
    conn.close()
    return dict(club) if club else None


# ---------------------------------------------------------------------------
# Route principale appelée par le formulaire
# ---------------------------------------------------------------------------

@app.route("/")
def accueil():
    if not get_club_connecte():
        return redirect("/connexion")
    return send_from_directory(".", "formulaire.html")


@app.route("/vente")
def page_vente():
    return send_from_directory(".", "page-vente.html")


@app.route("/inscription")
def page_inscription():
    return send_from_directory(".", "inscription.html")


@app.route("/connexion")
def page_connexion():
    return send_from_directory(".", "connexion.html")


@app.route("/api/inscription", methods=["POST"])
def api_inscription():
    data = request.get_json()
    nom = data.get("nom", "").strip()
    email = data.get("email", "").strip().lower()
    mot_de_passe = data.get("mot_de_passe", "")

    if not nom or not email or len(mot_de_passe) < 6:
        return jsonify({"erreur": "Champs invalides — mot de passe 6 caractères minimum"}), 400

    conn = sqlite3.connect(DB_PATH)
    existe = conn.execute("SELECT id FROM clubs WHERE email = ?", (email,)).fetchone()
    conn.close()
    if existe:
        return jsonify({"erreur": "Un compte existe déjà avec cet email"}), 400

    club = creer_club(nom, email, mot_de_passe)
    session["club_id"] = club["id"]
    return jsonify({"ok": True})


@app.route("/api/connexion", methods=["POST"])
def api_connexion():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    mot_de_passe = data.get("mot_de_passe", "")

    club = verifier_identifiants(email, mot_de_passe)
    if not club:
        return jsonify({"erreur": "Email ou mot de passe incorrect"}), 401

    session["club_id"] = club["id"]
    return jsonify({"ok": True})


@app.route("/api/deconnexion", methods=["POST"])
def api_deconnexion():
    session.pop("club_id", None)
    return jsonify({"ok": True})


@app.route("/api/generer-seance", methods=["POST"])
def api_generer_seance():
    club = get_club_connecte()
    if not club:
        return jsonify({"erreur": "Non connecté"}), 401

    data = request.get_json()

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
