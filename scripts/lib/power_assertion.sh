#!/bin/bash
# Houdt de machine wakker zolang een langlopend verzamelproces draait.
#
# Waarom: van 2026-07-20 t/m 2026-07-25 ving de archiver ~2% van zijn nominale
# volume (orderbook 4.280 rijen/uur wakker vs 20-90 rijen/uur slapend). Oorzaak
# was niet de code maar de host: `pmset -g` gaf `sleep 1` (idle sleep na 1
# minuut) en 767 sleep/wakes sinds boot. De enige sleep-blokkade was powerd's
# "display is on", die verdwijnt zodra het scherm uitgaat.
#
# `caffeinate -w <pid>` bindt de assertion aan de levensduur van het proces:
# gaat het verzamelproces dood, dan verdwijnt de assertion vanzelf en mag de
# machine weer slapen. Geen sudo nodig, geen permanente systeemwijziging.
#
# BEPERKING (bewust niet weggeschreven): caffeinate blokkeert *idle* sleep.
# Het dichtklappen van de deksel forceert clamshell-sleep die caffeinate niet
# kan tegenhouden. Voor echt 24/7 verzamelen moet de machine open blijven of op
# een externe display/runner draaien.

# hold_power_assertion <pid> <label>
# Start een caffeinate die leeft zolang <pid> leeft. Faalt nooit hard: het
# verzamelproces mag niet sneuvelen omdat een assertion niet kon starten.
hold_power_assertion() {
  local target_pid="$1"
  local label="${2:-collector}"

  if [ "${POWER_ASSERTION_ENABLED:-true}" != "true" ]; then
    echo "power-assertion overgeslagen (POWER_ASSERTION_ENABLED=${POWER_ASSERTION_ENABLED:-true})"
    return 0
  fi

  if ! command -v caffeinate >/dev/null 2>&1; then
    echo "WAARSCHUWING: caffeinate niet gevonden; machine kan tijdens verzamelen slapen"
    return 0
  fi

  if [ -z "$target_pid" ] || ! kill -0 "$target_pid" 2>/dev/null; then
    echo "WAARSCHUWING: geen levend pid voor power-assertion ($label)"
    return 0
  fi

  # -i idle system sleep, -m disk sleep, -s system sleep op netstroom.
  # Bewust geen -d/-u: het scherm mag slapen, dat raakt de verzameling niet.
  caffeinate -ims -w "$target_pid" >/dev/null 2>&1 &
  local caffeinate_pid=$!
  echo "power-assertion actief | label=$label | caffeinate_pid=$caffeinate_pid | target_pid=$target_pid"
  return 0
}

# assert_power_settings_sane — leest alleen, wijzigt niets (pmset schrijven
# vereist sudo en is een bewuste eigenaar-actie).
assert_power_settings_sane() {
  command -v pmset >/dev/null 2>&1 || return 0
  local sleep_minutes
  sleep_minutes="$(pmset -g 2>/dev/null | awk '$1=="sleep" {print $2; exit}')"
  if [ -n "$sleep_minutes" ] && [ "$sleep_minutes" != "0" ]; then
    echo "let op: systeem-idle-sleep staat op ${sleep_minutes} min; power-assertion compenseert dit tijdens de run"
    echo "       permanent uitzetten (eigenaar, vereist sudo): sudo pmset -a sleep 0 disksleep 0"
  fi
}
