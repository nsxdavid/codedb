"""narwhals-rolling-window-suite validator.

Tests four NEW rolling window methods on the narwhals Expr/Series namespaces:
  rolling_min, rolling_max, rolling_median, rolling_quantile
Checks (functional, observable) derived ONLY from the task spec:
 - methods exist and produce correct rolling results on a pandas-backed eager frame
 - trailing-window + null-exclusion + min_samples semantics
 - default min_samples == window_size
 - center=True windowing
 - Series namespace path works
 - rolling_quantile validation: out-of-range quantile -> ValueError "Quantile must be between 0.0 and 1.0"
 - rolling_quantile validation: bad interpolation -> ValueError "Interpolation must be one of"
"""

import sys
import json
import traceback

TASK_ID = "narwhals-rolling-window-suite"


def _close(actual, expected, tol=1e-9):
    """Compare two lists where entries may be None / NaN, with float tolerance."""
    import math

    if len(actual) != len(expected):
        return False
    for a, e in zip(actual, expected):
        a_is_null = a is None or (isinstance(a, float) and math.isnan(a))
        e_is_null = e is None
        if e_is_null:
            if not a_is_null:
                return False
            continue
        if a_is_null:
            return False
        if abs(float(a) - float(e)) > tol:
            return False
    return True


def main():
    results = {"task": TASK_ID, "checks": {}}

    # ---- import_narwhals: module + new public feature must exist ----
    try:
        import narwhals as nw

        # the NEW feature: rolling_min must be exposed on the Expr namespace
        if not hasattr(nw.Expr, "rolling_min"):
            raise AttributeError("narwhals.Expr has no attribute 'rolling_min'")
        results["checks"]["import_narwhals"] = True
    except Exception as e:
        results["checks"]["import_narwhals"] = False
        results["checks"]["import_narwhals_err"] = f"{type(e).__name__}: {e}"
        return results

    import pandas as pd

    data = {"a": [None, 1, 2, None, 4, 6, 11]}

    def eager_col(method, **kwargs):
        df = nw.from_native(pd.DataFrame(data))
        result = df.select(getattr(nw.col("a"), method)(**kwargs))
        return result.to_native()["a"].tolist()

    # ---- rolling_min: trailing window, null-excluded, min_samples=1 ----
    try:
        got = eager_col("rolling_min", window_size=3, min_samples=1)
        results["checks"]["rolling_min_values"] = _close(
            got, [None, 1, 1, 1, 2, 4, 4]
        )
    except Exception as e:
        results["checks"]["rolling_min_values"] = False
        results["checks"]["rolling_min_values_err"] = f"{type(e).__name__}: {e}"

    # ---- rolling_max ----
    try:
        got = eager_col("rolling_max", window_size=3, min_samples=1)
        results["checks"]["rolling_max_values"] = _close(
            got, [None, 1, 2, 2, 4, 6, 11]
        )
    except Exception as e:
        results["checks"]["rolling_max_values"] = False
        results["checks"]["rolling_max_values_err"] = f"{type(e).__name__}: {e}"

    # ---- rolling_median ----
    try:
        got = eager_col("rolling_median", window_size=3, min_samples=1)
        results["checks"]["rolling_median_values"] = _close(
            got, [None, 1.0, 1.5, 1.5, 3.0, 5.0, 6.0]
        )
    except Exception as e:
        results["checks"]["rolling_median_values"] = False
        results["checks"]["rolling_median_values_err"] = f"{type(e).__name__}: {e}"

    # ---- default min_samples == window_size : a window with < window_size
    #      non-null values must produce null ----
    try:
        got = eager_col("rolling_min", window_size=2)  # min_samples defaults to 2
        results["checks"]["default_min_samples_is_window"] = _close(
            got, [None, None, 1, None, None, 4, 6]
        )
    except Exception as e:
        results["checks"]["default_min_samples_is_window"] = False
        results["checks"]["default_min_samples_is_window_err"] = (
            f"{type(e).__name__}: {e}"
        )

    # ---- center=True windowing on a clean (no-null) series ----
    try:
        df = nw.from_native(pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0, 5.0]}))
        got = (
            df.select(nw.col("a").rolling_max(window_size=3, min_samples=1, center=True))
            .to_native()["a"]
            .tolist()
        )
        # centered window of 3: rows include i-1, i, i+1
        results["checks"]["center_true_window"] = _close(
            got, [2.0, 3.0, 4.0, 5.0, 5.0]
        )
    except Exception as e:
        results["checks"]["center_true_window"] = False
        results["checks"]["center_true_window_err"] = f"{type(e).__name__}: {e}"

    # ---- rolling_quantile: q=0.5 linear == median behavior ----
    try:
        df = nw.from_native(pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0, 5.0]}))
        got = (
            df.select(
                nw.col("a").rolling_quantile(
                    window_size=3, quantile=0.5, min_samples=1
                )
            )
            .to_native()["a"]
            .tolist()
        )
        results["checks"]["rolling_quantile_values"] = _close(
            got, [1.0, 1.5, 2.0, 3.0, 4.0]
        )
    except Exception as e:
        results["checks"]["rolling_quantile_values"] = False
        results["checks"]["rolling_quantile_values_err"] = f"{type(e).__name__}: {e}"

    # ---- rolling_quantile: q=0.0 == rolling min ; q=1.0 == rolling max ----
    try:
        df = nw.from_native(pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0, 5.0]}))
        q0 = (
            df.select(
                nw.col("a").rolling_quantile(window_size=3, quantile=0.0, min_samples=1)
            )
            .to_native()["a"]
            .tolist()
        )
        q1 = (
            df.select(
                nw.col("a").rolling_quantile(window_size=3, quantile=1.0, min_samples=1)
            )
            .to_native()["a"]
            .tolist()
        )
        results["checks"]["rolling_quantile_extremes"] = _close(
            q0, [1.0, 1.0, 1.0, 2.0, 3.0]
        ) and _close(q1, [1.0, 2.0, 3.0, 4.0, 5.0])
    except Exception as e:
        results["checks"]["rolling_quantile_extremes"] = False
        results["checks"]["rolling_quantile_extremes_err"] = f"{type(e).__name__}: {e}"

    # ---- rolling_quantile out-of-range quantile raises ValueError ----
    try:
        df = nw.from_native(pd.DataFrame({"a": [1.0, 2.0, 3.0]}))
        raised = False
        msg = ""
        try:
            df.select(
                nw.col("a").rolling_quantile(window_size=2, quantile=1.5)
            ).to_native()
        except ValueError as ve:
            raised = True
            msg = str(ve)
        results["checks"]["quantile_out_of_range_raises"] = raised and msg.startswith(
            "Quantile must be between 0.0 and 1.0"
        )
        if raised and not msg.startswith("Quantile must be between 0.0 and 1.0"):
            results["checks"]["quantile_out_of_range_raises_err"] = (
                f"ValueError raised but message was: {msg!r}"
            )
    except Exception as e:
        results["checks"]["quantile_out_of_range_raises"] = False
        results["checks"]["quantile_out_of_range_raises_err"] = (
            f"{type(e).__name__}: {e}"
        )

    # ---- rolling_quantile bad interpolation raises ValueError ----
    try:
        df = nw.from_native(pd.DataFrame({"a": [1.0, 2.0, 3.0]}))
        raised = False
        msg = ""
        try:
            df.select(
                nw.col("a").rolling_quantile(
                    window_size=2, quantile=0.5, interpolation="bogus"
                )
            ).to_native()
        except ValueError as ve:
            raised = True
            msg = str(ve)
        results["checks"]["quantile_bad_interpolation_raises"] = (
            raised and msg.startswith("Interpolation must be one of")
        )
        if raised and not msg.startswith("Interpolation must be one of"):
            results["checks"]["quantile_bad_interpolation_raises_err"] = (
                f"ValueError raised but message was: {msg!r}"
            )
    except Exception as e:
        results["checks"]["quantile_bad_interpolation_raises"] = False
        results["checks"]["quantile_bad_interpolation_raises_err"] = (
            f"{type(e).__name__}: {e}"
        )

    # ---- Series namespace path: rolling_min on a Series ----
    try:
        s = nw.from_native(pd.Series([None, 1, 2, None, 4, 6, 11]), series_only=True)
        got = s.rolling_min(window_size=3, min_samples=1).to_native().tolist()
        results["checks"]["series_rolling_min"] = _close(
            got, [None, 1, 1, 1, 2, 4, 4]
        )
    except Exception as e:
        results["checks"]["series_rolling_min"] = False
        results["checks"]["series_rolling_min_err"] = f"{type(e).__name__}: {e}"

    # ---- Series namespace path: rolling_median on a Series ----
    try:
        s = nw.from_native(pd.Series([1.0, 2.0, 3.0, 4.0]), series_only=True)
        got = s.rolling_median(window_size=2, min_samples=1).to_native().tolist()
        results["checks"]["series_rolling_median"] = _close(
            got, [1.0, 1.5, 2.5, 3.5]
        )
    except Exception as e:
        results["checks"]["series_rolling_median"] = False
        results["checks"]["series_rolling_median_err"] = f"{type(e).__name__}: {e}"

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
