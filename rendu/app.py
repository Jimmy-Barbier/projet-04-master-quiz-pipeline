import json
import os
import shutil
import subprocess
import boto3
import textwrap

from PIL import Image, ImageDraw, ImageFont, ImageChops

s3 = boto3.client("s3")
polly = boto3.client("polly")

FONT = "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"
LETTRES = ["A", "B", "C", "D"]
COULEURS = [(231, 76, 60), (52, 152, 219), (46, 204, 113), (241, 196, 15)]
OR = (243, 190, 45)
NB_SHORT = 10                          # nombre de questions annoncées dans le short
NB_SHORT_RENDU = 20                    # questions rendues pour le short (partie 1 + partie 2)
NB_YOUTUBE = 25                        # nombre de questions annoncées dans la vidéo YouTube
VOIX = os.environ.get("VOIX", "Lea")   # voix de l'intro et de l'outro (Rémi lit les questions)
LOGO = "/tmp/logo.png"

# Paliers de la grille de score de l'outro
PALIERS = {
    "short":   [("0-3", "DÉBUTANT"), ("4-6", "PAS MAL !"), ("7-9", "EXPERT"),
                ("10", "MASTER QUIZ !")],
    "youtube": [("0-8", "DÉBUTANT"), ("9-15", "PAS MAL !"), ("16-22", "EXPERT"),
                ("23-25", "MASTER QUIZ !")],
}

# Mise en page et rythme de chaque format.
LAYOUTS = {
    "youtube": {
        "W": 1920, "H": 1080,
        "w_box": 760, "h_box": 140,
        "positions": [(160, 520), (1000, 520), (160, 720), (1000, 720)],
        "bandeau": (260, 150, 1660, 315),
        "q_y": 232, "q_wrap": 42, "q_size": 50,
        "c_y": 417, "c_size": 110,
        "ch_size": 48,
        "logo": (40, 25, 150),             # x, y, taille du badge
        "timing": (2, 10, 4),              # pause, compte à rebours, révélation
        # outro : taille du panneau, centre, titre, pastilles, textes du bas
        # grille sur la moitié gauche : la moitié droite reste libre pour l'écran de fin
        "outro": {"W": 860, "H": 860, "cx": 520, "cy": 620,
                  "titre": (85, 76), "pastille": (700, 100, 165, 122, 46),
                  "bas": [("Dis-le en commentaire !", 700, 50),
                          ("Abonne-toi pour le prochain quiz !", 780, 40)]},
        "outro_min": 10.0,                 # durée mini (écran de fin YouTube)
    },
    "short": {
        "W": 1080, "H": 1920,
        "w_box": 940, "h_box": 175,
        "positions": [(70, 860), (70, 1055), (70, 1250), (70, 1445)],
        "bandeau": (60, 320, 1020, 640),
        "q_y": 480, "q_wrap": 24, "q_size": 64,
        "c_y": 770, "c_size": 150,
        "ch_size": 62,
        "logo": (40, 150, 150),
        "timing": (0.5, 4, 1.5),
        "outro": {"W": 920, "H": 960, "cx": 540, "cy": 1010,
                  "titre": (100, 90), "pastille": (780, 120, 200, 140, 56),
                  "bas": [("Dis-le en commentaire !", 815, 54)]},
        "outro_min": 0.0,
    },
}


# ---------- Outils ----------

def executer(commande):
    """Lance une commande et affiche le vrai message d'erreur si elle plante."""
    resultat = subprocess.run(commande, capture_output=True, text=True)
    if resultat.returncode != 0:
        raise RuntimeError(
            f"{commande[0]} a échoué (code {resultat.returncode}) :\n"
            + resultat.stderr[-2000:]
        )
    return resultat.stdout


