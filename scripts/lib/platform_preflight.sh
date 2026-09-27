#!/usr/bin/env bash

detect_architecture() {
  local arch="${CGC_UNAME_M:-$(uname -m)}"
  case "$arch" in
    arm64|x86_64) printf '%s\n' "$arch" ;;
    *) echo "ERROR: unsupported architecture: $arch" >&2; return 1 ;;
  esac
}

homebrew_prefix_for_architecture() {
  case "$1" in
    arm64) printf '%s\n' /opt/homebrew ;;
    x86_64) printf '%s\n' /usr/local ;;
    *) echo "ERROR: unsupported architecture: $1" >&2; return 1 ;;
  esac
}

require_macos_platform() {
  local system="${CGC_UNAME_S:-$(uname -s)}"
  [[ "$system" == "Darwin" ]] || { echo "ERROR: macOS required" >&2; return 1; }
  detect_architecture >/dev/null
}

find_compatible_python() {
  local required="$1" arch prefix candidate actual
  arch="$(detect_architecture)" || return 1
  prefix="${CGC_HOMEBREW_PREFIX:-$(homebrew_prefix_for_architecture "$arch")}" || return 1
  candidate="${CGC_PYTHON_BIN:-$prefix/bin/python$required}"
  [[ -x "$candidate" ]] || {
    echo "ERROR: Homebrew Python $required not found at $candidate" >&2
    echo "Install it with: $prefix/bin/brew install python@$required" >&2
    return 1
  }
  actual="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [[ "$actual" == "$required" ]] || {
    echo "ERROR: Python $required required; found $actual at $candidate" >&2
    return 1
  }
  printf '%s\n' "$candidate"
}

require_homebrew() {
  local arch prefix brew
  arch="$(detect_architecture)" || return 1
  prefix="${CGC_HOMEBREW_PREFIX:-$(homebrew_prefix_for_architecture "$arch")}" || return 1
  brew="$prefix/bin/brew"
  [[ -x "$brew" ]] || {
    echo "ERROR: Homebrew is required at $brew; install it manually from https://brew.sh" >&2
    return 1
  }
  printf '%s\n' "$brew"
}
