"""Original demonstration media: "The Lumiere Protocol".

Every line of dialogue, every scene description and every translation in this
file is original work written for this project and licensed under the repository
licence. No third-party film, script, subtitle file or recording is used
anywhere in AccessPulse. See docs/MEDIA_RIGHTS.md.

The programme is modelled as a timed event list rather than an encoded video so
that the whole pipeline runs on a laptop with no media assets to download. The
probes consume exactly the same structures they would consume from a real
decoder: timed dialogue tokens, timed caption cues, described-audio windows and
interpreter-feed frame statistics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueLine:
    index: int
    start_s: float
    end_s: float
    speaker: str
    text: dict[str, str]  # language -> line
    scene: str

    def tokens(self, language: str) -> list[str]:
        return _tokenise(self.text.get(language, self.text["en"]))


@dataclass(frozen=True)
class SceneDescription:
    """A visual event that audio description must cover."""

    index: int
    start_s: float
    end_s: float
    text: dict[str, str]
    importance: float  # 0..1, drives semantic-coverage scoring


def _tokenise(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("’", "'").split():
        tok = "".join(ch for ch in raw.lower() if ch.isalnum() or ch in "'-")
        if tok:
            out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Act 1 of the premiere - looped by the simulator to fill the event window.
# ---------------------------------------------------------------------------

# Timings leave deliberate 4-5 second dialogue gaps: audio description is
# authored into those gaps, which is what the description-masking probe checks.
SCRIPT: list[DialogueLine] = [
    DialogueLine(
        0, 4.5, 8.7, "ELIAS",
        {
            "en": "The projector has been running for eleven hours and nobody knows who started it.",
            "fr": "Le projecteur tourne depuis onze heures et personne ne sait qui l'a lance.",
            "de": "Der Projektor laeuft seit elf Stunden und niemand weiss, wer ihn gestartet hat.",
            "es": "El proyector lleva once horas encendido y nadie sabe quien lo puso en marcha.",
        },
        "projection_booth",
    ),
    DialogueLine(
        1, 9.1, 13.5, "NOOR",
        {
            "en": "Then we stop guessing. We read the reels in order, the way the archive intended.",
            "fr": "Alors arretons de deviner. Lisons les bobines dans l'ordre, comme l'archive le voulait.",
            "de": "Dann hoeren wir auf zu raten. Wir lesen die Rollen der Reihe nach, wie das Archiv es wollte.",
            "es": "Entonces dejemos de adivinar. Leamos los rollos en orden, como el archivo pretendia.",
        },
        "projection_booth",
    ),
    DialogueLine(
        2, 13.9, 17.6, "ELIAS",
        {
            "en": "Reel four is missing. It has been missing since nineteen sixty two.",
            "fr": "La bobine quatre a disparu. Elle manque depuis mille neuf cent soixante-deux.",
            "de": "Rolle vier fehlt. Sie fehlt seit neunzehnhundertzweiundsechzig.",
            "es": "Falta el rollo cuatro. Falta desde mil novecientos sesenta y dos.",
        },
        "projection_booth",
    ),
    DialogueLine(
        3, 23.0, 27.9, "ARCHIVIST",
        {
            "en": "Nothing here is missing. Some of it is simply waiting for the right light.",
            "fr": "Rien ici n'a disparu. Une partie attend simplement la bonne lumiere.",
            "de": "Hier fehlt nichts. Manches wartet nur auf das richtige Licht.",
            "es": "Aqui no falta nada. Una parte simplemente espera la luz adecuada.",
        },
        "vault_corridor",
    ),
    DialogueLine(
        4, 28.3, 33.1, "NOOR",
        {
            "en": "If the light is the key, then the protocol was never about the film at all.",
            "fr": "Si la lumiere est la cle, alors le protocole n'a jamais concerne le film.",
            "de": "Wenn das Licht der Schluessel ist, ging es beim Protokoll nie um den Film.",
            "es": "Si la luz es la clave, el protocolo nunca trato sobre la pelicula.",
        },
        "vault_corridor",
    ),
    DialogueLine(
        5, 38.3, 42.1, "ELIAS",
        {
            "en": "It was about who would still be here to watch it.",
            "fr": "Il s'agissait de savoir qui serait encore la pour le voir.",
            "de": "Es ging darum, wer noch da sein wuerde, um ihn zu sehen.",
            "es": "Se trataba de quien seguiria aqui para verla.",
        },
        "vault_corridor",
    ),
    DialogueLine(
        6, 42.5, 47.8, "ARCHIVIST",
        {
            "en": "Every restoration is an argument about memory. Ours is unusually loud.",
            "fr": "Toute restauration est un debat sur la memoire. La notre est inhabituellement bruyante.",
            "de": "Jede Restaurierung ist ein Streit ueber Erinnerung. Unserer ist ungewoehnlich laut.",
            "es": "Toda restauracion es una discusion sobre la memoria. La nuestra es inusualmente ruidosa.",
        },
        "screening_room",
    ),
    DialogueLine(
        7, 48.2, 53.4, "NOOR",
        {
            "en": "Play it for the room. If the audience cannot follow it, the restoration failed.",
            "fr": "Diffusez-le pour la salle. Si le public ne peut pas suivre, la restauration a echoue.",
            "de": "Spielt ihn fuer den Saal ab. Wenn das Publikum nicht folgen kann, ist die Restaurierung gescheitert.",
            "es": "Proyectalo para la sala. Si el publico no puede seguirlo, la restauracion fracaso.",
        },
        "screening_room",
    ),
    DialogueLine(
        8, 58.5, 63.7, "ELIAS",
        {
            "en": "Captions on every seat, description in both languages, and an interpreter on the side panel.",
            "fr": "Sous-titres a chaque place, audiodescription dans les deux langues, et un interprete sur le panneau lateral.",
            "de": "Untertitel auf jedem Platz, Beschreibung in beiden Sprachen und ein Dolmetscher auf der Seitenflaeche.",
            "es": "Subtitulos en cada asiento, audiodescripcion en ambos idiomas y un interprete en el panel lateral.",
        },
        "screening_room",
    ),
    DialogueLine(
        9, 64.3, 68.6, "ARCHIVIST",
        {
            "en": "Then we are finally showing the whole film, not merely the picture.",
            "fr": "Alors nous montrons enfin tout le film, pas seulement l'image.",
            "de": "Dann zeigen wir endlich den ganzen Film, nicht nur das Bild.",
            "es": "Entonces por fin mostramos la pelicula entera, no solo la imagen.",
        },
        "screening_room",
    ),
    DialogueLine(
        10, 73.3, 78.1, "NOOR",
        {
            "en": "Reel four was never lost. It was catalogued under a name nobody thought to read.",
            "fr": "La bobine quatre n'a jamais ete perdue. Elle etait cataloguee sous un nom que personne n'a pense a lire.",
            "de": "Rolle vier war nie verloren. Sie war unter einem Namen katalogisiert, den niemand zu lesen dachte.",
            "es": "El rollo cuatro nunca se perdio. Estaba catalogado con un nombre que nadie penso en leer.",
        },
        "vault_corridor",
    ),
    DialogueLine(
        11, 78.7, 83.5, "ELIAS",
        {
            "en": "Read it aloud. Some archives only answer when you say the name correctly.",
            "fr": "Lisez-le a voix haute. Certaines archives ne repondent que si l'on prononce bien le nom.",
            "de": "Lies ihn laut vor. Manche Archive antworten nur, wenn man den Namen richtig ausspricht.",
            "es": "Leelo en voz alta. Algunos archivos solo responden si dices bien el nombre.",
        },
        "vault_corridor",
    ),
]

SCENES: list[SceneDescription] = [
    SceneDescription(
        0, 0.3, 4.1,
        {
            "en": "A film projector throws a trembling beam across an empty booth.",
            "fr": "Un projecteur lance un faisceau tremblant a travers une cabine vide.",
        },
        0.9,
    ),
    SceneDescription(
        1, 18.4, 22.6,
        {
            "en": "An archivist steps out of a corridor lined with numbered metal cans.",
            "fr": "Une archiviste sort d'un couloir borde de boites metalliques numerotees.",
        },
        0.8,
    ),
    SceneDescription(
        2, 33.6, 37.9,
        {
            "en": "Dust rises in the beam. Noor turns a reel over in gloved hands.",
            "fr": "La poussiere s'eleve dans le faisceau. Noor retourne une bobine dans ses mains gantees.",
        },
        0.7,
    ),
    SceneDescription(
        3, 53.9, 58.1,
        {
            "en": "Rows of seats fill. A side panel lights up with a sign-language interpreter.",
            "fr": "Les rangees de sieges se remplissent. Un panneau lateral s'allume avec une interprete en langue des signes.",
        },
        0.95,
    ),
    SceneDescription(
        4, 69.0, 73.0,
        {
            "en": "A catalogue card, handwritten, reads: LUMIERE PROTOCOL, REEL FOUR.",
            "fr": "Une fiche de catalogue, manuscrite, indique : PROTOCOLE LUMIERE, BOBINE QUATRE.",
        },
        1.0,
    ),
]

LOOP_SECONDS = 85.0
SPEAKERS = sorted({line.speaker for line in SCRIPT})
SUPPORTED_LANGUAGES = ("en", "fr", "de", "es")


# ---------------------------------------------------------------------------
# Timeline helpers
# ---------------------------------------------------------------------------


def line_at(t: float) -> DialogueLine | None:
    """Dialogue line active at programme time t (looped)."""
    tt = t % LOOP_SECONDS
    for line in SCRIPT:
        if line.start_s <= tt < line.end_s:
            return line
    return None


def lines_in(start: float, end: float) -> list[tuple[float, DialogueLine]]:
    """(absolute_start, line) pairs overlapping [start, end)."""
    out: list[tuple[float, DialogueLine]] = []
    loop0 = int(start // LOOP_SECONDS)
    loop1 = int(end // LOOP_SECONDS) + 1
    for loop in range(loop0, loop1 + 1):
        base = loop * LOOP_SECONDS
        for line in SCRIPT:
            abs_start = base + line.start_s
            abs_end = base + line.end_s
            if abs_end > start and abs_start < end:
                out.append((abs_start, line))
    return sorted(out, key=lambda x: x[0])


def scenes_in(start: float, end: float) -> list[tuple[float, SceneDescription]]:
    out: list[tuple[float, SceneDescription]] = []
    loop0 = int(start // LOOP_SECONDS)
    loop1 = int(end // LOOP_SECONDS) + 1
    for loop in range(loop0, loop1 + 1):
        base = loop * LOOP_SECONDS
        for scene in SCENES:
            abs_start = base + scene.start_s
            abs_end = base + scene.end_s
            if abs_end > start and abs_start < end:
                out.append((abs_start, scene))
    return sorted(out, key=lambda x: x[0])


def spoken_tokens(start: float, end: float, language: str = "en") -> list[tuple[float, str]]:
    """Timestamped reference tokens - the ground truth captions are measured against."""
    out: list[tuple[float, str]] = []
    for abs_start, line in lines_in(start, end):
        toks = line.tokens(language)
        if not toks:
            continue
        span = (line.end_s - line.start_s) / len(toks)
        for i, tok in enumerate(toks):
            ts = abs_start + i * span
            if start <= ts < end:
                out.append((ts, tok))
    return out


def dialogue_gaps(start: float, end: float, min_gap: float = 1.2) -> list[tuple[float, float]]:
    """Windows with no dialogue - where audio description belongs."""
    busy = [(at, at + (line.end_s - line.start_s)) for at, line in lines_in(start, end)]
    busy.sort()
    gaps: list[tuple[float, float]] = []
    cursor = start
    for s, e in busy:
        if s - cursor >= min_gap:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if end - cursor >= min_gap:
        gaps.append((cursor, end))
    return gaps


MEDIA_MANIFEST = {
    "title": "The Lumiere Protocol",
    "origin": "original work created for AccessPulse",
    "licence": "Apache-2.0 (same as repository)",
    "third_party_content": "none",
    "languages": list(SUPPORTED_LANGUAGES),
    "described_languages": ["en", "fr"],
    "sign_language": "fr-LSF",
    "loop_seconds": LOOP_SECONDS,
    "dialogue_lines": len(SCRIPT),
    "described_scenes": len(SCENES),
}
