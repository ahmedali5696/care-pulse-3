#!/usr/bin/env bash
# Loaded only by the disposable profiling controller, never by Full Foundation.
profile_clock() {
  python3 -c 'from time import monotonic_ns; print(monotonic_ns())'
}

profile_checkpoint() {
  local tick
  tick="$(profile_clock)" || return 1
  printf '%s\t%s\n' "$1" "$tick" >> "$profile_dir/timings.tsv"
}

docker() {
  local -a args=("$@")
  local report='' arg index status=0
  for arg in "${args[@]}"; do
    case "$arg" in
      --junitxml=/tmp/foundation-results/*.xml)
        report="${arg##*/}"
        report="${report%.xml}"
        ;;
    esac
  done
  if [ -z "$report" ]; then
    command docker "$@" || status=$?
    return "$status"
  fi
  [[ "$report" =~ ^[a-z0-9_-]+$ ]] || return 1
  if [[ "$profile_variant" = instrumented && "$report" =~ ^(f01|d08|b09|b10|w3_queue_scope|w3_checkin|w3_admission_confirmations)$ ]]; then
    for ((index=0; index+1<${#args[@]}; index++)); do
      if [[ "${args[index]}" = -m && "${args[index+1]}" = pytest ]]; then
        args=("${args[@]:0:index+1}" ops.profile_ordinary "/tmp/foundation-results/profile-$report.json" "${args[@]:index+2}")
        break
      fi
    done
    [[ "${args[index+1]:-}" = ops.profile_ordinary ]] || return 1
  fi
  profile_checkpoint "pytest:$report:start" || return 1
  command docker "${args[@]}" || status=$?
  profile_checkpoint "pytest:$report:end:$status" || return 1
  return "$status"
}
