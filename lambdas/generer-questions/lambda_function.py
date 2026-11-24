import json
import os
import random
import urllib.parse
import boto3

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
MODEL_ID = os.environ["MODEL_ID"]

MARGE = 10         # questions demandées en plus, pour remplacer celles rejetées par le contrôle
MAX_CHOIX = 20     # longueur maxi d'un choix (au-delà, ça déborde des cases)
MAX_QUESTION = 80  # longueur maxi d'une question

PROMPT = """Tu es l'auteur des questions d'un quiz télé français grand public.
Thème : {theme}
Difficulté : {difficulte}
Nombre de questions : {nombre}

EXACTITUDE
- Chaque question a UNE SEULE bonne réponse, vérifiable et incontestable.
- Vérifie chaque date, nom et fait. Au moindre doute, choisis un autre fait.
- Pour chaque question, vérifie qu'aucune des trois mauvaises réponses ne pourrait aussi être correcte.
- Pas de question piège, ni de question dont la réponse fait débat ou dépend de l'interprétation.

MAUVAISES RÉPONSES
- Elles doivent être crédibles et de même nature que la bonne réponse (un acteur avec des acteurs, une ville avec des villes, une année avec des années).
- Jamais de fragments de mots, de réponses absurdes ou sans rapport avec la question.

FORMAT
- Question de {max_question} caractères maximum, claire et naturelle, comme la poserait un animateur de quiz télé.
- Chaque choix fait {max_choix} caractères maximum, espaces compris. Si la bonne réponse est plus longue, choisis une autre question.
- Utilise les titres, noms et répliques de la version française officielle.
- Écris les titres d'œuvres entre guillemets français « ».

VARIÉTÉ
- Pas deux questions sur la même œuvre, la même personne ou le même événement.
- Jamais deux fois la même bonne réponse dans le quiz.
- Couvre des aspects variés du thème et des niveaux de connaissance différents, en respectant la difficulté demandée.
- Pas de questions trop évidentes sur les règles de base ou les notions que tout le monde connaît (nombre de joueurs, couleur d'un carton, durée d'une mi-temps).
- La réponse ne doit jamais apparaître, même en partie, dans la question.
- Choisis des sujets connus du grand public français (œuvres, personnes, événements populaires en France), pas des références réservées aux spécialistes ou connues seulement à l'étranger.

JUSTIFICATION
- Pour chaque question, ajoute une justification courte (20 mots maximum) qui cite le fait précis prouvant la bonne réponse (date, œuvre, auteur, lieu…).
- Si tu ne peux pas écrire une justification précise et certaine, ne propose pas cette question.

SUJETS À ÉVITER
- Les sujets tragiques ou sensibles : attentats, guerres, catastrophes, crimes, maladies, décès.

Mets toujours la bonne réponse en premier dans la liste des choix.
Réponds uniquement avec un tableau JSON, sans texte autour, au format :
[{{"question": "...", "choix": ["bonne réponse", "...", "...", "..."], "justification": "..."}}]"""


def est_valide(q, bonnes_vues):
    """Rejette les questions mal formées, trop longues, ou en double."""
    question, choix = q.get("question"), q.get("choix")
    if not isinstance(question, str) or not isinstance(choix, list) or len(choix) != 4:
        return False
    if len(question) > MAX_QUESTION:
        return False
    if any(not isinstance(c, str) or not c.strip() or len(c) > MAX_CHOIX for c in choix):
        return False
    if len({c.strip().lower() for c in choix}) != 4:        # deux choix identiques
        return False
    bonne = choix[0].strip().lower()
    if bonne in bonnes_vues:                                 # même réponse déjà utilisée
        return False
    bonnes_vues.add(bonne)
    return True


def lambda_handler(event, context):
    if "Records" in event:
        record = event["Records"][0]["s3"]
        bucket = record["bucket"]["name"]
        cle = urllib.parse.unquote_plus(record["object"]["key"])
    else:
        bucket = event["bucket"]
        cle = event["demande"]

    demande = json.loads(s3.get_object(Bucket=bucket, Key=cle)["Body"].read())
    nombre = demande.get("nombre", 20)
    prompt = PROMPT.format(
        theme=demande["theme"],
        difficulte=demande.get("difficulte", "moyenne"),
        nombre=nombre + MARGE,
        max_question=MAX_QUESTION - 10,   # on demande plus court que la vraie limite, par sécurité
        max_choix=MAX_CHOIX,
    )

    reponse = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 12000, "temperature": 0.1},
    )
    texte = reponse["output"]["message"]["content"][0]["text"]
    texte = texte[texte.find("["): texte.rfind("]") + 1]
    brutes = json.loads(texte)

    bonnes_vues = set()
    questions = [q for q in brutes if est_valide(q, bonnes_vues)][:nombre]
    print(f"{len(brutes)} questions générées, {len(questions)} gardées après contrôle")

    for q in questions:
        bonne_reponse = q["choix"][0]
        random.shuffle(q["choix"])
        q["bonne"] = q["choix"].index(bonne_reponse)

    nom = os.path.splitext(os.path.basename(cle))[0]
    sortie = f"questions/{nom}.json"
    s3.put_object(
        Bucket=bucket, Key=sortie,
        Body=json.dumps(questions, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return {"bucket": bucket, "questions": sortie, "nombre": len(questions)}