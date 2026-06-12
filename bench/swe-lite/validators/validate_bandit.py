"""bandit-structured-nosec-directives validator.

Tests the new structured nosec directives:
  # nosec-begin [SELECTOR] / # nosec-end  -> region suppression
  # nosec-next-line [SELECTOR]            -> next-statement suppression

All checks are black-box: write a Python source file containing real
bandit-detectable issues (B404 import subprocess, B602/B607 shell=True,
B307 eval), insert directive comments, run a real BanditManager scan, and
assert observable behaviour via the issue list and the nosec/skipped_tests
metrics.

Reliable baseline findings (no config) for the probe lines used below:
  import subprocess            -> B404  (LOW / HIGH)
  subprocess.Popen(.., shell=) -> B607 + B602
  eval("...")                  -> B307  (MEDIUM / HIGH)
"""
import sys
import os
import json
import tempfile
import traceback

TASK_ID = "bandit-structured-nosec-directives"


def _scan(src, ignore_nosec=False):
    """Run a real bandit scan over `src`. Returns (issue_list, totals_dict)."""
    from bandit.core import config as b_config
    from bandit.core import manager as b_manager
    from bandit.core import test_set as b_test_set

    d = tempfile.mkdtemp()
    p = os.path.join(d, "probe.py")
    with open(p, "w") as fh:
        fh.write(src)

    b_conf = b_config.BanditConfig()
    mgr = b_manager.BanditManager(b_conf, "file")
    mgr.b_ts = b_test_set.BanditTestSet(config=b_conf)
    mgr.ignore_nosec = ignore_nosec
    mgr.discover_files([p], True)
    mgr.run_tests()
    issues = mgr.get_issue_list()
    totals = mgr.metrics.data["_totals"]
    return issues, totals


def _ids(issues):
    return sorted(i.test_id for i in issues)


