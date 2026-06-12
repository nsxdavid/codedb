"""vulture-persistent-analysis-cache validator.

Tests the persistent analysis cache feature described in the SPEC:
  * vulture.cache module with normalize_path(), get_cache_path(), __version__,
    importlib imported at module scope.
  * Vulture(cache_dir=..., cache_settings=...) constructor params.
  * --cache / --cache-clear / --cache-dir CLI flags.
  * cache.json top-level "modules" key; cache.json.bak + cache.json.meta
    written on every save (incl. first save); meta has "sha256".
  * On a 2nd run with no changes, files are reused (Vulture._cache_stats
    {"scanned","reused"} as sets of normalized paths).
  * Changed file + transitive importer re-analyzed.
  * Corrupt cache -> stderr warning "cache is corrupted or unreadable" + rescan.
  * SHA-256 mismatch in cache.json.meta treated as corruption.
"""
import sys, os, json, traceback, subprocess, tempfile, shutil, textwrap, hashlib
from pathlib import Path

TASK_ID = "vulture-persistent-analysis-cache"


def _write(p, text):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(textwrap.dedent(text), encoding="utf-8")


def _make_project(root):
    """Create a small package with a transitive import chain a <- b <- c."""
    _write(os.path.join(root, "a.py"), """
        def used_in_b():
            return 1

        def dead_a():
            return 0
    """)
    _write(os.path.join(root, "b.py"), """
        from a import used_in_b

        def used_in_c():
            return used_in_b() + 1
    """)
    _write(os.path.join(root, "c.py"), """
        from b import used_in_c

        print(used_in_c())
    """)


def _run_cli(args, cwd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "vulture", *args],
        cwd=cwd, capture_output=True, text=True, env=e,
    )


