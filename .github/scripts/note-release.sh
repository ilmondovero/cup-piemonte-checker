#!/usr/bin/env bash
# Le note della release dell'app: link diretti a ogni installer, avvisi del primo avvio, somme.
# Uso: note-release.sh <tag> <cartella con gli installer>   (stampa il markdown)
set -euo pipefail

tag="$1"
cartella="$2"
repo="${GITHUB_REPOSITORY:-ilmondovero/cup-piemonte-checker}"
base="https://github.com/$repo/releases/download/$tag"
v="${tag#app-v}"

# ogni file deve esserci: meglio fermarsi che pubblicare note con dei buchi
for tipo in '*-setup.exe' '*.msi' '*.dmg' '*.deb' '*.rpm' '*.AppImage' 'SHA256SUMS.txt'; do
  if ! compgen -G "$cartella/$tipo" >/dev/null; then
    echo "Manca un file $tipo in $cartella" >&2
    exit 1
  fi
done

link() { # il primo file che corrisponde, come link markdown
  local f
  f=$(cd "$cartella" && ls -- $1 | head -1)
  printf '[**%s**](%s/%s)' "$2" "$base" "$f"
}

cat <<EOF
App per il computer che controlla il [CUP Piemonte](https://cup.isan.csi.it/) ogni 20 minuti e avvisa quando
si libera una data prima della tua prenotazione. Sposta o disdice solo quando lo chiedi tu.
Pagina di download: https://ilmondovero.github.io/cup-piemonte-checker/

## Scarica la versione $v

| Sistema | File | |
|---|---|---|
| **Windows** 10 e 11 | $(link '*-setup.exe' 'Installer (.exe)') | consigliato, per il solo utente · oppure $(link '*.msi' '.msi') per tutti gli utenti |
| **macOS** (Intel e Apple Silicon) | $(link '*.dmg' 'Immagine disco (.dmg)') | trascina l'app in Applicazioni |
| **Linux** 64 bit | $(link '*.deb' '.deb') · $(link '*.rpm' '.rpm') · $(link '*.AppImage' '.AppImage') | Ubuntu 22.04+, Debian 12+, Mint 21+ · Fedora e openSUSE · le altre |

Somme SHA-256: $(link 'SHA256SUMS.txt' 'SHA256SUMS.txt') (su Windows: \`Get-FileHash .\nome-del-file -Algorithm SHA256\`).

## Al primo avvio

Gli installer non sono firmati con un certificato, quindi il sistema avvisa una volta:
- **Windows** ("Windows ha protetto il PC"): *Ulteriori informazioni*, poi *Esegui comunque*.
- **macOS** ("non può essere aperta" o "è danneggiata"): prova ad aprirla una volta, poi *Impostazioni di
  Sistema* > *Privacy e sicurezza* > *Apri comunque*. Se il pulsante non c'è: \`xattr -dr com.apple.quarantine "/Applications/CUP Piemonte.app"\`
- **Linux**: l'AppImage va resa eseguibile (\`chmod +x\`).

Dalla 0.3.0 l'app si aggiorna da sola (48 ore dopo la pubblicazione, o subito con ⚙ > Aggiorna). Da una versione
più vecchia si installa questa sopra: le ricette restano. I controlli girano con
il computer acceso e non in sospensione.

Disinstallazione, dove stanno i dati e altre domande: https://ilmondovero.github.io/cup-piemonte-checker/#domande
Problemi: [Issues](https://github.com/$repo/issues/new/choose), senza mai scrivere codice fiscale,
numero della ricetta o dati della visita.

## Com'è stata costruita

Gli installer li ha costruiti GitHub Actions dal codice di questo tag: prima i test sui tre sistemi, poi
ogni installer è stato installato e avviato su una macchina di prova (Windows, macOS, Ubuntu, Fedora).
Istruzioni complete e privacy: [app/README.md](https://github.com/$repo/tree/$tag/app#readme).
EOF