def duree_audio(chemin):
    out = executer(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", chemin])
    return float(out.strip())


def fond_video(L):
    W, H = L["W"], L["H"]
    return (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=4,eq=brightness=-0.15[bg]")


def encoder(entrees, graphe, total, sortie):
    executer([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "30", "-i", "/tmp/fond.jpg",
        *entrees,
        "-filter_complex", graphe, "-map", "[v]", "-map", "[a]",
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", sortie])


def sans_les_espaces_orphelines(texte):
    """Colle ? et ! au mot précédent pour qu'ils ne partent pas seuls à la ligne."""
    return (texte.replace(" ?", "\u00a0?").replace(" !", "\u00a0!")
            .replace("« ", "«\u00a0").replace(" »", "\u00a0»"))


def police(taille):
    return ImageFont.truetype(FONT, taille)


def ajuster(texte, taille, largeur_max):
    """Réduit la taille de police jusqu'à ce que le texte tienne dans la largeur."""
    while taille > 20 and police(taille).getlength(texte) > largeur_max:
        taille -= 2
    return taille


def synthese_lea(texte, chemin):
    r = polly.synthesize_speech(Text=texte, OutputFormat="mp3", VoiceId=VOIX,
                                Engine="neural", LanguageCode="fr-FR")
    with open(chemin, "wb") as f:
        f.write(r["AudioStream"].read())


# ---------- Logo ----------

def telecharger_logo(bucket):
    try:
        s3.download_file(bucket, "assets/logo.png", LOGO)
        return True
    except Exception:
        return False                  # pas de logo : la vidéo se fait quand même


def badge(taille):
    """Découpe le disque central du logo en badge rond, avec un fin liseré doré."""
    if not os.path.exists(LOGO):
        return None
    img = Image.open(LOGO).convert("RGBA")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    r, c = int(s * 0.44), s // 2
    img = img.crop((c - r, c - r, c + r, c + r)).resize((taille, taille), Image.LANCZOS)
    masque = Image.new("L", (taille, taille), 0)
    ImageDraw.Draw(masque).ellipse([0, 0, taille - 1, taille - 1], fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), masque))
    ImageDraw.Draw(img).ellipse([1, 1, taille - 2, taille - 2],
                                outline=OR + (255,), width=max(2, taille // 50))
    return img


# ---------- Dessins Pillow ----------

def dessiner_cases(L, chemin, logo=None):
    img = Image.new("RGBA", (L["W"], L["H"]), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w, h = L["w_box"], L["h_box"]
    for i, (x, y) in enumerate(L["positions"]):
        d.rounded_rectangle([x, y, x + w, y + h], radius=30,
                            fill=(15, 15, 35, 180),
                            outline=COULEURS[i] + (255,), width=6)
    d.rounded_rectangle(list(L["bandeau"]), radius=30,
                        fill=(15, 15, 35, 180),
                        outline=(255, 255, 255, 120), width=4)
    if logo is not None:
        img.alpha_composite(logo, (L["logo"][0], L["logo"][1]))
    img.save(chemin)


def dessiner_bonne(L, x, y, chemin):
    img = Image.new("RGBA", (L["W"], L["H"]), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x, y, x + L["w_box"], y + L["h_box"]], radius=30,
                        fill=(46, 204, 113, 220),
                        outline=(255, 255, 255, 255), width=6)
    img.save(chemin)


def banniere(lignes, largeur_max):
    """Bandeau doré arrondi avec une ou plusieurs lignes de texte foncé."""
    lignes = [(t, ajuster(t, s, largeur_max - 120)) for t, s in lignes]
    largeur = int(max(police(s).getlength(t) for t, s in lignes)) + 120
    hauteurs = [int(s * 1.3) for _, s in lignes]
    hauteur = sum(hauteurs) + 70
    img = Image.new("RGBA", (largeur, hauteur), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, largeur - 1, hauteur - 1], radius=36,
                        fill=OR + (255,), outline=(255, 255, 255, 230), width=6)
    y = 35
    for (t, s), hl in zip(lignes, hauteurs):
        d.text((largeur / 2, y + hl / 2), t, font=police(s),
               fill=(20, 20, 35, 255), anchor="mm")
        y += hl
    return img


def panneau_score(fmt, bas=None):
    """Grand encadré de l'outro : TON SCORE ? + 4 pastilles de couleur + texte du bas."""
    o = LAYOUTS[fmt]["outro"]
    W, H = o["W"], o["H"]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=50,
                        fill=(15, 15, 35, 220), outline=OR + (255,), width=8)
    ty, ts = o["titre"]
    d.text((W / 2, ty), "TON SCORE\u00a0?", font=police(ts), fill="white",
           anchor="mm", stroke_width=3, stroke_fill="black")
    pw, ph, py, pas, taille = o["pastille"]
    couleurs = [COULEURS[0], COULEURS[1], COULEURS[2], OR]
    for i, (plage, label) in enumerate(PALIERS[fmt]):
        x0, y0 = (W - pw) / 2, py + i * pas
        d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=30,
                            fill=couleurs[i] + (255,))
        texte = sans_les_espaces_orphelines(f"{plage}  ·  {label}")
        if i == 3:
            d.text((W / 2, y0 + ph / 2), texte, font=police(taille),
                   fill=(20, 20, 35, 255), anchor="mm")
        else:
            d.text((W / 2, y0 + ph / 2), texte, font=police(taille), fill="white",
                   anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 160))
    for texte, y, taille in (bas or o["bas"]):
        texte = sans_les_espaces_orphelines(texte)
        d.text((W / 2, y), texte, font=police(ajuster(texte, taille, W - 80)),
               fill="white", anchor="mm")
    return img


