#!/bin/bash
set -euo pipefail
WORKDIR="$(pwd)"
COOKIE_FILE="$WORKDIR/cookies.txt"

# Prüfe ob Tools vorhanden sind
command -v ffmpeg >/dev/null 2>&1 || { echo "❌ ffmpeg ist nicht installiert"; exit 1; }
command -v yt-dlp >/dev/null 2>&1 || { echo "❌ yt-dlp ist nicht installiert"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "❌ jq ist nicht installiert"; exit 1; }

# Init parameters
USERNAME=""
PASSWORD=""
SEARCH_STRING=""
SUBTITLES=0
BILINGUAL=0
EXTERNAL_DL=""
LIMIT_RESULTS=0

# Parse command-line arguments
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        -u|--username) USERNAME="$2"; shift ;;
        -p|--password) PASSWORD="$2"; shift ;;
        -f|--filter) SEARCH_STRING="$2"; shift ;;
        -s|--subtitles) SUBTITLES=1 ;;
        -b|--bilingual) BILINGUAL=1 ;;
        -e|--external-dl) EXTERNAL_DL="$2"; shift ;;
        -l|--limit-results) LIMIT_RESULTS="$2"; shift ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

if [[ -z "$USERNAME" || -z "$PASSWORD" ]]
then
  echo "Credentials missing! Please start the script with at least 2 parameters: "
  echo "./zattoo-downloader.sh -u <username> -p <password>"
  exit 1
fi

DOMAIN="zattoo.com"

# HTTP Header
HEADERS=(
  -H "Accept: application/json"
  -H "Accept-Language: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.87 Safari/537.36"
  -H "Content-Type: application/x-www-form-urlencoded"
  -H "X-Requested-With: XMLHttpRequest"
  -H "Referer: https://$DOMAIN/client"
  -H "Origin: https://$DOMAIN"
  -H "Host: $DOMAIN"
)

# --- Login ---
login() {
  echo "🔑 Neuer Login..."

  # Alte Cookie-Datei löschen, falls vorhanden
  [ -f "$COOKIE_FILE" ] && rm -f "$COOKIE_FILE"

  APP_TOKEN=$(curl -s "https://$DOMAIN/token.json" "${HEADERS[@]}" -c "$COOKIE_FILE" | jq -r '.session_token')
  UUID=$(uuidgen)
  echo "uuid=$UUID; Domain=$DOMAIN; Path=/; Secure; HttpOnly" >> "$COOKIE_FILE"

  SESSION_INFO=$(curl -s -X POST "https://$DOMAIN/zapi/v3/session/hello" \
    -d "uuid=$UUID&lang=en&format=json&app_version=3.2120.1&client_app_token=$APP_TOKEN" \
    "${HEADERS[@]}" \
    -b "$COOKIE_FILE" -c "$COOKIE_FILE")

  SUCCESS=$(echo "$SESSION_INFO" | jq -r '.active')
  if [[ "$SUCCESS" != "true" ]]; then
    echo "❌ Session Hello fehlgeschlagen"
    exit 1
  fi

  encoded_username=$(printf %s "$USERNAME" | jq -s -R -r @uri)
  encoded_password=$(printf %s "$PASSWORD" | jq -s -R -r @uri)
  
  LOGIN_RESPONSE=$(curl -s -c "$COOKIE_FILE" -b "$COOKIE_FILE" \
    "${HEADERS[@]}" \
    --data "login=$encoded_username&password=$encoded_password&remember=true&format=json" \
    "https://$DOMAIN/zapi/v3/account/login")

  if echo "$LOGIN_RESPONSE" | jq -e '.active' >/dev/null 2>&1; then
    echo "✅ Login erfolgreich."
  else
    echo "❌ Login fehlgeschlagen."
    exit 1
  fi
}

