#!/usr/bin/env bash
# Le note della release dell'app: link diretti a ogni installer, avvisi del primo avvio, somme.
# Uso: note-release.sh <tag> <cartella con gli installer>   (stampa il markdown)
set -euo pipefail

tag="$1"
cartella="$2"
repo="${GITHUB_REPOSITORY:-ilmondovero/cup-piemonte-checker}"
base="https://github.com/$repo/releases/download/$tag"
v="${tag#app-v}"

link() { # il primo file che corrisponde, come link markdown
  local f
  f=$(cd "$cartella" && ls -- $1 2>/dev/null | head -1) || true
  [ -n "$f" ] && printf '[**%s**](%s/%s)' "$2" "$base" "$f"
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
| **Linux** 64 bit | $(link '*.deb' '.deb') · $(link '*.rpm' '.rpm') · $(link '*.AppImage' '.AppImage') | Ubuntu 22.04+ e Debian 12+ · Fedora e openSUSE · le altre |

Somme SHA-256: $(link 'SHA256SUMS.txt' 'SHA256SUMS.txt').

## Al primo avvio

Gli installer non sono firmati con un certificato, quindi il sistema avvisa una volta:
- **Windows** ("Windows ha protetto il PC"): *Ulteriori informazioni*, poi *Esegui comunque*.
- **macOS** ("non può essere aperta" o "è danneggiata"): *Impostazioni di Sistema* > *Privacy e sicurezza* >
  *Apri comunque*. Se il pulsante non c'è: \`xattr -dr com.apple.quarantine "/Applications/CUP Piemonte.app"\`
- **Linux**: l'AppImage va resa eseguibile (\`chmod +x\`).

Per aggiornare si installa la versione nuova sopra la vecchia: le ricette restano.

## Com'è stata costruita

Gli installer li ha costruiti GitHub Actions dal codice di questo tag: prima i test sui tre sistemi, poi
ogni installer è stato installato e avviato su una macchina pulita (Windows, macOS, Ubuntu, Fedora).
Istruzioni complete e privacy: [app/README.md](https://github.com/$repo/tree/$tag/app#readme).
EOF