def generer_frames(L, dossier, statiques, animes, n_anim=12):
    """Images PNG de l'animation : les éléments 'animes' apparaissent en zoom + fondu.
    statiques : [(image, x, y)]   animes : [(image, centre_x, centre_y, image_de_depart)]"""
    shutil.rmtree(dossier, ignore_errors=True)
    os.makedirs(dossier)
    nb = max([a[3] for a in animes], default=0) + n_anim
    for f in range(nb):
        cadre = Image.new("RGBA", (L["W"], L["H"]), (0, 0, 0, 0))
        for im, x, y in statiques:
            cadre.alpha_composite(im, (x, y))
        for im, cx, cy, debut in animes:
            if f < debut:
                continue
            p = min(1.0, (f - debut + 1) / n_anim)
            echelle = 0.3 + 0.7 * (1 - (1 - p) ** 3)
            w, h = max(1, int(im.width * echelle)), max(1, int(im.height * echelle))
            e = im.resize((w, h), Image.LANCZOS)
            if p < 1:
                k = min(1.0, p * 1.5)
                e.putalpha(e.getchannel("A").point(lambda v, k=k: int(v * k)))
            cadre.alpha_composite(e, (int(cx - w / 2), int(cy - h / 2)))
        cadre.save(f"{dossier}/f_{f:03d}.png")


# ---------- Clip d'une question ----------

