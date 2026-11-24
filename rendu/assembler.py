import os
import shutil
import subprocess
import boto3

from PIL import Image, ImageDraw, ImageFont

s3 = boto3.client("s3")

FONT = "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"
OR = (243, 190, 45)


def executer(commande):
    """Lance une commande et affiche le vrai message d'erreur si elle plante."""
    resultat = subprocess.run(commande, capture_output=True, text=True)
    if resultat.returncode != 0:
        raise RuntimeError(
            f"{commande[0]} a échoué (code {resultat.returncode}) :\n"
            + resultat.stderr[-2000:]
        )
    return resultat.stdout


def infos_video(chemin):
    """Renvoie (largeur, hauteur, durée) d'une vidéo."""
    out = executer(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:format=duration",
                    "-of", "default=nw=1", chemin])
    v = dict(l.split("=", 1) for l in out.strip().splitlines())
    return int(v["width"]), int(v["height"]), float(v["duration"])


def pastille(W, H, texte, chemin):
    """Petite image de la pastille dorée ; renvoie sa position (centrée en haut,
    à la hauteur du logo)."""
    portrait = H > W
    taille = 52 if portrait else 44
    centre_y = 225 if portrait else 100
    police = ImageFont.truetype(FONT, taille)
    largeur = int(police.getlength(texte)) + 70
    hauteur = int(taille * 1.6)
    img = Image.new("RGBA", (largeur, hauteur), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, largeur - 1, hauteur - 1], radius=hauteur // 2,
                        fill=OR + (255,), outline=(255, 255, 255, 230), width=4)
    d.text((largeur / 2, hauteur / 2), texte, font=police, fill=(20, 20, 35, 255),
           anchor="mm")
    img.save(chemin)
    return (W - largeur) // 2, centre_y - hauteur // 2


def lambda_handler(event, context):
    bucket = event["bucket"]
    nom = event["nom"]
    prefixe = f"{event.get('dossier', 'clips')}/{nom}/"

    objets = s3.list_objects_v2(Bucket=bucket, Prefix=prefixe).get("Contents", [])
    cles = sorted(o["Key"] for o in objets if o["Key"].endswith(".mp4"))
    maxi = event.get("max")
    debut = int(event.get("debut", 0))
    partie = event.get("partie")
    suffixe = f"_p{partie}" if partie else ""
    intro = [c for c in cles if c.endswith(f"/a_intro{suffixe}.mp4")]
    outro = [c for c in cles if c.endswith(f"/z_outro{suffixe}.mp4")]
    questions = [c for c in cles if "/clip_" in c][debut:]
    if maxi:
        questions = questions[:int(maxi)]
    if not questions:
        if partie:
            return {"video": None, "raison": f"pas de questions pour la partie {partie}"}
        raise Exception(f"Aucun clip dans {prefixe}")
    cles = intro + questions + outro

    # Téléchargement des clips
    travail = "/tmp/assemblage"
    shutil.rmtree(travail, ignore_errors=True)
    os.makedirs(travail)
    locaux = []
    for i, cle in enumerate(cles):
        local = f"{travail}/clip_{i:02d}.mp4"
        s3.download_file(bucket, cle, local)
        locaux.append(local)

    with open(f"{travail}/liste.txt", "w") as f:
        for local in locaux:
            f.write(f"file '{local}'\n")

    # Pastilles « QUESTION k/N », calculées sur l'ordre final des clips
    W, H, _ = infos_video(locaux[0])
    total_q = sum(1 for c in cles if "/clip_" in c)
    filtres, courant, debut, k = [], "0:v", 0.0, 0
    for cle, local in zip(cles, locaux):
        duree = infos_video(local)[2]
        if "/clip_" in cle:                        # pas de pastille sur l'intro et l'outro
            k += 1
            image = f"{travail}/q{k:02d}.png"
            x, y = pastille(W, H, f"QUESTION {k}/{total_q}", image)
            fin = debut + duree
            filtres.append(
                f"movie={image}[p{k}];[{courant}][p{k}]overlay={x}:{y}:"
                f"enable='between(t,{debut:.3f},{fin - 0.02:.3f})'[v{k}]")
            courant = f"v{k}"
        debut += duree
    filtres.append(f"[{courant}]format=yuv420p[v]")

    s3.download_file(bucket, "assets/musique.mp3", "/tmp/musique.mp3")
    sortie = f"{travail}/{nom}.mp4"
    executer([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", f"{travail}/liste.txt",
        "-stream_loop", "-1", "-i", "/tmp/musique.mp3",
        "-filter_complex",
        ";".join(filtres) + ";"
        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=0.12[m];"
        "[0:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[vx][sc];"
        "[m][sc]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=400[md];"
        "[vx][md]amix=inputs=2:duration=first:normalize=0[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        sortie])

    nom_fichier = f"{nom}-partie{partie}" if partie else nom
    cle_sortie = f"{event.get('sortie', 'videos')}/{nom_fichier}.mp4"
    s3.upload_file(sortie, bucket, cle_sortie)
    shutil.rmtree(travail, ignore_errors=True)
    return {"video": cle_sortie, "clips": len(cles), "questions": total_q}