# --- Cookie-Check ---
check_or_login() {
  if [[ -f "$COOKIE_FILE" ]]; then
    TEST=$(curl -s -b "$COOKIE_FILE" "${HEADERS[@]}" \
      "https://$DOMAIN/zapi/channels/favorites")
    if echo "$TEST" | jq -e '.success == true' >/dev/null 2>&1; then
      echo
      echo "✅ Cookie noch gültig - bereits eingeloggt"
      return
    else
      echo
      echo "⚠️  Cookie abgelaufen – neuer Login"
      login
    fi
  else
    login
  fi
}

# --- fetch all zattoo recordings ---
fetch_recordings() {
  # add logic
  RECORDINGS_ALL=$(curl -s "https://$DOMAIN/zapi/v2/playlist" \
    "${HEADERS[@]}" \
    -b "$COOKIE_FILE")
  
  if [[ "$(uname)" == "Darwin" ]]; then
    # macOS
    now=$(date -u +"%s")
  else
    # Linux
    now=$(date -u +%s)
  fi

  # filter scheduled recordings from the future
  RECORDINGS=$(echo "$RECORDINGS_ALL" | jq --arg now "$now" '
    .recordings |= map(
      select((.end | fromdateiso8601) <= ($now | tonumber))
    )
  ')
  echo "$RECORDINGS"
}

# --- Download mit Spinner ---
download_with_spinner() {
    local URL="$1"
    local FILENAME="$2"
    local FILENAME_NO_EXT="${FILENAME%.mp4}"
    local metubeStatus=""

    if [[ -n "$EXTERNAL_DL" ]]; then
      FILENAME_PLAIN=$(basename "$FILENAME_NO_EXT")     

      # forward download to metube
      metubeResponse=$(curl -s -X POST "http://$EXTERNAL_DL/add" \
        -H "Content-Type: application/json" \
        -d "{
          \"url\": \"${URL}\",
          \"quality\": \"best\",
          \"format\": \"any\",
          \"auto_start\": true,
          \"custom_name_prefix\": \"${FILENAME_PLAIN}\"
        }")
      metubeStatus=$(echo $metubeResponse | jq -r '.status')
    elif (( BILINGUAL )); then
      yt-dlp --quiet --progress --no-warnings --audio-multistreams -f "bv+mergeall[vcodec=none]" --sub-langs "en.*,de.*,fr.*,es.*" --embed-subs --merge-output-format mp4 ${URL} -o "$FILENAME"
    else
      # yt-dlp --quiet --progress --no-warnings ${URL} -o "$FILENAME"
      ffmpeg -i ${URL} -map 0:v:0 -map 0:a:0 -c copy -stats -loglevel 0 "$FILENAME"
    fi
    
    EXIT_CODE=$?

    if [[ -n "EXTERNAL_DL" ]]; then
      if [[ "$metubeStatus" == "ok" ]]; then
        echo -e "\r✅ Download erfolgreich an Metube übergeben."
      else 
        echo -e "\r❌ Download via Metube fehlgeschlagen."
      fi
      sleep 2
    elif [ $EXIT_CODE -eq 0 ]; then
        echo -e "\r✅ Download abgeschlossen: $FILENAME"
    else
        echo -e "\r❌ Download fehlgeschlagen oder abgebrochen."
    fi
    echo
}

# --- Hauptprogramm ---
main() {
  check_or_login

  echo "⏳ Zattoo Aufnahmen abrufen..."
  RECORDINGS=$(fetch_recordings)
  
  echo
  echo "📝 Verfügbare Aufnahmen:"
  echo "------------------------"
  echo

  SEARCH_LOWER=$(echo "$SEARCH_STRING" | tr '[:upper:]' '[:lower:]')

  # total amount (for column width)
  TOTAL=$(echo "$RECORDINGS" | jq '.recordings | length' 2>/dev/null || echo 0)
  if [[ "$TOTAL" -eq 0 ]]; then
    echo "Keine Aufnahmen gefunden."
    exit 0
  fi
  MAX_NUM_LEN=${#TOTAL}

  # TSV with numbers (original index), ID (or program_id fallback), CID, TITLE, EPISODE, START
  RECS=$(jq -r '
    .recordings
    | to_entries[]
    | .key as $num
    | .value as $rec
    | [
        ($num + 1 | tostring),
        ($rec.id // $rec.program_id // ""),
        ($rec.cid // ""),
        ($rec.title // ""),
        (($rec.episode_title // "") | if . == "" then "*" else . end),
        ($rec.start // "")
      ]
    | @tsv
  ' <<< "$RECORDINGS")

  declare -a PROGRAM_IDS=() # indexed by num-1 -> id

  count=0
  while IFS=$'\t' read -r NUM ID CID TITLE EPISODE START; do
      
      # show first or last n entries
      if [[ -n "$LIMIT_RESULTS" ]]; then
        ((count++))
        if [[ "$LIMIT_RESULTS" -gt 0 ]]; then
          if (( count > LIMIT_RESULTS )); then
              break
          fi
        elif [[ "$LIMIT_RESULTS" -lt 0  ]]; then
          if (( count <= TOTAL + LIMIT_RESULTS )); then
            continue
          fi
        fi
      fi
      PROGRAM_IDS[$((NUM-1))]="$ID"

      # filter by search term, if available
      if [[ -n "$SEARCH_STRING" ]]; then
          TITLE_LOWER=$(echo "$TITLE" | awk '{print tolower($0)}')
          if [[ "$EPISODE" != "*" ]]; then
            EPISODE_LOWER=$(echo "$EPISODE" | awk '{print tolower($0)}')
            if [[ "$TITLE_LOWER" != *"$SEARCH_LOWER"* && "$EPISODE_LOWER" != *"$SEARCH_LOWER"* ]]; then
              continue
            fi
          else 
            if [[ "$TITLE_LOWER" != *"$SEARCH_LOWER"* ]]; then
              continue
            fi
          fi
      fi

      # Date: ISO -> TT.MM.JJJJ
      FMT_DATE=""
      if [[ -n "$START" && "$START" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})T ]]; then
          FMT_DATE="${BASH_REMATCH[3]}.${BASH_REMATCH[2]}.${BASH_REMATCH[1]}"
      fi

      if [[ "$EPISODE" == "*" ]]; then
          printf "%*s | %s | %s | %s\n\n" \
            "$MAX_NUM_LEN" "$NUM" "$TITLE" "$CID" "$FMT_DATE"
      else
          printf "%*s | %s - %s | %s | %s\n\n" \
            "$MAX_NUM_LEN" "$NUM" "$TITLE" "$EPISODE" "$CID" "$FMT_DATE"
      fi

  done <<< "$RECS"

  MAX_NUM=${#PROGRAM_IDS[@]}

  echo "------------------------"
  echo

  # --- multiple choice ---
  while true; do
      echo -n "Bitte gib die Nummern der Aufnahmen ein für den Download (z.B. 1,12-16): "
      read INPUT

      # Prüfen, dass nur Zahlen, Kommas, Bindestriche und Leerzeichen enthalten sind
      if ! echo "$INPUT" | grep -Eq '^[-0-9, ]+$'; then
          echo "❌ Ungültige Eingabe. Nur Zahlen, Kommas und Bindestriche erlaubt."
          continue
      fi

      SELECTED_NUMS=()
      IFS=',' read -ra PARTS <<< "$INPUT"
      for p in "${PARTS[@]}"; do
          part=$(echo "$p" | xargs)
          [ -z "$part" ] && continue

          if echo "$part" | grep -Eq '^[0-9]+$'; then
              if [ "$part" -lt 1 ] || [ "$part" -gt "$MAX_NUM" ]; then
                  echo "❌ Nummer $part außerhalb des gültigen Bereichs."
                  continue 2
              fi
              SELECTED_NUMS+=("$part")
          elif echo "$part" | grep -Eq '^[0-9]+-[0-9]+$'; then
              start=$(echo "$part" | cut -d'-' -f1)
              end=$(echo "$part" | cut -d'-' -f2)
              if [ "$start" -gt "$end" ]; then
                  echo "❌ Ungültiger Bereich $part."
                  continue 2
              fi
              if [ "$start" -lt 1 ] || [ "$end" -gt "$MAX_NUM" ]; then
                  echo "❌ Bereich $part außerhalb des gültigen Bereichs."
                  continue 2
              fi
              for ((i=start;i<=end;i++)); do
                  SELECTED_NUMS+=("$i")
              done
          else
              echo "❌ Ungültiges Format: $part"
              continue 2
          fi
      done

      # remove duplicates and sort
      IFS=$'\n' SELECTED_NUMS=($(sort -nu <<<"${SELECTED_NUMS[*]}"))
      break
  done

  echo
  # echo "Gewählte Aufnahmen:"
  # echo "${SELECTED_NUMS[*]}"
  # echo

  mkdir -p "$WORKDIR/output"
  i=1
  TOTAL=${#SELECTED_NUMS[@]}
  for NUM in "${SELECTED_NUMS[@]}"; do
      INDEX=$((NUM-1))
      MATCH="${PROGRAM_IDS[$INDEX]}"
      RECS_LINE=$(echo "$RECS" | sed -n "${NUM}p")

      IFS=$'\t' read -r _ _ _ TITLE EPISODE DATE <<< "$RECS_LINE"

      DATE_ONLY="${DATE%%Z}"
      DATE_ONLY="${DATE_ONLY//T/ }"

      # → 2025-10-01 15h10
      DATE_FORMATTED=$(echo "$DATE_ONLY" | sed -E 's/^([0-9-]+) ([0-9]{2}):([0-9]{2}).*/\1 \2h\3/')

      SAFE_TITLE=$(echo "$TITLE" | sed 's/[\/:*?"<>|\\]/ /g')
      SAFE_EPISODE=$(echo "$EPISODE" | sed 's/[\/:*?"<>|\\]/ /g')

      BASE_FILENAME="$WORKDIR/output/${DATE_FORMATTED} ${SAFE_TITLE}"
      if [[ -n "$SAFE_EPISODE" && "$EPISODE" != "*" ]]; then
        BASE_FILENAME="${BASE_FILENAME} - ${SAFE_EPISODE}"
      fi
      EXT=".mp4"
      FILENAME="${BASE_FILENAME}${EXT}"

      if [ -f "$FILENAME" ]; then
          echo "⚠️  Datei existiert bereits: $FILENAME"
          while true; do
              read -p "Möchtest du die Datei nochmal herunterladen? (y/n): " yn
              case $yn in
                  [Yy]* ) break;;
                  [Nn]* ) echo "⏹ Download übersprungen.";echo; continue 2;;
                  * ) echo "Bitte y oder n eingeben.";;
              esac
          done
      fi

      STREAM_URL="https://$DOMAIN/zapi/watch/recording/$MATCH"
      STREAM_JSON=$(curl -s -X POST "$STREAM_URL" \
        -d "with_schedule=false&stream_type=hls7_fairplay&https_watch_urls=true&sdh_subtitles=true" \
        "${HEADERS[@]}" -b "$COOKIE_FILE")

      URL=$(echo "$STREAM_JSON" | jq -r '.stream.url')
      URL_CLEAN=$(echo "$URL" | sed 's/enc//g' | sed -E 's#(/m)[^/]*\.m3u8#\1.m3u8#')
      
      FILENAME_PLAIN=$(basename "$FILENAME")
      FILENAME_PLAIN="${FILENAME_PLAIN%.*}"
      if [[ "$EPISODE" != "*" ]]; then
        echo "🎬 Download $i von $TOTAL startet gleich: $NUM | $TITLE - $EPISODE ..."
      else 
        echo "🎬 Download $i von $TOTAL startet gleich: $NUM | $TITLE ..."
      fi
      download_with_spinner "$URL_CLEAN" "$FILENAME"
      ((i++))
  done

  echo "🎉 Alle gewählten Aufnahmen wurden verarbeitet."

}

main "$@"