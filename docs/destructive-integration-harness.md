# Destructive integration harness

Run with `python -m pytest tests/test_destructive_integration.py`; the existing
Linux matrix and macOS production job also run it through `python -m pytest`.
Each test creates source, archive, state, history and staging under `tmp_path`.
The fixture runner replaces only the external rclone process: it writes the
combined report and JSON execution log and performs actual copies and moves
inside the fixture. The production planner, safety gates, backup executor,
verification, ledger, restore and prune code run unchanged.

| Scenario | Coverage |
| --- | --- |
| First sync, new file, modification, old version, deletion, deleted version, restore both versions | `test_first_sync_update_delete_restore_and_unicode` |
| File rename and folder move | `test_rename_or_folder_move_preserves_original` |
| Unexpectedly empty source and wrong source or destination marker | `test_preflight_rejects_empty_and_wrong_markers` |
| Insufficient capacity and mass deletion | `test_insufficient_capacity_blocks_execution`, `test_mass_delete_blocked_before_file_mutation` |
| Recent/in-progress file and concurrent execution | `test_recent_file_is_ignored`, `test_concurrent_backup_stops_before_planning` |
| Failed/interrupted execution and checksum mismatch cannot promote known-good | `test_failed_execution_never_becomes_known_good`, `test_checksum_mismatch_never_becomes_known_good` |
| Prune dry-run, real prune and confinement to history | `test_prune_changes_only_fixture_history` |
| Paths with spaces, accents and Unicode | `test_first_sync_update_delete_restore_and_unicode`, `test_rename_or_folder_move_preserves_original` |
| Tampered prune path and symlink escape | `test_manipulated_plan_path_is_rejected`, `test_symlink_escape_is_rejected_and_outside_is_intact`, `test_symlink_substitution_after_plan_aborts_before_deletion` in `test_pruning.py` |

The harness uses a simulated rclone boundary because rclone is not a test
dependency. The native macOS no-replace operation remains covered by
`test_real_macos_rename_no_replace` in the macOS production job.
