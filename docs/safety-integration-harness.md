# Isolated safety integration harness

This test harness must never be pointed at production paths. It is structurally
confined to pytest-managed temporary directories. Every source, archive, state,
history and restore staging path must resolve beneath one `tmp_path` sandbox
root. No test receives, discovers or accesses a real SAM/NAS or backup HDD path.
The path guard raises `SandboxViolation` before any fixture write or file move;
negative tests exercise each externally supplied fake-rclone path and config or
staging escapes.

Run this suite with `python -m pytest tests/test_safety_integration.py`, or run
the full suite with `python -m pytest`. The existing Linux Python 3.9–3.14
matrix and macOS production job both use the full command.

The production planner, safety gates, backup executor, rename identity proof,
destination-side rename, execution reconciliation, verifier, ledger, restore
and prune code run unchanged. The harness simulates only the external rclone
process and, on Linux, the no-replace filesystem primitive. Its rclone fixture
produces combined planning output and JSON execution logs, and performs real
copy and move operations on small synthetic files beneath `tmp_path`. The
optimized rename tests exercise the real capability parser with simulated
backend output, and the real rename logic; macOS uses the native primitive.

| Scenario | Coverage |
| --- | --- |
| First sync, new file, modification, old version, deletion, deleted version, restore both versions | `test_first_sync_update_delete_restore_and_unicode` |
| File rename and folder move, fallback archive/copy | `test_rename_or_folder_move_preserves_original` |
| File rename and folder move, optimized without retransmission | `test_optimized_rename_without_retransmission` |
| No overwrite of existing or concurrent destination | `test_existing_destination_is_never_overwritten`, `test_concurrent_destination_conflict_preserves_both_files` in `test_renames.py` |
| Unexpectedly empty source and wrong source or destination marker | `test_preflight_rejects_empty_and_wrong_markers` |
| Insufficient capacity and mass deletion | `test_insufficient_capacity_blocks_execution`, `test_mass_delete_blocked_before_file_mutation` |
| Recent/in-progress file and concurrent execution | `test_recent_file_is_ignored`, `test_concurrent_backup_stops_before_planning` |
| Failed/interrupted execution and checksum mismatch cannot promote known-good | `test_failed_execution_never_becomes_known_good`, `test_checksum_mismatch_never_becomes_known_good` |
| Prune dry-run, real prune and confinement to history | `test_prune_changes_only_fixture_history` |
| Paths with spaces, accents and Unicode | `test_first_sync_update_delete_restore_and_unicode`, `test_optimized_rename_without_retransmission` |
| Tampered prune path and symlink escape | `test_manipulated_plan_path_is_rejected`, `test_symlink_escape_is_rejected_and_outside_is_intact`, `test_symlink_substitution_after_plan_aborts_before_deletion` in `test_pruning.py` |
| Sandbox escape rejection | `test_fixture_rejects_external_paths_before_mutation`, `test_config_and_restore_staging_cannot_leave_sandbox` |

This harness does not prove real rclone behavior, filesystem durability after
power loss, real storage capacity, or safety on production mounts. The native
macOS no-replace behavior is separately covered by
`test_real_macos_rename_no_replace` in the macOS production job.