def rendre_question(bucket, nom, i, fmt):
    L = LAYOUTS[fmt]
    PAUSE, COMPTE, REVELATION = L["timing"]
    tmp = f"/tmp/{fmt}_q{i:02d}"
    os.makedirs(tmp, exist_ok=True)

    questions = json.loads(
        s3.get_object(Bucket=bucket, Key=f"questions/{nom}-final.json")["Body"].read())
    q = questions[i]

    voix_q, voix_r = f"{tmp}/question.mp3", f"{tmp}/reponse.mp3"
    s3.download_file(bucket, f"voix/{nom}/q{i:02d}_question.mp3", voix_q)
    s3.download_file(bucket, f"voix/{nom}/q{i:02d}_reponse.mp3", voix_r)
    s3.download_file(bucket, "assets/fond.jpg", "/tmp/fond.jpg")
    s3.download_file(bucket, "assets/tic.mp3", "/tmp/tic.mp3")
    s3.download_file(bucket, "assets/bonne.mp3", "/tmp/bonne.mp3")
    telecharger_logo(bucket)
    d_q, d_r = duree_audio(voix_q), duree_audio(voix_r)

    debut_compte = d_q + PAUSE
    reveal = debut_compte + COMPTE
    total = reveal + max(REVELATION, d_r + 1)

    with open(f"{tmp}/question.txt", "w", encoding="utf-8") as f:
        texte = sans_les_espaces_orphelines(q["question"])
        f.write("\n".join(textwrap.wrap(texte, L["q_wrap"])))

    cases, bonne = f"{tmp}/cases.png", f"{tmp}/bonne.png"
    dessiner_cases(L, cases, badge(L["logo"][2]))

    filtres = [
        f"drawtext=fontfile={FONT}:textfile={tmp}/question.txt:text_align=C:"
        f"fontsize={L['q_size']}:fontcolor=white:x=(w-text_w)/2:y={L['q_y']}-text_h/2",
        f"drawtext=fontfile={FONT}:text='%{{eif\\:ceil({reveal:.3f}-t)\\:d}}':"
        f"fontsize={L['c_size']}:fontcolor=0x2ECC71:borderw=4:bordercolor=black:"
        f"x=(w-text_w)/2:y={L['c_y']}-text_h/2:"
        f"enable='between(t,{debut_compte:.3f},{reveal:.3f})'",
    ]
    for k, (x, y) in enumerate(L["positions"]):
        with open(f"{tmp}/choix_{k}.txt", "w", encoding="utf-8") as f:
            f.write(f"{LETTRES[k]}.  {q['choix'][k]}")
        if k == q["bonne"]:
            dessiner_bonne(L, x, y, bonne)
        filtres.append(
            f"drawtext=fontfile={FONT}:textfile={tmp}/choix_{k}.txt:fontsize={L['ch_size']}:"
            f"fontcolor=white:x={x}+({L['w_box']}-text_w)/2:y={y}+({L['h_box']}-text_h)/2")
    filtres.append("fade=t=in:st=0:d=0.4")
    filtres.append(f"fade=t=out:st={total - 0.4:.3f}:d=0.4")

    tic_ms = int((reveal - 3) * 1000)
    rev_ms = int(reveal * 1000)
    graphe = (
        f"amovie=/tmp/tic.mp3,volume=2.5,volume=0.6,adelay={tic_ms}|{tic_ms}[tic];"
        f"amovie=/tmp/bonne.mp3,volume=0.8,adelay={rev_ms}|{rev_ms}[ding];"
        f"{fond_video(L)};"
        f"movie={cases}[cases];[bg][cases]overlay=0:0[v1];"
        f"movie={bonne}[bonne];[v1][bonne]overlay=0:0:enable='gte(t,{reveal:.3f})',"
        f"{','.join(filtres)}[v];"
        f"[2:a]adelay=delays={rev_ms}:all=1[r];"
        f"[1:a][r][tic][ding]amix=inputs=4:duration=longest:normalize=0,apad[a]"
    )

    sortie = f"{tmp}/clip.mp4"
    encoder(["-i", voix_q, "-i", voix_r], graphe, total, sortie)

    dossier = "clips" if fmt == "youtube" else "clips-shorts"
    cle = f"{dossier}/{nom}/clip_{i:02d}.mp4"
    s3.upload_file(sortie, bucket, cle)

    # Version sans voix : uniquement pour la vidéo YouTube
    if fmt == "youtube":
        sans_voix = f"{tmp}/sans_voix.mp4"
        executer([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", sortie, "-i", "/tmp/tic.mp3", "-i", "/tmp/bonne.mp3",
            "-filter_complex",
            f"[1:a]volume=2.5,adelay={tic_ms}|{tic_ms}[t];[2:a]adelay={rev_ms}|{rev_ms}[b];"
            "[t][b]amix=inputs=2:normalize=0,apad[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-ac", "2", "-t", f"{total:.3f}", sans_voix])
        s3.upload_file(sans_voix, bucket, f"clips-sans-voix/{nom}/clip_{i:02d}.mp4")

    return {"clip": cle}


# ---------- Clips d'accroche (début et fin) ----------

