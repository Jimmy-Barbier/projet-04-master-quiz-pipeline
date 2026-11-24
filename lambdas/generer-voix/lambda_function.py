import json
import os
import urllib.parse
import boto3

s3 = boto3.client("s3")
polly = boto3.client("polly")
VOIX = os.environ.get("VOIX", "Remi")
LETTRES = ["A", "B", "C", "D"]
NB_MAX = int(os.environ.get("NB_MAX", "25"))   # nombre de questions gardées pour la vidéo


def generer(texte, bucket, cle):
    r = polly.synthesize_speech(
        Text=texte, OutputFormat="mp3", VoiceId=VOIX, Engine="neural", LanguageCode="fr-FR"
    )
    s3.put_object(Bucket=bucket, Key=cle, Body=r["AudioStream"].read(),
                  ContentType="audio/mpeg")


def lambda_handler(event, context):
    if "Records" in event:
        record = event["Records"][0]["s3"]
        bucket = record["bucket"]["name"]
        cle = urllib.parse.unquote_plus(record["object"]["key"])
        exclure = []
    else:
        bucket = event["bucket"]
        cle = event["questions"]
        exclure = event.get("exclure", [])
    nom = os.path.splitext(os.path.basename(cle))[0]

    # Le fichier généré par Bedrock n'est jamais modifié : on en tire une version finale
    brutes = json.loads(s3.get_object(Bucket=bucket, Key=cle)["Body"].read())
    exclus = {int(n) for n in exclure}
    questions = [q for i, q in enumerate(brutes) if i not in exclus][:NB_MAX]
    print(f"{len(brutes)} questions, {len(exclus)} exclues, {len(questions)} gardées")

    s3.put_object(
        Bucket=bucket, Key=f"questions/{nom}-final.json",
        Body=json.dumps(questions, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    for i, q in enumerate(questions):
        bonne = q["bonne"]
        reponse = f"Réponse {LETTRES[bonne]} : {q['choix'][bonne]}."
        generer(q["question"], bucket, f"voix/{nom}/q{i:02d}_question.mp3")
        generer(reponse, bucket, f"voix/{nom}/q{i:02d}_reponse.mp3")

    return {"bucket": bucket, "nom": nom, "voix": f"voix/{nom}/",
            "nombre": len(questions), "index": list(range(len(questions)))}
