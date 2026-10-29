"""Il manifesto del canale degli aggiornamenti (latest.json) per una release pubblicata dell'app.

Uso: canale-aggiornamenti.py <tag> <cartella con i file della release> <data di pubblicazione ISO> <urgente: si|no>
Stampa il manifesto. Ogni pacchetto deve avere la sua firma (.sig), firmata per la versione del tag: la verifica
con la chiave pubblica la fa verifica-firme.sh prima di questo script.
I nomi delle piattaforme sono gli stessi di TARGET in app/desktop/src/aggiornamenti.rs (la prova dell'app li usa):
se non coincidono lo script si ferma.
"""
import json
import pathlib
import re
import sys

REPO = "ilmondovero/cup-piemonte-checker"
RUST = pathlib.Path(__file__).resolve().parents[2] / "app/desktop/src/aggiornamenti.rs"

# pacchetto -> piattaforme: {os}-{arch}-{installazione} per chi sa come e' installato, {os}-{arch} di riserva
PIATTAFORME = {
    "-setup.exe": ["windows-x86_64-nsis", "windows-x86_64"],
    ".msi": ["windows-x86_64-msi"],
    ".app.tar.gz": ["darwin-aarch64", "darwin-x86_64"],  # un solo pacchetto per Intel e Apple Silicon
    ".AppImage": ["linux-x86_64-appimage", "linux-x86_64"],
    ".deb": ["linux-x86_64-deb"],
    ".rpm": ["linux-x86_64-rpm"],
}


def main():
    tag, cartella, data, urgente = sys.argv[1:5]
    versione = tag.removeprefix("app-v")
    cartella = pathlib.Path(cartella)

    nomi = sorted(t for ts in PIATTAFORME.values() for t in ts)
    rust = sorted(re.findall(r'"([a-z0-9_-]+)"', RUST.read_text(encoding="utf-8").split("pub const TARGET")[1].split("];")[0]))
    if nomi != rust:
        sys.exit(f"Piattaforme diverse da TARGET in aggiornamenti.rs: {nomi} contro {rust}")

    piattaforme = {}
    for fine, targets in PIATTAFORME.items():
        trovati = [f for f in cartella.iterdir() if f.name.endswith(fine) and versione in f.name]
        if len(trovati) != 1:
            sys.exit(f"Atteso un pacchetto *{fine} della versione {versione}, trovati {[f.name for f in trovati]}")
        pacchetto = trovati[0]
        sig = pacchetto.with_name(pacchetto.name + ".sig")
        if not sig.exists():
            sys.exit(f"Manca la firma {sig.name}")
        voce = {
            "url": f"https://github.com/{REPO}/releases/download/{tag}/{pacchetto.name}",
            "signature": sig.read_text(encoding="utf-8").strip(),
        }
        for t in targets:
            piattaforme[t] = voce

    manifesto = {
        "version": versione,
        "notes": f"Novità della versione {versione}: https://github.com/{REPO}/releases/tag/{tag}",
        "pub_date": data,
        # l'app non aspetta il rodaggio di 48 ore (vedi aggiornamenti.rs)
        "urgente": urgente == "si",
        "platforms": piattaforme,
    }
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
