#!/bin/bash
# runners-restart-safe.sh — Gracefully restart molecule-runner containers.
#
# Before restarting a runner container, waits for any active Gitea Actions
# task that is currently running on it to complete (so we don't kill a
# running job mid-execution).  After restart, verifies the runner
# successfully re-registers with the platform by checking for the
# 'declare successfully' log line.
#
# Usage: bin/runners-restart-safe.sh
# Requires: docker, logger

set -eu

LOG_TAG="runners-restart-safe"
# TEST_MODE=1 skips the task-wait loop (MAX_WAIT_MINUTES=0) so tests run fast.
# In test mode the fake docker must faithfully represent what docker returns.
MAX_WAIT_MINUTES=${TEST_MODE:-60}   # default 60 min; 0 = skip wait loop

# Poll every 30 s for running tasks on a given runner.
# Returns 0 when the runner is idle (safe to restart).
# Returns 1 when tasks are still running (caller must skip this runner).
wait_for_idle() {
    local name="$1"
    local waited=0
    while true; do
        # Temporarily disable set -e so grep's "no match" (rc=1) does not
        # cause an early script exit.  The assignment captures the exit code.
        set +e
        docker ps --format '{{.Names}}' | grep -qE "GITEA-ACTIONS-TASK-.+-${name}"
        local rc=$?
        set -e
        if (( rc == 0 )); then
            # Task still running — wait or give up.
            if (( waited >= MAX_WAIT_MINUTES * 60 )); then
                logger -t "$LOG_TAG" "$name: waited ${MAX_WAIT_MINUTES}m for tasks — giving up"
                return 0   # timed out → treat as idle, let restart proceed
            fi
            sleep 30
            waited=$(( waited + 30 ))
        else
            break   # no tasks, runner is idle
        fi
    done
    return 1   # idle → safe to restart
}

restart_runner() {
    local name="$1"

    # Skip if the container doesn't exist.
    if ! docker inspect "$name" >/dev/null 2>&1; then
        return 0
    fi

    # Wait for in-flight tasks to drain before touching the container.
    # wait_for_idle returns 1 when idle (safe to restart), 0 when busy/timed-out.
    wait_for_idle "$name"; local wi_rc=$?
    if (( wi_rc != 1 )); then
        # Runner is busy or timed out — skip silently.
        return 0
    fi

    docker restart -t 30 "$name" || true

    # Give the runner process time to start and emit its re-register log line.
    sleep 8

    # Verify re-registration succeeded.
    set +e
    docker logs --since 30s "$name" 2>&1 | grep -q 'declare successfully'
    local rc=$?
    set -e
    if (( rc == 0 )); then
        logger -t "$LOG_TAG" "$name: recycled OK"
    else
        logger -t "$LOG_TAG" "$name: failed to re-register (no 'declare successfully' in recent logs)"
        return 1
    fi
}

main() {
    local failures=0
    for i in 1 2 3 4 5 6 7 8; do
        name="molecule-runner-$i"
        restart_runner "$name" || failures=$(( failures + 1 ))
    done

    if (( failures > 0 )); then
        logger -t "$LOG_TAG" "completed with $failures runner(s) failing re-register check"
        exit 1
    fi
    logger -t "$LOG_TAG" "all runners recycled OK"
}

main "$@"