def rendre_panneau(bucket, nom, typ, fmt, partie=None):
    L = LAYOUTS[fmt]
    suffixe = f"_p{partie}" if partie else ""
    tmp = f"/tmp/{fmt}_{typ}{suffixe}"
    os.makedirs(tmp, exist_ok=True)

    demande = json.loads(
        s3.get_object(Bucket=bucket, Key=f"demandes/{nom}.json")["Body"].read())
    theme = demande.get("theme", nom)
    nb_final = len(json.loads(
        s3.get_object(Bucket=bucket, Key=f"questions/{nom}-final.json")["Body"].read()))
    if fmt == "short":
        debut = (partie - 1) * NB_SHORT if partie else 0
        n = max(0, min(NB_SHORT, nb_final - debut))
        if n == 0:
            return {"clip": None}       # pas assez de questions pour cette partie
    else:
        n = min(NB_YOUTUBE, nb_final)

    s3.download_file(bucket, "assets/fond.jpg", "/tmp/fond.jpg")
    telecharger_logo(bucket)
    lx, ly, ltaille = L["logo"]
    petit_logo = badge(ltaille)
    statiques, animes = [], []

    if typ == "intro":
        nom_clip = f"a_intro{suffixe}"       # trié avant clip_00
        if fmt == "short":
            texte_partie = f", partie {partie}" if partie else ""
            texte_voix = f"{n} questions sur le thème : {theme}{texte_partie}. C'est parti !"
            if petit_logo is not None:
                statiques.append((petit_logo, lx, ly))
            lignes = [(f"{n} QUESTIONS", 100), (theme.upper(), 76)]
            if partie:
                lignes.append((f"PARTIE {partie}", 64))
            ban = banniere(lignes, L["W"] - 120)
            animes.append((ban, L["W"] // 2, 960, 0))
        else:
            texte_voix = (f"Bienvenue sur Master Quiz ! {n} questions sur le thème : "
                          f"{theme}. Note tes points, et c'est parti !")
            grand_logo = badge(340)
            if grand_logo is not None:
                animes.append((grand_logo, L["W"] // 2, 330, 0))
            ban = banniere([(f"QUIZ – {theme.upper()}", 84), (f"{n} QUESTIONS", 64)],
                           L["W"] - 300)
            animes.append((ban, L["W"] // 2, 770, 12))
    else:
        nom_clip = f"z_outro{suffixe}"       # trié après le dernier clip
        bas = None
        if fmt == "short" and partie == 1:
            texte_voix = ("Alors, quel est ton score ? Dis-le en commentaire, "
                          "et abonne-toi pour la partie 2 !")
            bas = [("Dis-le en commentaire !", 815, 54),
                   ("Abonne-toi pour la partie 2 !", 890, 50)]
        else:
            texte_voix = ("Alors, quel est ton score ? Dis-le en commentaire, "
                          "et abonne-toi pour le prochain quiz !")
        if petit_logo is not None:
            statiques.append((petit_logo, lx, ly))
        o = L["outro"]
        animes.append((panneau_score(fmt, bas), o["cx"], o["cy"], 0))

    voix = f"{tmp}/voix.mp3"
    synthese_lea(texte_voix, voix)
    total = duree_audio(voix) + (0.7 if typ == "intro" else 1.5)
    if typ == "outro":
        total = max(total, L["outro_min"])

    frames = f"{tmp}/frames"
    generer_frames(L, frames, statiques, animes)

    graphe = (
        f"{fond_video(L)};[2:v]format=rgba[an];"
        f"[bg][an]overlay=0:0:eof_action=repeat,"
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={total - 0.3:.3f}:d=0.3[v];"
        f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,apad[a]"
    )

    sortie = f"{tmp}/{nom_clip}.mp4"
    encoder(["-i", voix, "-framerate", "30", "-i", f"{frames}/f_%03d.png"],
            graphe, total, sortie)

    dossier = "clips" if fmt == "youtube" else "clips-shorts"
    cle = f"{dossier}/{nom}/{nom_clip}.mp4"
    s3.upload_file(sortie, bucket, cle)

    # Version muette de l'intro et de l'outro pour la vidéo sans voix
    if fmt == "youtube":
        sv = f"{tmp}/sans_voix.mp4"
        executer([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", sortie, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-ac", "2", "-t", f"{total:.3f}", sv])
        s3.upload_file(sv, bucket, f"clips-sans-voix/{nom}/{nom_clip}.mp4")

    return {"clip": cle}


# ---------- Point d'entrée ----------

def lambda_handler(event, context):
    bucket, nom = event["bucket"], event["nom"]
    fmt = event.get("format", "youtube")
    typ = event.get("type", "question")

    if typ in ("intro", "outro"):
        partie = event.get("partie")
        return rendre_panneau(bucket, nom, typ, fmt, int(partie) if partie else None)

    i = int(event["index"])
    if fmt == "short" and i >= NB_SHORT_RENDU:
        return {"clip": None}      # le short rend 20 questions : partie 1 (1-10) et partie 2 (11-20)
    return rendre_question(bucket, nom, i, fmt)