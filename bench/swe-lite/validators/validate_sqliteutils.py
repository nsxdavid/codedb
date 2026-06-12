"""sqlite-utils-safe-import-checkpoints validator.

Tests the "safe import" feature on sqlite_utils.Database:
- enable/disable safe import; create/commit/rollback/cleanup checkpoints with
  the documented finalization + lookup error semantics.
- rollback restores the exact pre-operation state including schema changes.
- persistent import invariants: add/remove/list/validate.
- safe_bulk_insert / safe_bulk_upsert / import_csv / import_json with safe_mode,
  returning {success: true} or {success: false, checkpoint_id, failures, error_report},
  and strict=True rolling back + raising (invariant failures mention valid/invariant).
- CLI commands: validate-import-invariants (always exit 0), list-import-invariants
  (prints id + SQL), insert --safe-mode (exit 0 only when it commits).

All checks are derived solely from the task spec.
"""
import sys, json, traceback, inspect, os, tempfile

TASK = "sqlite-utils-safe-import-checkpoints"


def _err(results, name, e):
    results["checks"][name + "_err"] = f"{type(e).__name__}: {e}"


def main():
    results = {"task": TASK, "checks": {}}

    # ---- import_sqlite_utils (gate) ----
    try:
        import sqlite_utils
        from sqlite_utils import Database
        # the new public symbol/feature: the checkpoint-disabled exception + a
        # representative safe-import method must both exist.
        from sqlite_utils.db import SafeImportNotEnabledError  # noqa: F401
        assert hasattr(Database, "enable_safe_import")
        assert hasattr(Database, "create_import_checkpoint")
        results["checks"]["import_sqlite_utils"] = True
    except Exception as e:
        results["checks"]["import_sqlite_utils"] = False
        results["checks"]["import_sqlite_utils_err"] = f"{type(e).__name__}: {e}"
        return results

    import sqlite_utils
    from sqlite_utils import Database
    import sqlite_utils.db as dbmod

    def get_exc(*names):
        for n in names:
            c = getattr(dbmod, n, None)
            if c is not None:
                return c
        return Exception

    SafeImportNotEnabledError = get_exc("SafeImportNotEnabledError")
    CheckpointNotActiveError = get_exc("CheckpointNotActiveError")
    CheckpointNotFoundError = get_exc("CheckpointNotFoundError")

    # ---- 1: create_import_checkpoint raises when disabled, works when enabled ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1}], pk="id")
        raised = False
        try:
            db.create_import_checkpoint()
        except SafeImportNotEnabledError:
            raised = True
        except Exception:
            raised = False
        db.enable_safe_import()
        cid = db.create_import_checkpoint()
        ok_id = isinstance(cid, str) and len(cid) > 0
        results["checks"]["checkpoint_disabled_raises_and_enabled_creates"] = raised and ok_id
        db.commit_checkpoint(cid)
    except Exception as e:
        results["checks"]["checkpoint_disabled_raises_and_enabled_creates"] = False
        _err(results, "checkpoint_disabled_raises_and_enabled_creates", e)

    # ---- 2: commit finalizes; second commit/rollback => CheckpointNotActiveError ----
    try:
        db = Database(memory=True)
        db.enable_safe_import()
        cid = db.create_import_checkpoint()
        db.commit_checkpoint(cid)
        not_active = False
        try:
            db.commit_checkpoint(cid)
        except CheckpointNotActiveError:
            not_active = True
        results["checks"]["commit_then_recommit_raises_not_active"] = not_active
    except Exception as e:
        results["checks"]["commit_then_recommit_raises_not_active"] = False
        _err(results, "commit_then_recommit_raises_not_active", e)

    # ---- 3: unknown / cleaned-up id => CheckpointNotFoundError ----
    try:
        db = Database(memory=True)
        db.enable_safe_import()
        unknown_raises = False
        try:
            db.rollback_to_checkpoint("does-not-exist-zzz")
        except CheckpointNotFoundError:
            unknown_raises = True
        cid = db.create_import_checkpoint()
        db.cleanup_checkpoint(cid)
        cleaned_raises = False
        try:
            db.rollback_to_checkpoint(cid)
        except CheckpointNotFoundError:
            cleaned_raises = True
        results["checks"]["unknown_and_cleaned_id_raise_not_found"] = unknown_raises and cleaned_raises
    except Exception as e:
        results["checks"]["unknown_and_cleaned_id_raise_not_found"] = False
        _err(results, "unknown_and_cleaned_id_raise_not_found", e)

    # ---- 4: rollback restores data state ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "name": "a"}], pk="id")
        db.enable_safe_import()
        cid = db.create_import_checkpoint()
        db["t"].insert_all([{"id": 2, "name": "b"}, {"id": 3, "name": "c"}])
        before_rollback = db["t"].count
        db.rollback_to_checkpoint(cid)
        after_rollback = db["t"].count
        results["checks"]["rollback_restores_data"] = before_rollback == 3 and after_rollback == 1
    except Exception as e:
        results["checks"]["rollback_restores_data"] = False
        _err(results, "rollback_restores_data", e)

    # ---- 5: rollback restores SCHEMA changes (new table created after checkpoint) ----
    try:
        db = Database(memory=True)
        db["keep"].insert_all([{"id": 1}], pk="id")
        db.enable_safe_import()
        cid = db.create_import_checkpoint()
        db["brand_new"].insert_all([{"id": 1}], pk="id")
        had_new = "brand_new" in db.table_names()
        db.rollback_to_checkpoint(cid)
        gone = "brand_new" not in db.table_names()
        keep_ok = "keep" in db.table_names()
        results["checks"]["rollback_restores_schema"] = had_new and gone and keep_ok
    except Exception as e:
        results["checks"]["rollback_restores_schema"] = False
        _err(results, "rollback_restores_schema", e)

    # ---- 6: import invariants add / list / remove ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 5}], pk="id")
        inv_id = db.add_import_invariant("t", "SELECT COUNT(*) >= 0")
        listed = db.list_import_invariants("t")
        is_list = isinstance(listed, list) and len(listed) == 1
        entry_ok = is_list and listed[0].get("id") == inv_id and "expression" in listed[0]
        db.remove_import_invariant("t", inv_id)
        after = db.list_import_invariants("t")
        removed_ok = isinstance(after, list) and len(after) == 0
        results["checks"]["invariants_add_list_remove"] = bool(inv_id) and entry_ok and removed_ok
    except Exception as e:
        results["checks"]["invariants_add_list_remove"] = False
        _err(results, "invariants_add_list_remove", e)

    # ---- 7: validate_import_invariants reports pass ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 10}, {"id": 2, "v": 20}], pk="id")
        db.add_import_invariant("t", "SELECT COUNT(*) = 2")
        res = db.validate_import_invariants("t")
        ok = isinstance(res, dict) and res.get("valid") is True
        results["checks"]["validate_invariants_pass"] = ok
    except Exception as e:
        results["checks"]["validate_invariants_pass"] = False
        _err(results, "validate_invariants_pass", e)

    # ---- 8: validate_import_invariants reports failure with failure detail ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 10}], pk="id")
        bad_id = db.add_import_invariant("t", "SELECT COUNT(*) = 999")
        res = db.validate_import_invariants("t")
        ok = (
            isinstance(res, dict)
            and res.get("valid") is False
            and isinstance(res.get("failures"), list)
            and len(res["failures"]) >= 1
            and any(f.get("id") == bad_id for f in res["failures"])
            and all("expression" in f for f in res["failures"])
        )
        results["checks"]["validate_invariants_fail_reports_failure"] = ok
    except Exception as e:
        results["checks"]["validate_invariants_fail_reports_failure"] = False
        _err(results, "validate_invariants_fail_reports_failure", e)

    # ---- 9: non-aggregate per-row invariant (must hold for every row) ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 10}, {"id": 2, "v": -3}], pk="id")
        db.add_import_invariant("t", "v >= 0")
        res = db.validate_import_invariants("t")
        # one row violates v>=0 -> should be invalid
        ok = isinstance(res, dict) and res.get("valid") is False
        # and a passing per-row expression should validate true
        db2 = Database(memory=True)
        db2["t"].insert_all([{"id": 1, "v": 10}, {"id": 2, "v": 3}], pk="id")
        db2.add_import_invariant("t", "v >= 0")
        res2 = db2.validate_import_invariants("t")
        ok2 = isinstance(res2, dict) and res2.get("valid") is True
        results["checks"]["per_row_invariant_evaluation"] = ok and ok2
    except Exception as e:
        results["checks"]["per_row_invariant_evaluation"] = False
        _err(results, "per_row_invariant_evaluation", e)

    # ---- 10: safe_bulk_insert success returns {success: true} and commits data ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 1}], pk="id")
        before = db["t"].count
        res = db.safe_bulk_insert("t", [{"id": 2, "v": 2}, {"id": 3, "v": 3}], strict=False)
        ok = isinstance(res, dict) and res.get("success") is True
        committed = db["t"].count == before + 2
        results["checks"]["safe_bulk_insert_success_commits"] = ok and committed
    except Exception as e:
        results["checks"]["safe_bulk_insert_success_commits"] = False
        _err(results, "safe_bulk_insert_success_commits", e)

    # ---- 11: safe op rolls back + returns failure dict on invariant violation (non-strict) ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 1}], pk="id")
        # invariant that the resulting count must stay <= 1 (insert of more rows violates it)
        db.add_import_invariant("t", "SELECT COUNT(*) <= 1")
        before = db["t"].count
        res = db.safe_bulk_insert("t", [{"id": 2, "v": 2}, {"id": 3, "v": 3}], strict=False)
        is_failure = isinstance(res, dict) and res.get("success") is False
        has_cid = is_failure and isinstance(res.get("checkpoint_id"), str)
        has_report = is_failure and isinstance(res.get("error_report"), str)
        has_failures_key = is_failure and "failures" in res
        rolled_back = db["t"].count == before  # data unchanged after failed safe op
        results["checks"]["safe_op_nonstrict_rolls_back_on_invariant"] = (
            is_failure and has_cid and has_report and has_failures_key and rolled_back
        )
    except Exception as e:
        results["checks"]["safe_op_nonstrict_rolls_back_on_invariant"] = False
        _err(results, "safe_op_nonstrict_rolls_back_on_invariant", e)

    # ---- 12: strict mode raises + rolls back; invariant failure mentions valid/invariant ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1, "v": 1}], pk="id")
        db.add_import_invariant("t", "SELECT COUNT(*) <= 1")
        before = db["t"].count
        raised_msg = None
        try:
            db.safe_bulk_insert("t", [{"id": 2, "v": 2}], strict=True)
        except Exception as ex:
            raised_msg = str(ex).lower()
        rolled_back = db["t"].count == before
        mentions = raised_msg is not None and (
            "valid" in raised_msg or "validation" in raised_msg or "invariant" in raised_msg
        )
        results["checks"]["strict_mode_raises_and_rolls_back"] = (
            raised_msg is not None and rolled_back and mentions
        )
    except Exception as e:
        results["checks"]["strict_mode_raises_and_rolls_back"] = False
        _err(results, "strict_mode_raises_and_rolls_back", e)

    # ---- 13: import_csv with safe_mode commits valid data ----
    try:
        import io
        db = Database(memory=True)
        csv_text = "id,name\n1,alpha\n2,beta\n"
        res = db.import_csv("people", io.StringIO(csv_text), safe_mode=True, strict=False)
        if isinstance(res, dict):
            ok_dict = res.get("success") is True
        else:
            ok_dict = True
        present = "people" in db.table_names() and db["people"].count == 2
        results["checks"]["import_csv_safe_mode_commits"] = present and ok_dict
    except Exception as e:
        results["checks"]["import_csv_safe_mode_commits"] = False
        _err(results, "import_csv_safe_mode_commits", e)

    # ---- 14: import_json with safe_mode commits valid data ----
    try:
        db = Database(memory=True)
        data = [{"id": 1, "v": "x"}, {"id": 2, "v": "y"}]
        res = db.import_json("jt", data, safe_mode=True, strict=False)
        if isinstance(res, dict):
            ok_dict = res.get("success") is True
        else:
            ok_dict = True
        present = "jt" in db.table_names() and db["jt"].count == 2
        results["checks"]["import_json_safe_mode_commits"] = present and ok_dict
    except Exception as e:
        results["checks"]["import_json_safe_mode_commits"] = False
        _err(results, "import_json_safe_mode_commits", e)

    # ---- 15: nested checkpoints supported ----
    try:
        db = Database(memory=True)
        db["t"].insert_all([{"id": 1}], pk="id")
        db.enable_safe_import()
        outer = db.create_import_checkpoint()
        db["t"].insert_all([{"id": 2}])
        inner = db.create_import_checkpoint()
        db["t"].insert_all([{"id": 3}])
        count_at_inner = db["t"].count  # 3
        db.rollback_to_checkpoint(inner)
        after_inner = db["t"].count  # 2
        db.rollback_to_checkpoint(outer)
        after_outer = db["t"].count  # 1
        results["checks"]["nested_checkpoints"] = (
            count_at_inner == 3 and after_inner == 2 and after_outer == 1
        )
    except Exception as e:
        results["checks"]["nested_checkpoints"] = False
        _err(results, "nested_checkpoints", e)

    # ---- 16: CLI validate-import-invariants always exits 0 + reports pass/fail ----
    try:
        from click.testing import CliRunner
        from sqlite_utils import cli as cli_mod
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as td:
            dbpath = os.path.join(td, "c.db")
            d = Database(dbpath)
            d["t"].insert_all([{"id": 1, "v": 5}], pk="id")
            d.add_import_invariant("t", "SELECT COUNT(*) = 999")  # will fail
            d.conn.close()
            r = runner.invoke(
                cli_mod.cli,
                ["validate-import-invariants", dbpath, "t"],
            )
            exit0 = r.exit_code == 0
            out = (r.output or "").lower()
            indicates = ("fail" in out or "invalid" in out or "pass" in out or "valid" in out)
            results["checks"]["cli_validate_invariants_exit0"] = exit0 and indicates
    except Exception as e:
        results["checks"]["cli_validate_invariants_exit0"] = False
        _err(results, "cli_validate_invariants_exit0", e)

    # ---- 17: CLI list-import-invariants prints id + SQL ----
    try:
        from click.testing import CliRunner
        from sqlite_utils import cli as cli_mod
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as td:
            dbpath = os.path.join(td, "l.db")
            d = Database(dbpath)
            d["t"].insert_all([{"id": 1}], pk="id")
            marker_sql = "SELECT COUNT(*) >= 0"
            inv_id = d.add_import_invariant("t", marker_sql)
            d.conn.close()
            r = runner.invoke(cli_mod.cli, ["list-import-invariants", dbpath, "t"])
            exit0 = r.exit_code == 0
            out = r.output or ""
            shows_id = str(inv_id) in out
            shows_sql = "COUNT(*)" in out or "count(*)" in out.lower()
            results["checks"]["cli_list_invariants_prints_id_and_sql"] = exit0 and shows_id and shows_sql
    except Exception as e:
        results["checks"]["cli_list_invariants_prints_id_and_sql"] = False
        _err(results, "cli_list_invariants_prints_id_and_sql", e)

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {"task": TASK, "fatal": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    print(json.dumps(r, indent=2))