def main():
    results = {"task": TASK_ID, "checks": {}}
    C = results["checks"]

    # ---- import gate -------------------------------------------------------
    try:
        import vulture
        from vulture.core import Vulture
        import vulture.cache as vcache
        # The new public surface must exist.
        assert hasattr(vcache, "normalize_path")
        assert hasattr(vcache, "get_cache_path")
        assert hasattr(vcache, "__version__")
        C["import_vulture"] = True
    except Exception as e:
        C["import_vulture"] = False
        C["import_vulture_err"] = f"{type(e).__name__}: {e}"
        return results

    # ---- B1: normalize_path returns a normalized string/path --------------
    try:
        np = vcache.normalize_path("Foo/Bar.py")
        # Idempotent: normalizing an already-normalized path equals itself.
        again = vcache.normalize_path(np)
        C["normalize_path_idempotent"] = (str(again) == str(np))
        C["normalize_path_nonempty"] = bool(str(np))
    except Exception as e:
        C["normalize_path_idempotent"] = False
        C["normalize_path_idempotent_err"] = f"{type(e).__name__}: {e}"

    # ---- B2: get_cache_path points at cache.json under cache_dir ----------
    try:
        cp = vcache.get_cache_path("some_cache_dir")
        C["get_cache_path_is_path"] = isinstance(cp, Path)
        C["get_cache_path_cache_json"] = (Path(cp).name == "cache.json")
    except Exception as e:
        C["get_cache_path_is_path"] = False
        C["get_cache_path_err"] = f"{type(e).__name__}: {e}"

    # ---- B3: importlib imported at module scope in vulture.cache ----------
    try:
        import importlib as _il
        mod_importlib = getattr(vcache, "importlib", None)
        C["cache_module_imports_importlib"] = (mod_importlib is _il)
    except Exception as e:
        C["cache_module_imports_importlib"] = False
        C["cache_module_imports_importlib_err"] = f"{type(e).__name__}: {e}"

    # ---- B4: Vulture constructor accepts cache_dir + cache_settings -------
    try:
        import inspect
        sig = inspect.signature(Vulture.__init__)
        has_cache_dir = "cache_dir" in sig.parameters
        has_cache_settings = "cache_settings" in sig.parameters
        # Must actually construct with these kwargs without error.
        with tempfile.TemporaryDirectory() as td:
            Vulture(cache_dir=os.path.join(td, "cd"), cache_settings={})
        C["ctor_accepts_cache_kwargs"] = bool(has_cache_dir and has_cache_settings)
    except Exception as e:
        C["ctor_accepts_cache_kwargs"] = False
        C["ctor_accepts_cache_kwargs_err"] = f"{type(e).__name__}: {e}"

    # ---- B5: --cache CLI run creates a cache.json with "modules" key ------
    #         AND cache.json.bak + cache.json.meta (with sha256) on first save
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            r = _run_cli(["--cache", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
            cache_file = Path(cache_dir) / "cache.json"
            exists = cache_file.is_file()
            C["cli_cache_creates_file"] = exists
            data = {}
            if exists:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
            C["cache_has_modules_key"] = ("modules" in data)
            # backup + meta written even on first save
            C["cache_bak_written"] = (Path(cache_dir) / "cache.json.bak").is_file()
            meta_path = Path(cache_dir) / "cache.json.meta"
            C["cache_meta_written"] = meta_path.is_file()
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                # meta has sha256 of cache.json contents
                actual = hashlib.sha256(cache_file.read_bytes()).hexdigest()
                C["cache_meta_has_sha256"] = (meta.get("sha256") == actual)
            else:
                C["cache_meta_has_sha256"] = False
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["cli_cache_creates_file"] = False
        C["cli_cache_err"] = f"{type(e).__name__}: {e}"

    # ---- B6: --cache-clear empties the cache dir before running ----------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            os.makedirs(cache_dir, exist_ok=True)
            # plant a stale marker file
            marker = Path(cache_dir) / "STALE_MARKER.txt"
            marker.write_text("stale", encoding="utf-8")
            _run_cli(["--cache", "--cache-clear", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
            C["cache_clear_removes_stale"] = (not marker.exists())
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["cache_clear_removes_stale"] = False
        C["cache_clear_err"] = f"{type(e).__name__}: {e}"

    # ---- B7: _cache_stats reuse on unchanged 2nd run (via API) -----------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            paths = [os.path.join(td, f) for f in ("a.py", "b.py", "c.py")]

            v1 = Vulture(cache_dir=cache_dir)
            v1.scavenge(paths)
            stats1 = getattr(v1, "_cache_stats", None)
            ok_stats_shape = (
                isinstance(stats1, dict)
                and isinstance(stats1.get("scanned"), set)
                and isinstance(stats1.get("reused"), set)
            )
            C["cache_stats_shape"] = bool(ok_stats_shape)
            # On the first run everything is scanned, nothing reused.
            C["first_run_scans_files"] = bool(ok_stats_shape and len(stats1["scanned"]) >= 1
                                              and len(stats1["reused"]) == 0)

            # Second run, no changes -> files should be reused, not re-scanned.
            v2 = Vulture(cache_dir=cache_dir)
            v2.scavenge(paths)
            stats2 = getattr(v2, "_cache_stats", None)
            ok2 = (isinstance(stats2, dict)
                   and isinstance(stats2.get("reused"), set)
                   and isinstance(stats2.get("scanned"), set))
            C["second_run_reuses_unchanged"] = bool(ok2 and len(stats2["reused"]) >= 1
                                                     and len(stats2["scanned"]) == 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["cache_stats_shape"] = False
        C["cache_stats_err"] = f"{type(e).__name__}: {e}"

    # ---- B8: changed file + its transitive importers re-analyzed ---------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            a, b, c = (os.path.join(td, f) for f in ("a.py", "b.py", "c.py"))
            paths = [a, b, c]

            v1 = Vulture(cache_dir=cache_dir)
            v1.scavenge(paths)

            # Modify a.py (which b imports, which c imports).
            _write(a, """
                def used_in_b():
                    return 42

                def dead_a():
                    return 0
            """)

            v2 = Vulture(cache_dir=cache_dir)
            v2.scavenge(paths)
            stats2 = getattr(v2, "_cache_stats", {})
            scanned = stats2.get("scanned", set())
            na = vcache.normalize_path(a)
            # The changed file must be re-scanned.
            C["changed_file_rescanned"] = (na in scanned)
            # And at least one importer is pulled in too (more than just a.py).
            C["transitive_importer_rescanned"] = (len(scanned) >= 2)
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["changed_file_rescanned"] = False
        C["changed_file_rescanned_err"] = f"{type(e).__name__}: {e}"

    # ---- B9: corrupt cache.json -> warning + full rescan -----------------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            # First, populate the cache via CLI.
            _run_cli(["--cache", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
            cache_file = Path(cache_dir) / "cache.json"
            # Corrupt it with invalid JSON.
            cache_file.write_text("}{ this is not json", encoding="utf-8")
            r = _run_cli(["--cache", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
            combined = (r.stderr or "") + (r.stdout or "")
            C["corrupt_cache_warns"] = ("cache is corrupted or unreadable" in combined)
            # Run still completes (full rescan), so exit code is a normal vulture
            # exit code (0..3), not a crash (>=4 typically for unhandled).
            C["corrupt_cache_recovers"] = (r.returncode in (0, 1, 2, 3))
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["corrupt_cache_warns"] = False
        C["corrupt_cache_err"] = f"{type(e).__name__}: {e}"

    # ---- B10: SHA-256 mismatch in cache.json.meta -> corruption ----------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            _run_cli(["--cache", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
            meta_path = Path(cache_dir) / "cache.json.meta"
            cache_file = Path(cache_dir) / "cache.json"
            if meta_path.is_file() and cache_file.is_file():
                # Tamper the recorded checksum so it no longer matches contents.
                meta_path.write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
                r = _run_cli(["--cache", "--cache-dir", cache_dir, "a.py", "b.py", "c.py"], cwd=td)
                combined = (r.stderr or "") + (r.stdout or "")
                C["sha256_mismatch_warns"] = ("cache is corrupted or unreadable" in combined)
                C["sha256_mismatch_recovers"] = (r.returncode in (0, 1, 2, 3))
            else:
                C["sha256_mismatch_warns"] = False
                C["sha256_mismatch_warns_err"] = "meta/cache file not produced on first run"
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["sha256_mismatch_warns"] = False
        C["sha256_mismatch_err"] = f"{type(e).__name__}: {e}"

    # ---- B11: cache_settings change triggers full re-scan ----------------
    try:
        td = tempfile.mkdtemp()
        try:
            _make_project(td)
            cache_dir = os.path.join(td, ".vulture-cache")
            paths = [os.path.join(td, f) for f in ("a.py", "b.py", "c.py")]

            v1 = Vulture(cache_dir=cache_dir, cache_settings={"min_confidence": 0})
            v1.scavenge(paths)

            # Different cache_settings -> previous entries invalid -> rescan all.
            v2 = Vulture(cache_dir=cache_dir, cache_settings={"min_confidence": 60})
            v2.scavenge(paths)
            stats2 = getattr(v2, "_cache_stats", {})
            reused = stats2.get("reused", set())
            scanned = stats2.get("scanned", set())
            C["cache_settings_change_rescans"] = (len(reused) == 0 and len(scanned) >= 1)
        finally:
            shutil.rmtree(td, ignore_errors=True)
    except Exception as e:
        C["cache_settings_change_rescans"] = False
        C["cache_settings_change_rescans_err"] = f"{type(e).__name__}: {e}"

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {"task": TASK_ID, "fatal": str(e), "traceback": traceback.format_exc()}
    print(json.dumps(r, indent=2))