def main():
    results = {"task": TASK_ID, "checks": {}}
    c = results["checks"]

    # ---- import_bandit (RETURN on failure) ----------------------------------
    try:
        from bandit.core import config as b_config  # noqa: F401
        from bandit.core import manager as b_manager  # noqa: F401
        from bandit.core import test_set as b_test_set  # noqa: F401

        # establish the baseline finding set with NO directives so every later
        # check is measured against a known starting point.
        base_src = (
            "import subprocess\n"
            'subprocess.Popen("ls", shell=True)\n'
            'eval("1+1")\n'
        )
        base_issues, base_totals = _scan(base_src)
        base_ids = _ids(base_issues)
        # require the probe code to actually trip bandit, otherwise nothing to test
        if "B404" not in base_ids or "B307" not in base_ids:
            c["import_bandit"] = False
            c["import_bandit_err"] = (
                "probe baseline did not produce expected findings: " + repr(base_ids)
            )
            return results
        c["import_bandit"] = True
    except Exception as e:
        c["import_bandit"] = False
        c["import_bandit_err"] = f"{type(e).__name__}: {e}"
        return results

    # ---- B1: region blanket suppression -------------------------------------
    # begin..end wraps ALL probe statements; everything between should vanish.
    try:
        src = (
            "# nosec-begin\n"
            "import subprocess\n"
            'subprocess.Popen("ls", shell=True)\n'
            'eval("1+1")\n'
            "# nosec-end\n"
        )
        issues, totals = _scan(src)
        c["region_blanket_suppresses_all"] = len(issues) == 0
        if not c["region_blanket_suppresses_all"]:
            c["region_blanket_suppresses_all_err"] = "ids=" + repr(_ids(issues))
    except Exception as e:
        c["region_blanket_suppresses_all_err"] = f"{type(e).__name__}: {e}"

    # ---- B2: region begin is not retroactive / takes effect next line -------
    # The finding on the SAME line as the begin directive is NOT suppressed,
    # the finding on the following line IS.
    try:
        src = (
            "import subprocess  # nosec-begin\n"  # B404 here NOT suppressed
            'eval("1+1")\n'  # suppressed (inside region)
            "# nosec-end\n"
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["region_begin_not_retroactive"] = "B404" in ids and "B307" not in ids
        if not c["region_begin_not_retroactive"]:
            c["region_begin_not_retroactive_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["region_begin_not_retroactive_err"] = f"{type(e).__name__}: {e}"

    # ---- B3: region with a specific selector only suppresses that test ------
    # nosec-begin B404 should drop the B404 import finding but keep B307 (eval).
    try:
        src = (
            "# nosec-begin B404\n"
            "import subprocess\n"
            'eval("1+1")\n'
            "# nosec-end\n"
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["region_selector_specific"] = "B404" not in ids and "B307" in ids
        if not c["region_selector_specific"]:
            c["region_selector_specific_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["region_selector_specific_err"] = f"{type(e).__name__}: {e}"

    # ---- B4: unmatched / unterminated region runs to EOF --------------------
    # No nosec-end -> everything after the begin is suppressed.
    try:
        src = (
            "import subprocess\n"  # before region -> NOT suppressed (B404)
            "# nosec-begin\n"
            'eval("1+1")\n'  # suppressed
            'subprocess.Popen("ls", shell=True)\n'  # suppressed
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["unterminated_region_to_eof"] = (
            "B404" in ids and "B307" not in ids and "B602" not in ids
        )
        if not c["unterminated_region_to_eof"]:
            c["unterminated_region_to_eof_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["unterminated_region_to_eof_err"] = f"{type(e).__name__}: {e}"

    # ---- B5: nosec-next-line suppresses ONLY the next statement -------------
    try:
        src = (
            "# nosec-next-line\n"
            'eval("1+1")\n'  # suppressed
            "import subprocess\n"  # NOT suppressed (B404 remains)
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["next_line_suppresses_one"] = "B307" not in ids and "B404" in ids
        if not c["next_line_suppresses_one"]:
            c["next_line_suppresses_one_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["next_line_suppresses_one_err"] = f"{type(e).__name__}: {e}"

    # ---- B6: nosec-next-line with selector ----------------------------------
    # Target line trips two tests (B602 + B607). Selector picks just B602.
    try:
        src = (
            "# nosec-next-line B602\n"
            'subprocess.Popen("ls", shell=True)\n'
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["next_line_selector_specific"] = "B602" not in ids and "B607" in ids
        if not c["next_line_selector_specific"]:
            c["next_line_selector_specific_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["next_line_selector_specific_err"] = f"{type(e).__name__}: {e}"

    # ---- B7: nosec-next-line skips blank / comment / grouping-only lines -----
    # The real target statement is several non-statement lines below.
    try:
        src = (
            "# nosec-next-line\n"
            "\n"  # blank
            "# just a comment\n"  # comment-only
            'eval("1+1")\n'  # this is the next *statement* -> suppressed
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["next_line_skips_noise"] = "B307" not in ids
        if not c["next_line_skips_noise"]:
            c["next_line_skips_noise_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["next_line_skips_noise_err"] = f"{type(e).__name__}: {e}"

    # ---- B8: case-insensitive directive keywords ----------------------------
    try:
        src = (
            "# NOSEC-BEGIN\n"
            'eval("1+1")\n'
            "# NoSec-End\n"
        )
        issues, totals = _scan(src)
        c["case_insensitive"] = len(issues) == 0
        if not c["case_insensitive"]:
            c["case_insensitive_err"] = "ids=" + repr(_ids(issues))
    except Exception as e:
        c["case_insensitive_err"] = f"{type(e).__name__}: {e}"

    # ---- B9: ignore-nosec disables the new directives -----------------------
    try:
        src = (
            "# nosec-begin\n"
            "import subprocess\n"
            'eval("1+1")\n'
            "# nosec-end\n"
        )
        issues, totals = _scan(src, ignore_nosec=True)
        ids = _ids(issues)
        c["ignore_nosec_disables_directives"] = "B404" in ids and "B307" in ids
        if not c["ignore_nosec_disables_directives"]:
            c["ignore_nosec_disables_directives_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["ignore_nosec_disables_directives_err"] = f"{type(e).__name__}: {e}"

    # ---- B10: blanket region increments the `nosec` metric ------------------
    try:
        src = (
            "# nosec-begin\n"
            'eval("1+1")\n'
            "# nosec-end\n"
        )
        issues, totals = _scan(src)
        c["metric_blanket_counts_nosec"] = totals.get("nosec", 0) >= 1
        if not c["metric_blanket_counts_nosec"]:
            c["metric_blanket_counts_nosec_err"] = (
                "nosec=%r skipped_tests=%r"
                % (totals.get("nosec"), totals.get("skipped_tests"))
            )
    except Exception as e:
        c["metric_blanket_counts_nosec_err"] = f"{type(e).__name__}: {e}"

    # ---- B11: specific suppression increments `skipped_tests` ---------------
    try:
        src = (
            "# nosec-next-line B307\n"
            'eval("1+1")\n'
        )
        issues, totals = _scan(src)
        c["metric_specific_counts_skipped"] = totals.get("skipped_tests", 0) >= 1
        if not c["metric_specific_counts_skipped"]:
            c["metric_specific_counts_skipped_err"] = (
                "nosec=%r skipped_tests=%r"
                % (totals.get("nosec"), totals.get("skipped_tests"))
            )
    except Exception as e:
        c["metric_specific_counts_skipped_err"] = f"{type(e).__name__}: {e}"

    # ---- B12: selector 'none' applies no suppression ------------------------
    # `none` is the special token that means the directive has no effect.
    try:
        src = (
            "# nosec-next-line none\n"
            'eval("1+1")\n'
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["selector_none_no_effect"] = "B307" in ids
        if not c["selector_none_no_effect"]:
            c["selector_none_no_effect_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["selector_none_no_effect_err"] = f"{type(e).__name__}: {e}"

    # ---- B13: indentation auto-ends an indented unterminated region ---------
    # A begin on an indented line with no explicit end auto-closes when a later
    # line has smaller indentation. The dedented statement is NOT suppressed.
    try:
        src = (
            "if True:\n"
            "    # nosec-begin\n"
            '    eval("1+1")\n'  # suppressed (inside indented region)
            "import subprocess\n"  # dedented -> region ended -> B404 remains
        )
        issues, totals = _scan(src)
        ids = _ids(issues)
        c["indent_auto_ends_region"] = "B307" not in ids and "B404" in ids
        if not c["indent_auto_ends_region"]:
            c["indent_auto_ends_region_err"] = "ids=" + repr(ids)
    except Exception as e:
        c["indent_auto_ends_region_err"] = f"{type(e).__name__}: {e}"

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {
            "task": TASK_ID,
            "fatal": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(r, indent=2))
